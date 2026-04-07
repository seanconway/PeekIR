"""
Pi Camera Service (FastAPI)

Run:
  python3 -m uvicorn pi_camera_server:app --host 0.0.0.0 --port 9000

Endpoints:
  GET /api/health
  GET /api/camera/frame
  GET /api/camera/stream

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
    import numpy as np
    from picamera2 import Picamera2
except Exception as e:  # pragma: no cover - runtime dependency
    cv2 = None
    np = None
    Picamera2 = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

app = FastAPI(title="Pi Camera Service", version="0.1.0")

# Settings
FPS = 15.0
JPEG_QUALITY = 80
FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# Shared state
_latest_jpeg: Optional[bytes] = None
_frame_lock = threading.Lock()
_stop_event = threading.Event()
_capture_thread: Optional[threading.Thread] = None
_camera: Optional[Picamera2] = None


def _init_camera() -> Picamera2:
    if Picamera2 is None:
        raise RuntimeError(f"picamera2 unavailable: {_IMPORT_ERROR}")

    cam = Picamera2()
    config = cam.create_preview_configuration(
        main={"format": "RGB888", "size": (FRAME_WIDTH, FRAME_HEIGHT)}
    )
    cam.configure(config)
    cam.start()
    time.sleep(0.2)
    return cam


def _encode_jpeg(frame) -> bytes:
    if cv2 is None:
        raise RuntimeError("OpenCV not available for JPEG encoding.")
    # frame from Picamera2 is RGB; OpenCV expects BGR
    bgr = frame[..., ::-1]
    ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("Failed to encode JPEG")
    return encoded.tobytes()


def _capture_loop():
    global _latest_jpeg
    interval = 1.0 / FPS
    while not _stop_event.is_set():
        try:
            frame = _camera.capture_array()
            jpg = _encode_jpeg(frame)
            with _frame_lock:
                _latest_jpeg = jpg
        except Exception:
            # On capture error, sleep briefly and retry
            time.sleep(0.1)
        time.sleep(interval)


@app.on_event("startup")
def _startup() -> None:
    global _camera, _capture_thread
    if _IMPORT_ERROR is not None:
        # Defer failure to request time to keep FastAPI booting for /api/health
        return

    _camera = _init_camera()
    _stop_event.clear()
    _capture_thread = threading.Thread(target=_capture_loop, daemon=True)
    _capture_thread.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    _stop_event.set()
    if _capture_thread and _capture_thread.is_alive():
        _capture_thread.join(timeout=2)
    if _camera is not None:
        try:
            _camera.stop()
        except Exception:
            pass


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/camera/frame")
def camera_frame():
    if _IMPORT_ERROR is not None:
        raise HTTPException(status_code=503, detail=f"Camera unavailable: {_IMPORT_ERROR}")

    with _frame_lock:
        jpg = _latest_jpeg
    if jpg is None:
        raise HTTPException(status_code=503, detail="No frame available yet")
    return Response(content=jpg, media_type="image/jpeg")


@app.get("/api/camera/stream")
def camera_stream():
    if _IMPORT_ERROR is not None:
        raise HTTPException(status_code=503, detail=f"Camera unavailable: {_IMPORT_ERROR}")

    boundary = b"--frame\r\n"

    def _gen():
        while True:
            with _frame_lock:
                jpg = _latest_jpeg
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
