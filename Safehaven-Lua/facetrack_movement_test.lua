-- ============================================================================
-- Face Tracking Movement Test Script (No Radar)
-- ============================================================================
-- Tests face detection + movement path WITHOUT radar data collection
-- Use this to verify camera, face tracking, and motor movement logic
-- ============================================================================

-- =================================================================================
-- CONFIGURATION
-- =================================================================================
local root_path = "C:\\Users\\sean_\\projects\\PeekIR\\PeekIR\\Safehaven-Lua\\"
local num_y_steps = 40

-- SSH & Motor Configuration
local ssh_host = "peekir@10.247.232.157"
local ssh_exe = "C:\\Program Files\\Git\\usr\\bin\\ssh.exe"
local remote_dir = "PeekIR/SoftwareDemo/GantryFunctionality/MotorTest"
local python_script = "motorTest_rev13.py"

-- Movement Parameters
local x_dist_mm = 280
local y_step_mm = 1
local speed_mms = 18
local return_speed_mms = 36

-- Timing (simulating radar scan time)
local simulated_scan_time_ms = 15000  -- Simulate 15 second scan

-- Log file (will be updated per test)
local log_file = ""
local batch_file = "C:\\temp\\motor_cmd.bat"

-- Ensure temp directory exists
os.execute('mkdir "C:\\temp\\" 2>nul')

-- =================================================================================
-- HELPER FUNCTIONS
-- =================================================================================

function ExecuteRemoteCommand(args, async)
    print("[ExecuteRemoteCommand] Building command with args: " .. args)
    
    -- DO NOT add silent flag - it suppresses MOTOR_STARTED signal!
    -- The silent flag redirects stdout/stderr to /dev/null
    
    -- Build remote command
    local remote_cmd = string.format(
        "cd %s && sudo .venv/bin/python %s %s",
        remote_dir,
        python_script,
        args
    )
    
    print("[ExecuteRemoteCommand] Remote command: " .. remote_cmd)
    
    -- Create batch file
    local bat = io.open(batch_file, "w")
    if not bat then
        print("[ERROR] Could not create batch file: " .. batch_file)
        return false
    end
    
    bat:write("@echo off\n")
    if async then
        -- Async: append to log file
        bat:write(string.format('"%s" %s "%s" >> "%s" 2>&1\n', 
            ssh_exe, ssh_host, remote_cmd, log_file))
    else
        -- Blocking: overwrite log file
        bat:write(string.format('"%s" %s "%s" > "%s" 2>&1\n', 
            ssh_exe, ssh_host, remote_cmd, log_file))
    end
    bat:close()
    
    print("[ExecuteRemoteCommand] Executing batch file...")
    local result = os.execute(batch_file)
    print("[ExecuteRemoteCommand] Command result code: " .. tostring(result))
    
    return true
end

function WaitForMotorStart()
    print("[WaitForMotorStart] Polling for MOTOR_STARTED signal...")
    local poll_count = 0
    local max_polls = 200
    local motor_started = false
    
    while poll_count < max_polls and not motor_started do
        local file = io.open(log_file, "r")
        if file then
            local content = file:read("*all")
            file:close()
            if string.find(content, "MOTOR_STARTED") then
                motor_started = true
                print("[WaitForMotorStart] Motor started confirmed!")
                return true
            end
        end
        RSTD.Sleep(50)
        poll_count = poll_count + 1
    end
    
    if not motor_started then
        print("[ERROR] Timeout waiting for motor start!")
        return false
    end
end

function ResetGantryPosition()
    print("\n======================================")
    print("[ResetGantryPosition] Starting face tracking and positioning...")
    print("======================================")
    
    print("[ResetGantryPosition] Step 1: Running facetrack --wait-and-see")
    print("[ResetGantryPosition] This will:")
    print("  - Capture camera frame")
    print("  - Run YOLO face detection")
    print("  - Center gantry on detected face")
    
    -- Execute face tracking (blocking)
    ExecuteRemoteCommand("facetrack --wait-and-see", false)
    print("[ResetGantryPosition] Waiting 2s for face tracking to complete...")
    RSTD.Sleep(2000)
    
    -- Read log to check result
    local log = io.open(log_file, "r")
    if log then
        local content = log:read("*all")
        log:close()
        print("\n[ResetGantryPosition] === Facetrack Output ===")
        print(content)
        print("===========================================\n")
        
        -- Check for success indicators
        if string.find(content, "Face detected") or string.find(content, "Centering") then
            print("[ResetGantryPosition] ✓ Face detection successful!")
        else
            print("[WARNING] Face detection may have failed - check output above")
        end
    else
        print("[ERROR] Could not read log file after facetrack")
    end
    
    print("\n[ResetGantryPosition] Step 2: Moving down 500mm and left 150mm")
    print("[ResetGantryPosition] This positions the scan start point below the face")
    ExecuteRemoteCommand("down=500mm left=150mm", false)
    print("[ResetGantryPosition] Waiting 3s for positioning to complete...")
    RSTD.Sleep(3000)
    
    -- Read log to check result
    log = io.open(log_file, "r")
    if log then
        local content = log:read("*all")
        log:close()
        print("\n[ResetGantryPosition] === Position Movement Output ===")
        print(content)
        print("===============================================\n")
    else
        print("[ERROR] Could not read log file after positioning")
    end
    
    print("[ResetGantryPosition] ✓ Reset complete - ready to scan\n")
