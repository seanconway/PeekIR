#!/usr/bin/env python3
"""
Single script used in two ways:
1. Over SSH (from Lua): run with gantry args (e.g. right=280mm speed=18mms) -> forwards to motorTest_rev13.py.
2. Locally (from Lua after capture): run with --process --folder dumpsN --plotly -> runs SAR and writes HTML.

Lives in SoftwareDemo/RadarTests. On the Pi, Lua runs: cd PeekIR/SoftwareDemo/RadarTests; sudo .venv/bin/python capture_and_process.py <motor args>
"""
import os
import sys
import subprocess
import shutil
import glob
import re


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _motor_script_path():
    """From SoftwareDemo/RadarTests -> SoftwareDemo/GantryFunctionality/MotorTest/motorTest_rev13.py"""
    return os.path.join(
        _script_dir(),
        "..",
        "GantryFunctionality",
        "MotorTest",
        "motorTest_rev13.py",
    )


def _safehaven_lua_dir():
    """From SoftwareDemo/RadarTests -> Safehaven-Lua (repo root then Safehaven-Lua)."""
    return os.path.join(_script_dir(), "..", "..", "Safehaven-Lua")


def _normalize_dump_folder(folder: str) -> str:
    """If folder has scan1.bin, scan2.bin ... but mainSAR expects scan1_Raw_0.bin,
    create scanY_Raw_0.bin by copying scanY.bin (so we don't mutate originals)."""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return folder
    raw0 = os.path.join(folder, "scan1_Raw_0.bin")
    if os.path.isfile(raw0):
        return folder
    pattern = os.path.join(folder, "scan*.bin")
    files = glob.glob(pattern)
    for path in files:
        base = os.path.basename(path)
        m = re.match(r"scan(\d+)\.bin$", base)
        if m:
            y = m.group(1)
            dest = os.path.join(folder, f"scan{y}_Raw_0.bin")
            if not os.path.isfile(dest):
                shutil.copy2(path, dest)
    return folder


def run_process(folder: str, plotly: bool = True, extra_args: list = None) -> int:
    """Run mainSARneuronauts2py_rev3_2.py on folder and generate HTML."""
    folder = _normalize_dump_folder(folder)
    safehaven = _safehaven_lua_dir()
    sar_script = os.path.join(safehaven, "mainSARneuronauts2py_rev3_2.py")
    if not os.path.isfile(sar_script):
        print(f"Error: SAR script not found: {sar_script}", file=sys.stderr)
        return 1
    cmd = [sys.executable, sar_script, "--folder", folder, "--frames_in_x", "800", "--frames_in_y", "40"]
    if plotly:
        cmd.append("--plotly")
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, cwd=safehaven).returncode


def run_motor() -> int:
    """Forward all args to motorTest_rev13.py (for gantry control on Pi)."""
    motor_script = _motor_script_path()
    if not os.path.isfile(motor_script):
        print(f"Error: Motor script not found: {motor_script}", file=sys.stderr)
        return 1
    cmd = [sys.executable, motor_script] + sys.argv[1:]
    return subprocess.run(cmd, cwd=os.path.dirname(motor_script)).returncode


def main():
    argv = sys.argv[1:]
    process_mode = "--process" in argv
    if process_mode:
        args = [a for a in argv if a != "--process"]
        folder = "dumps"
        plotly = True
        extra = []
        i = 0
        while i < len(args):
            if args[i] == "--folder" and i + 1 < len(args):
                folder = args[i + 1]
                i += 2
                continue
            if args[i] == "--plotly":
                plotly = True
                i += 1
                continue
            if args[i] == "--no-plotly" or args[i] == "--mat_plot_lib":
                plotly = False
                i += 1
                continue
            extra.append(args[i])
            i += 1
        return run_process(folder, plotly=plotly, extra_args=extra if extra else None)
    return run_motor()


if __name__ == "__main__":
    sys.exit(main())
