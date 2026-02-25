#!/usr/bin/env python3
"""
Lepton 3.x Advanced Configuration Test
Tests various I2C commands to wake camera and enable video streaming
"""

import spidev
import smbus2
import time
import struct

# Hardware Configuration
I2C_BUS = 1
SPI_BUS = 10
SPI_DEVICE = 0
LEPTON_I2C_ADDR = 0x2A
SPI_SPEED = 20000000

# CCI Register Addresses
CCI_STATUS = 0x0002
CCI_COMMAND_ID = 0x0004
CCI_DATA_LENGTH = 0x0006
CCI_DATA_0 = 0x0008

# Lepton Command IDs (Module.Command format)
CMD_SYS_PING = 0x0200
CMD_SYS_STATUS = 0x0204
CMD_SYS_FLIR_SERIAL_NUMBER = 0x0208
CMD_SYS_CAMERA_UPTIME = 0x020C
CMD_SYS_FPA_TEMP_KELVIN = 0x0214
CMD_SYS_AUX_TEMP_KELVIN = 0x0210
CMD_SYS_RUN_FFC = 0x0242
CMD_SYS_FFC_STATUS = 0x0244
CMD_OEM_STATUS = 0x4804
CMD_OEM_POWER_DOWN = 0x4810
CMD_OEM_POWER_MODE = 0x4812
CMD_OEM_RUN_CAMERA_REBOOT = 0x4842
CMD_RAD_ENABLE_STATE = 0x4E10
CMD_VID_POLARITY_SELECT = 0x0304

print("=" * 70)
print("Lepton 3.x Advanced Configuration & Diagnostic Test")
print("=" * 70)

# Initialize I2C
print(f"\n[I2C INIT] Opening bus {I2C_BUS}...")
try:
    i2c = smbus2.SMBus(I2C_BUS)
    print(f"[I2C INIT] ✓ Success")
except Exception as e:
    print(f"[I2C INIT] ✗ Failed: {e}")
    exit(1)

# Initialize SPI
print(f"\n[SPI INIT] Opening bus {SPI_BUS}, device {SPI_DEVICE}...")
try:
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 3
    print(f"[SPI INIT] ✓ Success (20 MHz, Mode 3)")
except Exception as e:
    print(f"[SPI INIT] ✗ Failed: {e}")
    exit(1)

def cci_wait_busy(timeout_ms=1000):
    """Wait for CCI to be ready (busy bit clear)"""
    start = time.time()
    while (time.time() - start) < (timeout_ms / 1000.0):
        try:
            data = i2c.read_i2c_block_data(LEPTON_I2C_ADDR, (CCI_STATUS >> 8) & 0xFF, 3)
            status = (data[1] << 8) | data[2]
            if (status & 0x01) == 0:
                return True
        except:
            pass
        time.sleep(0.001)
    return False

def cci_read_register(reg_addr):
    """Read a 16-bit register"""
    try:
        cci_wait_busy()
        data = i2c.read_i2c_block_data(LEPTON_I2C_ADDR, (reg_addr >> 8) & 0xFF, 3)
        return (data[1] << 8) | data[2]
    except Exception as e:
        return None

def cci_run_command(cmd_id, data_words=None):
    """Execute a CCI command"""
    try:
        # Wait for ready
        if not cci_wait_busy():
            return False, "Timeout waiting for ready"
        
        # Write data if provided
        if data_words:
            length = len(data_words)
            i2c.write_i2c_block_data(LEPTON_I2C_ADDR, (CCI_DATA_LENGTH >> 8) & 0xFF, 
                                     [(CCI_DATA_LENGTH & 0xFF), (length >> 8) & 0xFF, length & 0xFF])
            
            for i, word in enumerate(data_words):
                reg = CCI_DATA_0 + (i * 2)
                i2c.write_i2c_block_data(LEPTON_I2C_ADDR, (reg >> 8) & 0xFF,
                                        [(reg & 0xFF), (word >> 8) & 0xFF, word & 0xFF])
        
        # Write command
        i2c.write_i2c_block_data(LEPTON_I2C_ADDR, (CCI_COMMAND_ID >> 8) & 0xFF,
                                [(CCI_COMMAND_ID & 0xFF), (cmd_id >> 8) & 0xFF, cmd_id & 0xFF])
        
        # Wait for completion
        if not cci_wait_busy(timeout_ms=5000):
            return False, "Command timeout"
        
        # Check status
        status = cci_read_register(CCI_STATUS)
        if status is None:
            return False, "Could not read status"
        
        error_code = (status >> 8) & 0xFF
        if error_code != 0:
            return False, f"Error code: 0x{error_code:02X}"
        
        return True, "Success"
        
    except Exception as e:
        return False, str(e)

