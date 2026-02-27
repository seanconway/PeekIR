-- SAR Capture and Process
-- Configures radar/DCA1000, captures raw .bin files, triggers gantry via Python over SSH,
-- then runs the same Python script locally to process captures and display HTML.
-- Lives in SoftwareDemo/RadarTests. Dumps go to Safehaven-Lua (root_path).

-- =================================================================================
-- CONFIGURATION
-- =================================================================================
-- Where dumps folders (dumps0, dumps1, ...) are created; keep Safehaven-Lua so SAR script finds them
local root_path = "C:\\Users\\sean_\\projects\\PeekIR\\PeekIR\\Safehaven-Lua\\"
local num_y_steps = 40
local frame_periodicity = 18   -- ms
local num_frames = 800
local frame_duration_ms = num_frames * frame_periodicity
local safety_buffer_ms = 1000

-- Gantry: Python script in SoftwareDemo/RadarTests (same script over SSH and locally)
local ssh_host = "peekir@10.247.232.157"
local ssh_exe = "C:\\Windows\\System32\\OpenSSH\\ssh.exe"
local remote_dir = "PeekIR/SoftwareDemo/RadarTests"
local python_script = "capture_and_process.py"
local x_dist_mm = 280
local y_step_mm = 1
local speed_mms = 18
local return_speed_mms = 36
local return_wait_ms = 8500

-- Path to capture_and_process.py for local process step (relative to root_path)
local capture_script = root_path .. "..\\SoftwareDemo\\RadarTests\\capture_and_process.py"
local log_file = ""

-- =================================================================================
-- HELPER: Run Python on Pi over SSH (async)
-- =================================================================================
function RunGantryAsync(args)
    local pwsh_exe = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"
    local args_with_silent = args .. " silent"
    local remote_cmd = string.format("cd %s; sudo .venv/bin/python %s %s", remote_dir, python_script, args_with_silent)
    local remote_shell_cmd = string.format("zsh -l -i -c '%s'", remote_cmd)
    local ssh_cmd_str = string.format("\\\"%s\\\" -t %s \\\"%s\\\"", ssh_exe, ssh_host, remote_shell_cmd)
    local full_cmd = string.format("start \"Gantry\" /min \"%s\" -NoProfile -Command \"%s | Tee-Object -FilePath '%s'\"", pwsh_exe, ssh_cmd_str, log_file)
    if WriteToLog then WriteToLog("Gantry Async: " .. args .. "\n", "black") end
    os.execute(full_cmd)
end

-- =================================================================================
-- HELPER: Run Python on Pi over SSH (blocking) for reset
-- =================================================================================
function RunGantryBlocking(args)
    local pwsh_exe = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"
    local remote_cmd = string.format("cd %s; sudo .venv/bin/python %s %s", remote_dir, python_script, args)
    local remote_shell_cmd = string.format("zsh -l -i -c '%s'", remote_cmd)
    local ssh_cmd_str = string.format("\\\"%s\\\" -t %s \\\"%s\\\"", ssh_exe, ssh_host, remote_shell_cmd)
    local full_cmd = string.format("start \"Gantry\" /wait /min \"%s\" -NoProfile -Command \"%s\"", pwsh_exe, ssh_cmd_str)
    if WriteToLog then WriteToLog("Gantry Blocking: " .. args .. "\n", "black") end
    os.execute(full_cmd)
end

function ResetGantryPosition()
    if WriteToLog then WriteToLog("Resetting Gantry (facetrack wait-and-see, then down/left)...\n", "magenta") end
    RunGantryBlocking("facetrack --wait-and-see")
    RSTD.Sleep(500)
    RunGantryBlocking("down=500mm left=150mm")
    if WriteToLog then WriteToLog("Reset Complete.\n", "green") end
    RSTD.Sleep(1000)
end

-- =================================================================================
-- INITIALIZATION (Radar + DCA1000)
-- =================================================================================
if WriteToLog then WriteToLog("SAR Capture and Process: Configuring radar and DCA1000...\n", "blue") end

