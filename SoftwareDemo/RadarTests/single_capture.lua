-- Single DCA1000 capture (no gantry).
-- Configures IWR1443 + DCA1000, arms one file, starts one frame, waits.
-- Run from TI mmWave Studio with radar API connected.
-- Then view the .bin file with: python view_capture.py --file <path>

-- =================================================================================
-- CONFIGURATION – set output path to where you want the .bin file
-- =================================================================================
local output_dir = "C:\\Users\\sean_\\projects\\PeekIR\\PeekIR\\SoftwareDemo\\RadarTests\\captures\\"
local output_file = output_dir .. "capture.bin"

local frame_periodicity = 18   -- ms
local num_frames = 800
local frame_duration_ms = num_frames * frame_periodicity
local safety_buffer_ms = 2000

-- =================================================================================
-- RADAR + DCA1000 SETUP
-- =================================================================================
if WriteToLog then WriteToLog("Single DCA1000 capture: configuring...\n", "blue") end

ar1.StopFrame()
ar1.CaptureCardConfig_StopRecord()
RSTD.Sleep(100)

-- Sensor config (match your IWR1443 / existing config)
if (ar1.ProfileConfig(0, 77, 7, 6, 63, 0, 0, 0, 0, 0, 0, 63.343, 0, 512, 9121, 0, 0, 30) == 0) then
    if WriteToLog then WriteToLog("ProfileConfig OK\n", "green") end
else
    if WriteToLog then WriteToLog("ProfileConfig Failed\n", "red") end
end
if (ar1.ChirpConfig(0, 0, 0, 0, 0, 0, 0, 1, 0, 0) == 0) then
    if WriteToLog then WriteToLog("ChirpConfig OK\n", "green") end
end
if (ar1.FrameConfig(0, 0, num_frames, 1, frame_periodicity, 0, 512, 1) == 0) then
    if WriteToLog then WriteToLog("FrameConfig OK\n", "green") end
else
    if WriteToLog then WriteToLog("FrameConfig Failed\n", "red") end
end

-- DCA1000 (adjust IPs if your setup differs)
ar1.SelectCaptureDevice("DCA1000")
ar1.CaptureCardConfig_EthInit("192.168.33.30", "192.168.33.180", "12:34:56:78:90:12", 4096, 4098)
ar1.CaptureCardConfig_Mode(1, 2, 1, 2, 3, 30)
ar1.CaptureCardConfig_PacketDelay(25)

-- =================================================================================
-- ONE RECORD
-- =================================================================================
-- Create output directory (Windows)
os.execute('mkdir "' .. output_dir .. '" 2>nul')

if WriteToLog then WriteToLog("Arming DCA1000: " .. output_file .. "\n", "blue") end
if (ar1.CaptureCardConfig_StartRecord(output_file, 1) == 0) then
    if WriteToLog then WriteToLog("Armed OK\n", "green") end
else
    if WriteToLog then WriteToLog("Arm failed\n", "red") end
end
RSTD.Sleep(100)

if WriteToLog then WriteToLog("Starting frame...\n", "blue") end
if (ar1.StartFrame() == 0) then
    if WriteToLog then WriteToLog("Frame started\n", "green") end
else
    if WriteToLog then WriteToLog("StartFrame failed\n", "red") end
end

local wait_ms = frame_duration_ms + safety_buffer_ms
if WriteToLog then WriteToLog("Waiting " .. wait_ms .. " ms for capture...\n", "black") end
RSTD.Sleep(wait_ms)

if WriteToLog then WriteToLog("Capture done. File: " .. output_file .. "\n", "green") end
if WriteToLog then WriteToLog("View with: python view_capture.py --file \"" .. output_file .. "\"\n", "black") end