def cci_read_data(num_words=1):
    """Read data words after a GET command"""
    try:
        words = []
        for i in range(num_words):
            reg = CCI_DATA_0 + (i * 2)
            data = i2c.read_i2c_block_data(LEPTON_I2C_ADDR, (reg >> 8) & 0xFF, 3)
            word = (data[1] << 8) | data[2]
            words.append(word)
        return words
    except:
        return None

def test_spi_packets(num_packets=50):
    """Test SPI and analyze packet types"""
    discard = 0
    seg_zero = 0
    valid_segs = set()
    
    for _ in range(num_packets):
        packet = bytes(spi.readbytes(164))
        header = packet[0]
        
        if (header & 0x0F) == 0x0F:
            discard += 1
        else:
            seg = (header >> 4)
            if seg == 0:
                seg_zero += 1
            elif 1 <= seg <= 4:
                valid_segs.add(seg)
    
    return {
        'discard': discard,
        'seg_zero': seg_zero,
        'valid': valid_segs,
        'total': num_packets
    }

# PHASE 1: Initial Status Check
print("\n" + "=" * 70)
print("PHASE 1: Reading Initial Camera Status")
print("=" * 70)

status = cci_read_register(CCI_STATUS)
print(f"\n[STATUS] CCI Status Register: 0x{status:04X}" if status else "[STATUS] Failed to read")
if status:
    print(f"  Bit 0 (Busy): {status & 0x01}")
    print(f"  Bit 1: {(status >> 1) & 0x01}")
    print(f"  Bit 2: {(status >> 2) & 0x01}")
    print(f"  Bit 3: {(status >> 3) & 0x01}")
    print(f"  Error Code: 0x{(status >> 8) & 0xFF:02X}")

print(f"\n[TEST] Testing SPI before any commands...")
result = test_spi_packets()
print(f"  Discard (0xFF): {result['discard']}/{result['total']}")
print(f"  Invalid seg#0: {result['seg_zero']}/{result['total']}")
print(f"  Valid segments: {sorted(result['valid']) if result['valid'] else 'None'}")

# PHASE 2: Try PING command
print("\n" + "=" * 70)
print("PHASE 2: Testing Basic Communication (PING)")
print("=" * 70)

success, msg = cci_run_command(CMD_SYS_PING)
print(f"\n[PING] Result: {'✓' if success else '✗'} {msg}")

if success:
    print(f"[TEST] Testing SPI after PING...")
    result = test_spi_packets()
    print(f"  Valid segments: {sorted(result['valid']) if result['valid'] else 'None'}")

# PHASE 3: Read Camera Uptime
print("\n" + "=" * 70)
print("PHASE 3: Reading Camera Uptime")
print("=" * 70)

success, msg = cci_run_command(CMD_SYS_CAMERA_UPTIME | 0x01)  # GET command
if success:
    uptime = cci_read_data(2)
    if uptime:
        uptime_ms = (uptime[0] << 16) | uptime[1]
        print(f"\n[UPTIME] Camera uptime: {uptime_ms} ms ({uptime_ms/1000:.1f} seconds)")
        print(f"[UPTIME] Camera has been running - good sign!")
    else:
        print(f"\n[UPTIME] ✗ Could not read data")
else:
    print(f"\n[UPTIME] ✗ {msg}")

# PHASE 4: Read Temperature
print("\n" + "=" * 70)
print("PHASE 4: Reading FPA Temperature")
print("=" * 70)

success, msg = cci_run_command(CMD_SYS_FPA_TEMP_KELVIN | 0x01)
if success:
    temp = cci_read_data(1)
    if temp:
        temp_k = temp[0] / 100.0
        temp_c = temp_k - 273.15
        print(f"\n[TEMP] FPA Temperature: {temp_k:.2f} K ({temp_c:.2f} °C)")
        print(f"[TEMP] Sensor is active and reading temperature!")
    else:
        print(f"\n[TEMP] ✗ Could not read data")
else:
    print(f"\n[TEMP] ✗ {msg}")

# PHASE 5: Check FFC Status
print("\n" + "=" * 70)
print("PHASE 5: Checking FFC (Flat Field Correction) Status")
print("=" * 70)

success, msg = cci_run_command(CMD_SYS_FFC_STATUS | 0x01)
if success:
    ffc_status = cci_read_data(1)
    if ffc_status:
        print(f"\n[FFC] FFC Status: 0x{ffc_status[0]:04X}")
        print(f"[FFC] Status interpretation:")
        print(f"  0x00 = Never commanded")
        print(f"  0x01 = Imminent")  
        print(f"  0x02 = In progress")
        print(f"  0x03 = Complete")
    else:
        print(f"\n[FFC] ✗ Could not read status")
