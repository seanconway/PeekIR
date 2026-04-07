"""
Pi Camera Service (FastAPI)

Run:
  python3 -m uvicorn pi_camera_server:app --host 0.0.0.0 --port 9000

Endpoints:
  GET /api/health
  GET /api/camera/frame
  GET /api/camera/stream
  GET /api/ir/frame
  GET /api/ir/stream

Notes:
- Captures frames in a background thread (~15 FPS).
- Stores latest JPEG bytes in memory (thread-safe).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse

try:
    import cv2
except Exception as e:  # pragma: no cover - runtime dependency
    cv2 = None
    _CV2_IMPORT_ERROR = e
else:
    _CV2_IMPORT_ERROR = None

try:
    import numpy as np
except Exception:
    np = None

try:
    from picamera2 import Picamera2
except Exception as e:  # pragma: no cover - runtime dependency
    Picamera2 = None
    _PICAMERA_IMPORT_ERROR = e
else:
    _PICAMERA_IMPORT_ERROR = None

app = FastAPI(title="Pi Camera Service", version="0.1.0")

# Settings
FPS = 15.0
JPEG_QUALITY = 80
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
IR_CAMERA_DEVICE = "/dev/video0"
IR_FRAME_WIDTH = 160
IR_FRAME_HEIGHT = 120
IR_FPS = 9.0

# Shared state
_latest_rgb_jpeg: Optional[bytes] = None
_latest_ir_jpeg: Optional[bytes] = None
_rgb_lock = threading.Lock()
_ir_lock = threading.Lock()
_stop_event = threading.Event()
_rgb_thread: Optional[threading.Thread] = None
_ir_thread: Optional[threading.Thread] = None
_rgb_camera: Optional[Picamera2] = None
_ir_camera = None


def _init_camera() -> Picamera2:
    if Picamera2 is None:
        raise RuntimeError(f"picamera2 unavailable: {_PICAMERA_IMPORT_ERROR}")

    cam = Picamera2()
    config = cam.create_preview_configuration(
        main={"format": "RGB888", "size": (FRAME_WIDTH, FRAME_HEIGHT)}
    )
    cam.configure(config)
    cam.start()
    time.sleep(0.2)
    return cam


def _init_ir_camera():
    if cv2 is None:
        raise RuntimeError(f"opencv unavailable: {_CV2_IMPORT_ERROR}")

    cap = cv2.VideoCapture(IR_CAMERA_DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(IR_CAMERA_DEVICE)
    if not cap.isOpened():
        raise RuntimeError(f"unable to open IR camera device {IR_CAMERA_DEVICE}")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, IR_FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, IR_FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, IR_FPS)
    return cap


def _encode_rgb_jpeg(frame) -> bytes:
    if cv2 is None:
        raise RuntimeError("OpenCV not available for JPEG encoding.")
    # frame from Picamera2 is RGB; OpenCV expects BGR
    bgr = frame[..., ::-1]
    ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("Failed to encode JPEG")
    return encoded.tobytes()


def _encode_ir_jpeg(frame) -> bytes:
    if cv2 is None:
        raise RuntimeError("OpenCV not available for JPEG encoding.")
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("Failed to encode JPEG")
    return encoded.tobytes()


def _rgb_capture_loop():
    global _latest_rgb_jpeg
    interval = 1.0 / FPS
    while not _stop_event.is_set():
        try:
            frame = _rgb_camera.capture_array()
            jpg = _encode_rgb_jpeg(frame)
            with _rgb_lock:
                _latest_rgb_jpeg = jpg
        except Exception:
            # On capture error, sleep briefly and retry
            time.sleep(0.1)
        time.sleep(interval)


def _ir_capture_loop():
    global _latest_ir_jpeg
    interval = 1.0 / IR_FPS if IR_FPS > 0 else 0.1
    while not _stop_event.is_set():
        try:
            ok, frame = _ir_camera.read()
            if not ok or frame is None:
                time.sleep(0.1)
                continue
            jpg = _encode_ir_jpeg(frame)
            with _ir_lock:
                _latest_ir_jpeg = jpg
        except Exception:
            time.sleep(0.1)
        time.sleep(interval)


@app.on_event("startup")
def _startup() -> None:
    global _rgb_camera, _rgb_thread, _ir_camera, _ir_thread
    _stop_event.clear()

    if _PICAMERA_IMPORT_ERROR is None:
        try:
            _rgb_camera = _init_camera()
            _rgb_thread = threading.Thread(target=_rgb_capture_loop, daemon=True)
            _rgb_thread.start()
        except Exception:
            _rgb_camera = None
            _rgb_thread = None

    if cv2 is not None:
        try:
            _ir_camera = _init_ir_camera()
            _ir_thread = threading.Thread(target=_ir_capture_loop, daemon=True)
            _ir_thread.start()
        except Exception:
            _ir_camera = None
            _ir_thread = None


@app.on_event("shutdown")
def _shutdown() -> None:
    _stop_event.set()
    if _rgb_thread and _rgb_thread.is_alive():
        _rgb_thread.join(timeout=2)
    if _ir_thread and _ir_thread.is_alive():
        _ir_thread.join(timeout=2)
    if _rgb_camera is not None:
        try:
            _rgb_camera.stop()
        except Exception:
            pass
    if _ir_camera is not None:
        try:
            _ir_camera.release()
        except Exception:
            pass


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "rgb_available": _rgb_camera is not None,
        "ir_available": _ir_camera is not None,
    }


@app.get("/api/camera/frame")
def camera_frame():
    if _rgb_camera is None:
        raise HTTPException(status_code=503, detail=f"RGB camera unavailable: {_PICAMERA_IMPORT_ERROR}")

    with _rgb_lock:
        jpg = _latest_rgb_jpeg
    if jpg is None:
        raise HTTPException(status_code=503, detail="No RGB frame available yet")
    return Response(content=jpg, media_type="image/jpeg")


@app.get("/api/camera/stream")
def camera_stream():
    if _rgb_camera is None:
        raise HTTPException(status_code=503, detail=f"RGB camera unavailable: {_PICAMERA_IMPORT_ERROR}")

    boundary = b"--frame\r\n"

    def _gen():
        while True:
            with _rgb_lock:
                jpg = _latest_rgb_jpeg
            if jpg is None:
                time.sleep(0.05)
                continue
            yield boundary
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
            yield jpg
            yield b"\r\n"
            time.sleep(1.0 / FPS)

    return StreamingResponse(
        _gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/ir/frame")
def ir_frame():
    if _ir_camera is None:
        raise HTTPException(status_code=503, detail="IR camera unavailable")

    with _ir_lock:
        jpg = _latest_ir_jpeg
    if jpg is None:
        raise HTTPException(status_code=503, detail="No IR frame available yet")
    return Response(content=jpg, media_type="image/jpeg")


@app.get("/api/ir/stream")
def ir_stream():
    if _ir_camera is None:
        raise HTTPException(status_code=503, detail="IR camera unavailable")

    boundary = b"--frame\r\n"

    def _gen():
        while True:
            with _ir_lock:
                jpg = _latest_ir_jpeg
            if jpg is None:
                time.sleep(0.05)
                continue
            yield boundary
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
            yield jpg
            yield b"\r\n"
            time.sleep(1.0 / IR_FPS if IR_FPS > 0 else 0.1)

    return StreamingResponse(
        _gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )
