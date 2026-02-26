# FLIR Lepton UWFOV Camera Module - Debugging Report

**Date**: February 24, 2026  
**Camera Module**: FLIR Lepton UWFOV (PN: 500-1387-00), 160x120, 160° FOV  
**Breakout Board**: FLIR Lepton Breakout Board v2.0 (PN: 250-0577-00)  
**Host**: Raspberry Pi 5  
**Objective**: Get the IR camera streaming thermal video over SPI

---

## 1. Background

The FLIR Lepton UWFOV (Ultra-Wide Field of View) is a 160x120 LWIR thermal imaging module. It communicates via two interfaces:
- **SPI** (Video over SPI / VoSPI): Streams 160x120 thermal video frames at ~27 Hz segment rate (~9 Hz unique frames)
- **I2C** (Command and Control Interface / CCI): Configures camera settings, reads status, controls operation

### UWFOV-Specific Characteristics

The Lepton UWFOV differs from other Lepton 3.x models in ways relevant to this debugging effort:
- **No integral shutter**: FFC defaults to "External" mode (not "Automatic"). The camera will NOT perform FFC at startup, and FFC status of "Never commanded" is expected behavior.
- **No radiometry**: Temperature readings (FPA, AUX) may not be available. A reading of 0.00K does not necessarily indicate a dead processor on this model.
- **Same internal ASIC**: Despite these feature differences, the UWFOV uses the same SoC, VoSPI protocol, power requirements, and clock requirements as all other Lepton 3.x models (3.0 FS1, 3.1R, 3.5).

The camera was connected to a Raspberry Pi 5 via the Lepton Breakout Board v2.0. The initial test script could read SPI packets but the camera never produced valid video frames.

---

## 2. Test Sequence and Findings

### Test 1: Initial SPI Communication

**Script**: `ircam_test.py` (original version)  
**Method**: Open SPI bus 10, device 0 at 20 MHz, Mode 3. Read 164-byte VoSPI packets and attempt to assemble 160x120 frames from 4 segments of 60 packets each.

**Result**: Script appeared to hang silently with no output.

**Finding**: The script was running but stuck in an infinite loop inside `get_frame()`, waiting for valid segments that never arrived. No debugging output existed to diagnose the issue.

---

### Test 2: SPI Packet Analysis (Added Diagnostics)

**Script**: `ircam_test.py` (with debugging output added)  
**Method**: Added packet counting, header byte analysis, and segment tracking to identify what the camera was actually sending.

**Result**:
```
[FRAME] Discarded 26000 packets so far...
```
100% of packets had header byte `0xFF`. Zero valid segments received.

**Finding**: The camera was sending only **discard packets** (`0xFF` header). According to the Lepton datasheet, discard packets are sent when:
- Camera is not initialized
- Camera is in standby mode
- VoSPI is out of sync
- No video data is available

---

### Test 3: VoSPI Resynchronization Attempts

**Script**: `ircam_test.py` (with resync logic)  
**Method**: Added automatic VoSPI resynchronization by cycling the SPI connection (close, wait 200ms, reopen) per datasheet recommendation of deasserting CS for >185ms.

**Result**: 5 resync attempts all failed. Camera continued sending 100% discard packets.

**Finding**: The problem was not VoSPI synchronization. The camera was fundamentally not streaming video data.

---

### Test 4: I2C Bus Discovery

**Script**: `check_i2c.py`  
**Method**: Scanned available I2C buses (`/dev/i2c-*`) to find the Lepton at its expected address 0x2A.

**Results**:
- Bus 10: Does not exist (`/dev/i2c-10` not found)
- Bus 4: No device at 0x2A
- Bus 11: No device at 0x2A (devices at 0x08, 0x1A used by kernel)
- Bus 13, 14: False positives across nearly all addresses (no pull-ups, invalid bus)
- **Bus 1: Camera found at 0x2A**

**Finding**: The Lepton camera's I2C interface is on **bus 1** (standard Raspberry Pi I2C). The original script incorrectly assumed bus 10.

---

### Test 5: I2C Communication + SPI (Wrong SPI Bus)

**Script**: `ircam_test.py` (I2C on bus 1, SPI changed to bus 0)  
**Method**: Connected to camera via I2C on bus 1, read status register, then attempted SPI on bus 0.

