-- ============================================================================
-- SSH Motor Control POC - Proof of Concept Script
-- ============================================================================
-- Purpose: Test remote motor control via SSH from mmWave Studio Lua
--
-- CRITICAL IMPLEMENTATION NOTES:
--
-- 1. SSH PATH ISSUE (32-bit vs 64-bit):
--    - mmWave Studio is a 32-bit application
--    - When 32-bit apps try to access C:\Windows\System32, Windows 
--      automatically redirects to C:\Windows\SysWOW64 (File System Redirector)
--    - OpenSSH is installed at C:\Windows\System32\OpenSSH\ssh.exe
--    - But 32-bit mmWave Studio cannot see it due to redirection!
--    - Solution: Use Git's SSH at "C:\Program Files\Git\usr\bin\ssh.exe"
--      which is accessible from both 32-bit and 64-bit processes
--
-- 2. BATCH FILE WRAPPER APPROACH:
--    - Direct os.execute() with quoted paths containing spaces fails:
--      os.execute('"C:\\Program Files\\Git\\...\\ssh.exe" args')
--      Result: 'ssh' is not recognized as internal or external command
--    - The complex quoting breaks when cmd.exe parses the command
--    - Solution: Create a temporary .bat file with the SSH command
--    - The batch file handles all quoting internally
--    - os.execute() simply runs the batch file (no quoting needed)
--
-- 3. SUDO REQUIREMENT:
--    - Hardware PWM on Raspberry Pi requires root privileges
--    - Cannot use 'uv run' because default user lacks PWM permissions
--    - Must use: sudo .venv/bin/python motorTest_rev13.py
--    - Assumes passwordless sudo is configured on the Pi
--
-- 4. OUTPUT REDIRECTION:
--    - SSH commands redirect output to C:\temp\motor_test_log.txt
--    - This allows Lua to capture and parse motor script output
--    - Look for "MOTOR_STARTED" string to confirm movement began
-- ============================================================================

local ssh_host = "peekir@10.247.232.157"
local remote_dir = "PeekIR/SoftwareDemo/GantryFunctionality/MotorTest"
local python_script = "motorTest_rev13.py"
local log_file = "C:\\temp\\motor_test_log.txt"
local ssh_exe = "C:\\Program Files\\Git\\usr\\bin\\ssh.exe"  -- Accessible from 32-bit mmWave Studio

-- Ensure log directory exists
os.execute('mkdir "C:\\temp\\" 2>nul')

function TestMotorCommand(args)
    -- Build the remote command that will run on Raspberry Pi
    -- Uses sudo because hardware PWM requires root privileges
    local remote_cmd = string.format(
        "cd %s && sudo .venv/bin/python %s %s",
        remote_dir,
        python_script,
        args
    )
    
    -- BATCH FILE WRAPPER APPROACH:
    -- Create a temporary batch file to execute the SSH command
    -- Why? Because os.execute() cannot properly handle:
    --   os.execute('"C:\\Program Files\\Git\\...\\ssh.exe" args')
    -- The quotes break when parsed by cmd.exe
    -- Instead, we write the command to a .bat file where quoting works normally
    local batch_file = "C:\\temp\\motor_cmd.bat"
    local bat = io.open(batch_file, "w")
    if not bat then
        print("ERROR: Could not create batch file")
        return
    end
    
    bat:write("@echo off\n")
    -- Write SSH command with proper quoting for paths with spaces
    -- Output redirected to log_file for capture and parsing
    bat:write(string.format('"%s" %s "%s" > "%s" 2>&1\n', 
        ssh_exe,          -- Quoted because path has spaces
        ssh_host,         -- user@host
        remote_cmd,       -- Remote command to execute
        log_file))        -- Capture stdout and stderr
    bat:close()
    
    print("Executing: " .. args)
    print("Running SSH command via batch file...")
    -- Execute the batch file (no quoting issues here)
    local result = os.execute(batch_file)
    print("Command result code: " .. tostring(result))
    
    -- Wait for SSH connection, command execution, and file write
    -- 1000ms accounts for network latency + command execution time
    RSTD.Sleep(1000)
    
    -- Read and display the captured output
    local log = io.open(log_file, "r")
    if log then
        local content = log:read("*all")
        log:close()
        if content and content ~= "" then
            print("Motor output:")
            print(content)
            print("---")
            
            -- Check for MOTOR_STARTED string printed by motorTest_rev13.py
            -- This confirms the motor actually began moving
            if string.find(content, "MOTOR_STARTED") then
                print("SUCCESS: Motor command executed!")
            end
        else
            print("Log file is empty!")
        end
    else
        print("ERROR: Could not read log file: " .. log_file)
    end
end

-- ============================================================================
-- TEST SEQUENCE
-- ============================================================================
-- Executes three motor movements to verify SSH control works:
-- 1. Move right 50mm (tests X-axis positive direction)
-- 2. Move left 50mm (returns to start, tests X-axis negative direction)
-- 3. Move up 10mm (tests Y-axis positive direction)
--
-- Each command is blocking (waits for completion) with 3-second delays
-- between movements to allow for visual confirmation
-- ============================================================================

print("=== Motor Control POC ===")
print("Testing motor movement...\n")

print("Test 1: Move right 50mm")
TestMotorCommand("right=50mm speed=18mms")
print("\nWaiting 3 seconds...\n")
RSTD.Sleep(3000)

print("Test 2: Move left 50mm") 
TestMotorCommand("left=50mm speed=18mms")
print("\nWaiting 3 seconds...\n")
RSTD.Sleep(3000)

print("Test 3: Move up 10mm")
TestMotorCommand("up=10mm speed=18mms")

print("\n=== Test Complete ===")
print("Check log file: " .. log_file)