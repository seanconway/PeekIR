-- ============================================================================
-- Test 1: Radar Configuration Test (Post-GUI Init)
-- ============================================================================
-- PREREQUISITES (do these FIRST in mmWave Studio GUI):
--   1. Connect device via GUI (Device menu or RadarAPI tab)
--   2. Configure channels via StaticConfig tab
--   3. Complete basic setup via DataConfig and SensorConfig tabs
--
-- This script tests Profile/Chirp/Frame configuration (like inherited script)
-- ============================================================================

WriteToLog("\n=== Test 1: Radar Configuration (Post-GUI Init) ===\n", "blue")
WriteToLog("NOTE: Assumes radar is already connected via mmWave Studio GUI\n", "magenta")
WriteToLog("If not connected, this test will fail!\n", "magenta")

-- Configuration Parameters
local num_frames = 800
local frame_periodicity = 18  -- ms

-- Step 1: Stop any running operations
WriteToLog("\nStep 1: Stopping any existing operations...\n", "black")
ar1.StopFrame()
ar1.CaptureCardConfig_StopRecord()
RSTD.Sleep(20)
WriteToLog("Stop commands sent.\n", "green")

-- Step 2: Profile Configuration
WriteToLog("\nStep 2: Configuring radar profile...\n", "black")
WriteToLog("Parameters: 77 GHz start, 63 MHz sampling, 512 ADC samples\n", "black")
if (ar1.ProfileConfig(0, 77, 7, 6, 63, 0, 0, 0, 0, 0, 0, 63.343, 0, 512, 9121, 0, 0, 30) == 0) then
    WriteToLog("ProfileConfig Success\n", "green")
else
    WriteToLog("ProfileConfig FAILED!\n", "red")
end
RSTD.Sleep(100)

-- Step 3: Chirp Configuration
WriteToLog("\nStep 3: Configuring chirp...\n", "black")
if (ar1.ChirpConfig(0, 0, 0, 0, 0, 0, 0, 1, 0, 0) == 0) then
    WriteToLog("ChirpConfig Success\n", "green")
else
    WriteToLog("ChirpConfig FAILED!\n", "red")
end
RSTD.Sleep(100)

-- Step 4: Frame Configuration
WriteToLog("\nStep 4: Configuring frame...\n", "black")
WriteToLog(string.format("Frames: %d, Periodicity: %dms, Duration: %.1fs\n", 
           num_frames, frame_periodicity, (num_frames * frame_periodicity) / 1000), "black")
if (ar1.FrameConfig(0, 0, num_frames, 1, frame_periodicity, 0, 512, 1) == 0) then
    WriteToLog("FrameConfig Success\n", "green")
else
    WriteToLog("FrameConfig FAILED!\n", "red")
end
RSTD.Sleep(100)

-- Step 5: DCA1000 Configuration
WriteToLog("\nStep 5: Configuring DCA1000 capture card...\n", "black")
ar1.SelectCaptureDevice("DCA1000")
ar1.CaptureCardConfig_EthInit("192.168.33.30", "192.168.33.180", "12:34:56:78:90:12", 4096, 4098)
ar1.CaptureCardConfig_Mode(1, 2, 1, 2, 3, 30)
ar1.CaptureCardConfig_PacketDelay(25)
WriteToLog("DCA1000 configured.\n", "green")
WriteToLog("IPs: DCA=192.168.33.30, Host=192.168.33.180\n", "black")

WriteToLog("\n=== Test 1 Complete ===\n", "blue")
WriteToLog("✓ All configuration steps completed\n", "green")
WriteToLog("Next: Run Test 2 (single stationary capture)\n", "black")