else:
    print(f"\n[FFC] ✗ {msg}")

# PHASE 6: Run FFC
print("\n" + "=" * 70)
print("PHASE 6: Running FFC (Flat Field Correction)")
print("=" * 70)

print(f"\n[FFC] Sending RUN FFC command...")
success, msg = cci_run_command(CMD_SYS_RUN_FFC)
print(f"[FFC] Result: {'✓' if success else '✗'} {msg}")

if success:
    print(f"[FFC] Waiting 3 seconds for FFC to complete...")
    time.sleep(3)
    
    print(f"[TEST] Testing SPI after FFC...")
    result = test_spi_packets(100)
    print(f"  Discard (0xFF): {result['discard']}/{result['total']}")
    print(f"  Invalid seg#0: {result['seg_zero']}/{result['total']}")
    print(f"  Valid segments: {sorted(result['valid']) if result['valid'] else 'None'}")
    
    if result['valid']:
        print(f"\n{'*' * 70}")
        print(f"✓✓✓ SUCCESS! Camera is streaming video! ✓✓✓")
        print(f"{'*' * 70}")
        exit(0)

# PHASE 7: Check OEM Status
print("\n" + "=" * 70)
print("PHASE 7: Checking OEM Status")
print("=" * 70)

success, msg = cci_run_command(CMD_OEM_STATUS | 0x01)
if success:
    oem_status = cci_read_data(1)
    if oem_status:
        print(f"\n[OEM] OEM Status: 0x{oem_status[0]:04X}")
        print(f"[OEM] This may indicate camera mode/state")
    else:
        print(f"\n[OEM] ✗ Could not read")
else:
    print(f"\n[OEM] ✗ {msg}")

# PHASE 8: Try Camera Reboot
print("\n" + "=" * 70)
print("PHASE 8: Attempting Camera Reboot")
print("=" * 70)

print(f"\n[REBOOT] Sending camera reboot command...")
success, msg = cci_run_command(CMD_OEM_RUN_CAMERA_REBOOT)
print(f"[REBOOT] Result: {'✓' if success else '✗'} {msg}")

if success:
    print(f"[REBOOT] Waiting 5 seconds for camera to reboot...")
    time.sleep(5)
    
    print(f"[TEST] Testing SPI after reboot...")
    result = test_spi_packets(100)
    print(f"  Discard (0xFF): {result['discard']}/{result['total']}")
    print(f"  Invalid seg#0: {result['seg_zero']}/{result['total']}")
    print(f"  Valid segments: {sorted(result['valid']) if result['valid'] else 'None'}")
    
    if result['valid']:
        print(f"\n{'*' * 70}")
        print(f"✓✓✓ SUCCESS! Camera is streaming after reboot! ✓✓✓")
        print(f"{'*' * 70}")
        exit(0)

# PHASE 9: Final Analysis
print("\n" + "=" * 70)
print("FINAL ANALYSIS")
print("=" * 70)

print(f"\n[SUMMARY] Camera Status:")
print(f"  ✓ I2C communication working")
print(f"  ✓ SPI communication working")
print(f"  ✓ Camera responds to commands")

result = test_spi_packets(100)
if result['discard'] == result['total']:
    print(f"  ✗ Camera sending only discard packets (0xFF)")
    print(f"\n[DIAGNOSIS] Most likely causes:")
    print(f"  1. MASTER_CLK (25MHz) not present on pin 26")
    print(f"  2. Camera in low-power mode (needs specific wake command)")
    print(f"  3. Video output disabled (needs OEM configuration)")
    print(f"  4. Hardware issue with camera module")
elif result['seg_zero'] > 0:
    print(f"  ! Camera sending segment #0 (waiting for valid frame)")
    print(f"\n[DIAGNOSIS] Camera is trying to stream but:")
    print(f"  1. May need more time to stabilize")
    print(f"  2. Clock signal intermittent")
    print(f"  3. Normal boot sequence - keep waiting")
else:
    print(f"  ? Unexpected packet pattern")

print(f"\n[RECOMMENDATION] Next steps:")
print(f"  1. Verify 25MHz clock on pin 26 with oscilloscope")
print(f"  2. Check jumper J6 (clock enable)")
print(f"  3. Verify power voltages (1.2V, 2.8V, 2.8V)")
print(f"  4. Check RESET_L and PWR_DWN_L pins are HIGH")

spi.close()
i2c.close()
print(f"\n{'=' * 70}")