**Result**:
```
I2C Camera status: 0x0000
SPI: 100% packets with header 0x00 (Invalid segment #0)
```

**Finding**: I2C communication confirmed working. SPI bus 0 produced all-zero packets (segment #0), which is different from the `0xFF` discard packets seen on bus 10. This indicated bus 0 was the wrong SPI bus.

---

### Test 6: Correct Bus Configuration (I2C bus 1, SPI bus 10)

**Script**: `ircam_test.py` (I2C bus 1, SPI bus 10)  
**Method**: Restored SPI to bus 10 (correct for Raspberry Pi 5) while keeping I2C on bus 1.

**Result**: Camera status `0x0006`, SPI still showing 100% discard packets.

**Finding**: Correct bus configuration identified:
- **I2C: Bus 1, Address 0x2A** (confirmed with `i2cdetect -y 1`)
- **SPI: Bus 10, Device 0** (Raspberry Pi 5 numbering)

Status register `0x0006` (bits 1 and 2 set) indicated partial boot progress, but camera still not streaming.

---

### Test 7: Advanced CCI Command Testing

**Script**: `ircam_test2.py`  
**Method**: Systematically tested I2C commands: PING, read camera uptime, read FPA temperature, check FFC status, run FFC, check OEM status, and attempt camera reboot. Tested SPI after each command.

**Results**:
```
PING: ✓ Success
Uptime: 0 ms (0.0 seconds)
FPA Temperature: 0.00 K (-273.15 °C)
FFC Status: 0x0000 (Never commanded)
Run FFC: ✓ Success (but no effect on SPI)
OEM Status: 0x0000
Camera Reboot: ✓ Success (but no effect on SPI)
SPI: 100% discard packets after every command
```

**Finding**: This was the critical diagnostic breakthrough. The camera's I2C interface responds to all commands, but:
- **Uptime = 0**: The processor has no sense of time passing
- **Temperature = 0.00 K (-273.15°C)**: Sensor not reading. Note: the UWFOV model lacks radiometry, so temperature readings may not be available even on a functional unit. This data point is therefore inconclusive for this specific model.
- **FFC Status = 0x0000 (Never commanded)**: Expected for the UWFOV since it has no shutter and defaults to External FFC mode. Not indicative of failure.
- **All other status registers = 0x0000**: No internal state
- **Commands "succeed" but have no effect**: Acknowledged by I2C interface but not executed

**Conclusion**: The camera's **main processor/ASIC is not running**. The I2C interface operates on minimal power and responds, but the core processing unit that captures images, reads sensors, and streams video is inactive. While the temperature and FFC readings are inconclusive for the shutterless, non-radiometric UWFOV model, the **uptime of 0 ms** and complete absence of video data remain definitive indicators that the processor is not booting.

---

### Test 8: Power Supply Verification

**Method**: Measured voltages on breakout board header pins with multimeter.

**Results**:
| Pin | Signal | Expected | Measured | Status |
|-----|--------|----------|----------|--------|
| 4 | VCC28 (VDD) | 2.8V | 2.8V | ✓ Correct |
| 6 | VCC28_IO (VDDIO) | 2.8V | 2.8V | ✓ Correct |
| 16 | VCC12 (VDDC) | 1.2V | 1.2V | ✓ Correct |
| 17 | RESET_L | 2.8V | 2.57V | ✗ Low |
| 20 | PW_DWN_L | 2.8V | 2.45V | ✗ Low |

**Finding**: All three power rails are correct. However, both active-low control signals (RESET_L and PW_DWN_L) are below VDDIO due to weak pull-up resistors on the breakout board. These marginal voltages could prevent the processor from fully exiting reset/power-down.

---

### Test 9: Control Signal Pull-up Fix

**Method**: Connected Pin 17 (RESET_L) and Pin 20 (PW_DWN_L) directly to Pin 6 (VDDIO/2.8V) via jumper wires to ensure full logic HIGH.

**Result**: No change. Camera processor still not running. All registers still 0x0000. SPI still 100% discard packets.

**Finding**: Weak pull-ups on RESET_L and PW_DWN_L were not the root cause.

---

### Test 10: Master Clock Verification

**Method**: Probed Pin 18 (MASTER_CLK) and the oscillator at J6 using oscilloscope.

**Results**:
- Oscillator at J6: 25.00 MHz signal present
- Pin 18 (MASTER_CLK): 25.00 MHz, 2.4V amplitude, waveform described as "squiggly square wave"
- Pin 26 (MASTER_CLK at camera): Signal confirmed reaching camera

**Finding**: The 25 MHz clock is present and reaching the camera, but with potential signal quality issues:
- **Amplitude**: 2.4V peak-to-peak vs expected 2.8V (86% of VDDIO)
- **Waveform quality**: Poor signal integrity ("squiggly"), possible ringing, noise, or inadequate rise/fall times
- The datasheet specifies rise/fall times should be within TBD limits; poor signal integrity at 25 MHz could prevent the internal PLL from locking

---

## 3. Summary of Verified Hardware State

| Parameter | Status | Notes |
|-----------|--------|-------|
| VDD (2.8V sensor) | ✓ | Correct |
| VDDIO (2.8V I/O) | ✓ | Correct |
| VDDC (1.2V core) | ✓ | Correct |
| RESET_L | ✓ | Pulled to VDDIO via jumper |
| PW_DWN_L | ✓ | Pulled to VDDIO via jumper |
| MASTER_CLK (25 MHz) | ⚠️ | Present but 2.4V amplitude, poor waveform |
| I2C communication | ✓ | Responds at 0x2A on bus 1 |
| SPI communication | ✓ | Responds on bus 10 with discard packets |
| Main processor | ✗ | Not running (uptime=0, temp=0, all regs=0) |
| Video streaming | ✗ | Only discard packets (0xFF) |

---

## 4. UWFOV Model Considerations

Several diagnostic data points must be interpreted differently for the Lepton UWFOV (500-1387-00) compared to shuttered, radiometric models like the Lepton 3.5:

| Data Point | Other Models | UWFOV Interpretation |
|-----------|-------------|---------------------|
| FPA Temperature = 0.00K | Definitive failure | Inconclusive (no radiometry) |
| FFC Status = "Never commanded" | Possible failure | Expected (no shutter, External FFC default) |
| Uptime = 0 ms | Definitive failure | **Definitive failure** (same ASIC) |
| All registers = 0x0000 | Definitive failure | **Definitive failure** (same ASIC) |
| No video streaming | Definitive failure | **Definitive failure** (same VoSPI) |

The UWFOV's lack of radiometry and shutter makes two of the diagnostic readings inconclusive. However, the remaining evidence (zero uptime, zero OEM status, no video output) is sufficient to confirm the processor is not running, since the UWFOV uses the identical SoC and VoSPI protocol as all other Lepton 3.x models.

---

## 5. Conclusion

After extensive testing across hardware and software, the Lepton UWFOV camera module's **main ASIC/processor is not booting**. The I2C peripheral interface responds to commands (operating on minimal power), but the core processing unit that captures images and streams video is completely inactive.

### Most Likely Root Cause

**The camera module is likely defective, possibly due to ESD damage.**

Supporting evidence:
- All external conditions for operation are met (power, clock, control signals)
- I2C works but processor reads all zeros (partial functionality = partial damage)
- Clock signal quality is marginal (2.4V vs 2.8V) but may not be the sole cause
- The Lepton's ESD rating is moderate: 2kV HBM, 500V CDM. Without proper ESD precautions during handling, the main ASIC's clock input or core logic could be damaged while leaving the I2C interface functional

### Secondary Possibility

**Clock signal quality is preventing PLL lock.** The 25 MHz clock reaching the camera has:
- 14% reduced amplitude (2.4V vs 2.8V expected)
- Poor waveform integrity

The internal PLL may fail to lock onto a degraded clock signal, preventing the processor from starting. A clock buffer IC (such as a 74LVC1G17 Schmitt trigger) between the oscillator and camera could resolve this if the module is not damaged.

### Recommended Next Steps

1. **Add a clock buffer** (74LVC1G17 or similar Schmitt trigger) to clean up and amplify the 25 MHz signal to full VDDIO swing. This is the lowest-cost test to rule out clock quality.
2. **If buffered clock doesn't help**: Contact the supplier for warranty replacement. The module is likely damaged.
3. **For replacement module**: Handle with proper ESD precautions (wrist strap, anti-static mat, grounded workspace).