ar1.StopFrame()
ar1.CaptureCardConfig_StopRecord()
RSTD.Sleep(20)

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

ar1.SelectCaptureDevice("DCA1000")
ar1.CaptureCardConfig_EthInit("192.168.33.30", "192.168.33.180", "12:34:56:78:90:12", 4096, 4098)
ar1.CaptureCardConfig_Mode(1, 2, 1, 2, 3, 30)
ar1.CaptureCardConfig_PacketDelay(25)

-- =================================================================================
-- ONE SCAN: create folder, capture loop, then process
-- =================================================================================
ResetGantryPosition()

-- Next dumps folder
local max_index = -1
local p = io.popen('dir "' .. root_path .. '" /b /ad')
if p then
    for filename in p:lines() do
        local index_str = string.match(filename, "^dumps(%d+)")
        if index_str then
            local index = tonumber(index_str)
            if index > max_index then max_index = index end
        end
    end
    p:close()
end
local new_index = max_index + 1
local new_folder_name = "dumps" .. new_index
local base_path = root_path .. new_folder_name .. "\\"
os.execute('mkdir "' .. root_path .. new_folder_name .. '"')
if WriteToLog then WriteToLog("Output folder: " .. base_path .. "\n", "blue") end

log_file = base_path .. "gantry_log.txt"

-- Capture loop
for y = 1, num_y_steps do
    local filename = base_path .. "scan" .. y .. ".bin"
    if WriteToLog then
        WriteToLog("Step " .. y .. " / " .. num_y_steps .. " -> " .. filename .. "\n", "blue")
    end

    if (ar1.CaptureCardConfig_StartRecord(filename, 1) == 0) then
        if WriteToLog then WriteToLog("DCA1000 Armed\n", "green") end
    else
        if WriteToLog then WriteToLog("DCA1000 Arm Failed\n", "red") end
        break
    end
    RSTD.Sleep(0)

    -- Trigger gantry (Python over SSH)
    local move_args = string.format("right=%dmm speed=%dmms", x_dist_mm, speed_mms)
    RunGantryAsync(move_args)

    -- Wait for motor start
    local poll_count = 0
    local motor_started = false
    while poll_count < 100 and not motor_started do
        local f = io.open(log_file, "r")
        if f then
            local content = f:read("*all")
            f:close()
            if string.find(content, "MOTOR_STARTED") then motor_started = true end
        end
        if not motor_started then RSTD.Sleep(10); poll_count = poll_count + 1 end
    end
    if not motor_started and WriteToLog then WriteToLog("Motor start timeout, continuing\n", "red") end
    if motor_started then RSTD.Sleep(500) end

    if (ar1.StartFrame() == 0) then
        if WriteToLog then WriteToLog("Frame Started\n", "green") end
    else
        if WriteToLog then WriteToLog("StartFrame Failed\n", "red") end
        break
    end

    RSTD.Sleep(frame_duration_ms + safety_buffer_ms)
    if WriteToLog then WriteToLog("Capture step done\n", "green") end

    -- Return
    RunGantryAsync(string.format("left=%dmm speed=%dmms", x_dist_mm, return_speed_mms))
    RSTD.Sleep(return_wait_ms)

    if y < num_y_steps then
        RunGantryAsync(string.format("up=%dmm speed=%dmms", y_step_mm, speed_mms))
    end
end

if WriteToLog then WriteToLog("Capture finished: " .. new_folder_name .. "\n", "blue") end

-- Run Python locally (RadarTests script); cwd = root_path so --folder dumpsN resolves
local python_cmd = string.format("cd /d \"%s\" && python \"%s\" --process --folder \"%s\" --plotly", root_path, capture_script, new_folder_name)
if WriteToLog then WriteToLog("Running process: " .. python_cmd .. "\n", "magenta") end
os.execute(python_cmd)
if WriteToLog then WriteToLog("Process (HTML) complete.\n", "green") end
