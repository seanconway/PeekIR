# Agents in the SafeHaven Codebase

This document describes the logical "agents" in the SafeHaven repository — the pieces of code that act as independent responsibilities or services — along with their responsibilities, inputs/outputs, error modes, where to find the code, and how to run them. It is intended to help contributors reason about interactions and implement tests, monitoring, or deployment around each agent.

Notes & assumptions
- This is a best-effort overview based on the repository layout and common usage. If an agent's exact runtime or interface differs in your environment, please update this file.
- Use `uv` to manage Python dependencies (`uv sync` in the project root and in `SoftwareDemo/` for demo-specific deps).

## Quick uv setup

1. Install uv (if not installed):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Install project dependencies from `pyproject.toml`:

```bash
uv sync
```

3. For the `SoftwareDemo` subproject, run:

```bash
cd SoftwareDemo
uv sync
```


## High-level agents

### 1) Camera / Detection Agent
Purpose: Capture frames from the camera (PiCamera) and run object detection (YOLO) to identify humans and bounding boxes.

- Responsibilities:
  - Acquire frames (PiCamera or other camera input).
  - Run YOLO (Ultralytics / model files like `yolov5s.pt`, `yolov8n.pt`).
  - Output detection bounding boxes and confidence scores.

- Inputs: camera stream or image frames.
- Outputs: bounding boxes, class labels, timestamps (written to stdout, logs, or passed to orchestrator).
- Typical files:
  - `SoftwareDemo/PiCamera/HeadlessPersonTracker.py`
  - `BoundaryBoxDetect/BoundaryBoxDetect.py`

- How to run (example):

```bash
python SoftwareDemo/PiCamera/HeadlessPersonTracker.py
```

- Edge cases / errors:
  - No camera available (hardware disconnected)
  - Frames dropped or very low FPS
  - YOLO model file missing or mismatched CUDA/torch versions

- Success criteria:
  - Emits bounding boxes with a reliable confidence score
  - Maintains acceptable frame rate for the scanning pipeline


### 2) Path Planning (Snake Path) Agent (Deprecated in final revision)
Purpose: Given a bounding box or region to scan, compute a gantry scanning path that covers the area efficiently.

-- Responsibilities:
  - Generate an ordered path of X/Y positions (snake pattern) that covers the bounding geometry.
  - Provide the path format expected by the Gantry / Motor controller.
  - NOTE: In the final automation (see `Safehaven-Lua/sar_scan_rev15.lua`) path generation and simple sequencing are handled by the Lua orchestrator. The standalone `SnakePathAlgorithm/` is kept for experimentation and isn't used in the automated Rev15 flow.

- Inputs: bounding box coordinates, resolution params.
- Outputs: ordered list of XY positions and timing information.
- Typical files:
  - `SnakePathAlgorithm/` (module code)

-- How to run / test:
  - Use the path algorithm script to preview the generated path for a sample bounding box (useful for offline planning and visualization).

- Edge cases:
  - Degenerate bounding box (zero area)
  - Very small or very large boxes -- may need sanity clamping


### 3) Gantry / Motor Controller Agent
Purpose: Move the stepper motors on the 2-axis gantry to the commanded positions and provide status/telemetry.

- Responsibilities:
  - Accept movement commands (absolute/relative) and speed parameters.
  - Manage limit switches, emergency stop, and homing.
  - Provide success/failure acknowledgement back to orchestrator.

- Inputs: movement commands, speeds, step counts.
- Outputs: position feedback, errors, limit switch triggers.
- Typical files:
  - `SoftwareDemo/GantryFunctionality/MotorTest/motorTest_rev13.py`
  - Arduino firmware in `Arduino/` (e.g. `simpleMain.cpp`)

- How to run (example):

```bash
python SoftwareDemo/GantryFunctionality/MotorTest/motorTest_rev13.py --help
```

- Communication:
  - Usually via serial (USB-serial) to the Arduino.

- Edge cases:
  - Motor stall, missed steps, cable disconnects
  - Limit switch failure or false triggers
  - Timeouts or long-running moves


### 4) Automation / Orchestrator Agent (Lua + Orchestrator)
Purpose: Coordinate the scan flow between radar, motors, and data acquisition. The main automation script for mmWave Studio is `Safehaven-Lua/sar_scan_rev15.lua` and it drives the scanning sequence.

- Responsibilities:
  - Configure radar parameters, start/stop scans.
  - Signal the gantry to move and set speed/frames.
  - Manage timing (frame rate, return wait times).

- Inputs: scanning parameters (speed, number of frames, range), motor/controller availability.
- Outputs: radar dumps, commands to the motor script.
- Typical files:
  - `Safehaven-Lua/sar_scan_rev15.lua`
  - `Safehaven-Lua/batch_process_dumps.py`

- How to run:
  - Run the Lua script inside TI mmWave Studio (or manually trigger relevant steps as per your scanner workflow).