end

function CreateTestLogFolder()
    print("\n[CreateTestLogFolder] Creating test output folder...")
    local max_index = -1
    local p = io.popen('dir "' .. root_path .. '" /b /ad')
    if p then
        for filename in p:lines() do
            local index_str = string.match(filename, "^test_movement(%d+)")
            if index_str then
                local index = tonumber(index_str)
                if index > max_index then
                    max_index = index
                end
            end
        end
        p:close()
    end
    
    local new_index = max_index + 1
    local new_folder_name = "test_movement" .. new_index
    local base_path = root_path .. new_folder_name .. "\\"
    
    print("[CreateTestLogFolder] Creating folder: " .. new_folder_name)
    os.execute('mkdir "' .. root_path .. new_folder_name .. '"')
    
    -- Update log file path
    log_file = base_path .. "movement_log.txt"
    print("[CreateTestLogFolder] Log file: " .. log_file)
    print("[CreateTestLogFolder] ✓ Folder created\n")
    
    return base_path, new_folder_name
end

-- =================================================================================
-- MAIN WORKFLOW (NO RADAR)
-- =================================================================================

print("\n")
print("================================================")
print("=== Face Tracking Movement Test ===")
print("================================================")
print("This script tests:")
print("  1. Camera face detection")
print("  2. Initial positioning (facetrack + offset)")
print("  3. Raster scan movement pattern")
print("  4. NO radar data collection")
print("================================================\n")

-- Step 1: Create output folder
local base_path, folder_name = CreateTestLogFolder()

-- Step 2: Reset gantry position with face tracking
ResetGantryPosition()

-- Step 3: Movement test loop (simulating scan pattern)
print("\n======================================")
print("[MovementTest] Starting " .. num_y_steps .. " movement passes...")
print("======================================")

for y = 1, num_y_steps do
    print("\n================================================")
    print("[MovementTest] Pass " .. y .. " of " .. num_y_steps)
    print("================================================")
    
    -- Clear log file for this pass
    local clear = io.open(log_file, "w")
    if clear then
        clear:write("")
        clear:close()
    end
    
    -- Trigger gantry motion right (async)
    local move_args = string.format("right=%dmm speed=%dmms", x_dist_mm, speed_mms)
    print("[MovementTest] Starting right scan: " .. move_args)
    print("[MovementTest] Expected duration: ~" .. math.floor(x_dist_mm / speed_mms) .. " seconds")
    ExecuteRemoteCommand(move_args, true)
    
    -- Wait for motor to start
    if not WaitForMotorStart() then
        print("[ERROR] Motor failed to start, aborting test")
        break
    end
    
    print("[MovementTest] ✓ Motor confirmed, waiting 500ms for stabilization...")
    RSTD.Sleep(500)
    
    -- Simulate radar scan time
    print("[MovementTest] [SIMULATING] Radar would be scanning now...")
    print(string.format("[MovementTest] Waiting %dms (simulated scan time)...", simulated_scan_time_ms))
    RSTD.Sleep(simulated_scan_time_ms)
    print("[MovementTest] ✓ Simulated scan complete")
    
    -- Return left
    print("[MovementTest] Returning left...")
    local return_args = string.format("left=%dmm speed=%dmms", x_dist_mm, return_speed_mms)
    print("[MovementTest] Expected duration: ~" .. math.floor(x_dist_mm / return_speed_mms) .. " seconds")
    ExecuteRemoteCommand(return_args, true)
    
    local return_wait = 8500
    print(string.format("[MovementTest] Waiting %dms for return...", return_wait))
    RSTD.Sleep(return_wait)
    print("[MovementTest] ✓ Return complete")
    
    -- Step up (except on last iteration)
    if y < num_y_steps then
        print("[MovementTest] Stepping up " .. y_step_mm .. "mm...")
        local step_args = string.format("up=%dmm speed=%dmms", y_step_mm, speed_mms)
        ExecuteRemoteCommand(step_args, true)
        RSTD.Sleep(2000)
        print("[MovementTest] ✓ Step up complete")
    end
    
    print("[MovementTest] ✓ Pass " .. y .. " complete")
end

print("\n")
print("================================================")
print("[Complete] Movement test finished!")
print("================================================")
print("Results:")
print("  - Test log folder: " .. folder_name)
print("  - Movement log: " .. log_file)
print("  - Passes completed: " .. num_y_steps)
print("  - Total distance X: " .. (x_dist_mm * num_y_steps) .. "mm")
print("  - Total distance Y: " .. (y_step_mm * num_y_steps) .. "mm")
print("================================================")
print("=== Test Complete ===\n")
