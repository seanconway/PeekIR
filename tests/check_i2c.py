#!/usr/bin/env python3
"""
Quick I2C diagnostic tool for Lepton camera
Tests if the camera is visible on the I2C bus
"""

import sys

try:
    import smbus2
except ImportError:
    print("ERROR: smbus2 not installed. Run: uv pip install smbus2")
    sys.exit(1)

I2C_BUS = 1  # Standard Raspberry Pi I2C bus
LEPTON_ADDRESS = 0x2A

print(f"Lepton Camera I2C Diagnostic")
print(f"=" * 50)
print(f"Bus: {I2C_BUS}")
print(f"Expected Address: 0x{LEPTON_ADDRESS:02X}")
print()

try:
    bus = smbus2.SMBus(I2C_BUS)
    print(f"✓ I2C bus {I2C_BUS} opened successfully")
except Exception as e:
    print(f"✗ Failed to open I2C bus {I2C_BUS}: {e}")
    print(f"\nTroubleshooting:")
    print(f"  1. Check I2C is enabled: ls /dev/i2c-*")
    print(f"  2. Check permissions: groups (should include i2c or gpio)")
    print(f"  3. Try with sudo: sudo python3 {sys.argv[0]}")
    sys.exit(1)

# Scan for devices on the bus
print(f"\nScanning I2C bus {I2C_BUS}...")
devices_found = []

for addr in range(0x03, 0x78):
    try:
        bus.read_byte(addr)
        devices_found.append(addr)
        if addr == LEPTON_ADDRESS:
            print(f"  0x{addr:02X}: Lepton Camera ✓")
        else:
            print(f"  0x{addr:02X}: Device")
    except:
        pass

if not devices_found:
    print(f"  No devices found on bus {I2C_BUS}")
    print(f"\nTroubleshooting:")
    print(f"  1. Check camera power (3-5.5V on power pin)")
    print(f"  2. Check pull-up resistors on SDA/SCL")
    print(f"  3. Verify correct I2C bus number")
    print(f"  4. Check camera is properly seated in socket")
    sys.exit(1)

if LEPTON_ADDRESS not in devices_found:
    print(f"\n✗ Lepton camera NOT found at address 0x{LEPTON_ADDRESS:02X}")
    print(f"  Found {len(devices_found)} other device(s)")
    print(f"\nPossible issues:")
    print(f"  1. Wrong I2C bus (try bus 1 or 0)")
    print(f"  2. Camera not powered")
    print(f"  3. Hardware connection issue")
else:
    print(f"\n✓ Lepton camera detected at 0x{LEPTON_ADDRESS:02X}!")
    print(f"\nTesting register read...")
    
    try:
        # Try to read status register
        CCI_STATUS_REG = 0x0002
        bus.write_byte_data(LEPTON_ADDRESS, 0x00, 0x02)
        import time
        time.sleep(0.001)
        data = bus.read_i2c_block_data(LEPTON_ADDRESS, 0, 2)
        status = (data[0] << 8) | data[1]
        print(f"  Status register: 0x{status:04X}")
        print(f"  Busy bit: {status & 0x01}")
        print(f"  Boot status: {(status >> 2) & 0x01}")
        print(f"\n✓ Camera is communicating via I2C!")
        print(f"\nYou can now run: uv run python ircam_test.py")
    except Exception as e:
        print(f"  Warning: Could not read status register: {e}")
        print(f"  Camera is present but may need initialization")

bus.close()
print(f"\nDone.")