- Edge cases:
  - Radar not connected or drivers missing
  - Motor script not responding within expected return wait
 
- Notes:
  - The final automation (rev15) includes the scanning sequencing and path management for the gantry. In other words, `Safehaven-Lua/sar_scan_rev15.lua` performs the pathing and movement sequencing for automated runs so the standalone `SnakePathAlgorithm/` is not required in normal Rev15 workflows.


### 5) SAR Processing Agent
Purpose: Convert raw radar dumps into SAR images across X-Y and Z slices using algorithms like Matched Filter (mf), FISTA, or BPA.

- Responsibilities:
  - Read radar dump files (folder typically `dumps/`) and process them into images.
  - Provide plotting and optional interactive visualization (`--plotly`).

- Inputs: raw dump directory, reconstruction parameters (z-index, algo, dx, frames_in_x etc.).
- Outputs: X-Y, X-Z, Y-Z images, optionally interactive Plotly HTML.
- Typical files:
  - `Safehaven-Lua/mainSARneuronauts2py_rev3_2.py`
  - `Safehaven-Lua/batch_process_dumps.py`

- How to run (example):

```bash
python Safehaven-Lua/mainSARneuronauts2py_rev3_2.py --folder dumps --xyonly
```

- Edge cases:
  - Missing dumps or incomplete files
  - Incorrect frame count (X mismatch) or wrong dx
  - Out-of-memory on very large datasets


### 6) Data Management Agent
Purpose: Store, normalize, and manage datasets (radar dumps, images) and datasets for ML training.

- Responsibilities:
  - Normalize, grayscale, and prepare dumps for ML pipelines.
  - Provide utilities for batch processing and dataset creation.

- Typical files:
  - `Safehaven-Lua/batch_process_dumps.py`
  - `Safehaven-Classification/generate_reflectivity_dataset.py`

- Edge cases:
  - Corrupted files
  - Partial/incomplete dump folders


### 7) Classification / ML Agent
Purpose: Classify objects (e.g., weapons) using trained models over SAR images or camera frames.

- Responsibilities:
  - Load trained models and run inference on images or patches.
  - Emit classification labels and confidences.

- Inputs: images or patches, model weights.
- Outputs: labels + confidences, optional visualization overlays.
- Typical files:
  - `Safehaven-Classification/weapon_classifier.py`

- Edge cases:
  - Missing weights (`weapon_classifier.pth`) or mismatch in PyTorch versions
  - Low confidence / ambiguous classification


## Contracts & example messages
For each agent keep the contract tiny and explicit (inputs, outputs, error modes):

- Camera/Detection Agent:
  - Inputs: image frames
  - Outputs: JSON-like objects {timestamp, [bounding_boxes], model_version}
  - Error: returns an error code/exception on camera failure

- Gantry Agent:
  - Inputs: move commands or path list
  - Outputs: success ACKs or error codes, current_position
  - Error: 'stall', 'limit_switch', 'comm_timeout'

- SAR Processing:
  - Inputs: directory of dumps and parameters (dx, frames)
  - Outputs: images or HTML visualizations
  - Error: 'missing_files', 'bad_format'


## Monitoring, Logs, and Telemetry
- Log to stdout/stderr and use structured logs where helpful (timestamp, level, component).
- Add retries and timeouts for serial/IO operations.
- For critical operations (motor move, radar capture) persist a short-lived progress file or status file that an external monitor can read.


## Testing and Suggested Next Steps
- Add unit tests for the Snake Path generator (happy path + degenerate bounding box).
- Add a small integration test that runs the detection agent on a saved sample image and verifies bounding boxes exist.
- Add a mock Motor Agent that simulates serial responses; use it in CI to test the Orchestrator.
- Add a quick CI job (GitHub Actions) that runs `uv sync` in a test environment and runs lightweight unit tests.


## Troubleshooting
- If a PyTorch wheel is not found, confirm `pyproject.toml` contains a `torch` source index (the project already includes a `tool.uv.index` entry for PyTorch in the root `pyproject.toml`).
- If motors don’t respond: check USB-serial connection and Arduino firmware; run motor scripts with `--debug` where available.
- For radar dumps that fail processing: verify `frames_in_x` matches the frames produced by the automation script (see `sar_scan_rev15.lua` speed / num_frames logic).


## Contact & Ownership
If you modify an agent, update `AGENTS.md` to describe any changed runtime behavior. Add a small changelog entry in the file (date + short note) so future contributors quickly see the evolution.


---

If you'd like, I can:
- Add small unit tests for Snake Path and detection (mocked),
- Add example status JSON message formats to `SoftwareDemo/` for the orchestrator,
- Or create a small `scripts/` directory with helper run/debug scripts and `uv`-aware helpers.

Tell me which of these you'd like me to start next and I'll add it to the todo list.