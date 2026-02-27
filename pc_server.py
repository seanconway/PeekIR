"""
PC Backend Server (FastAPI)

Run:
  python3 -m uvicorn pc_server:app --host 0.0.0.0 --port 8000

Env:
  PI_CAMERA_BASE_URL=http://<pi-ip>:9000

Endpoints:
  GET /api/health
  GET /api/camera/frame
  GET /api/camera/stream
  POST /api/detect-person  (multipart/form-data, field name: file)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import cv2
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

def _cors_allowlist() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["*"]

REPO_ROOT = Path(__file__).resolve().parent
CAPSTONE_ROOT = REPO_ROOT / "CapstoneProject"
if CAPSTONE_ROOT.exists():
    sys.path.insert(0, str(CAPSTONE_ROOT))

from backend.scripts.personDetect import (  # type: ignore  # noqa: E402
    detect_persons,
    DEFAULT_PERSON_WEIGHTS_PATH,
    CONF_THRESHOLD as DEFAULT_PERSON_CONF,
)

app = FastAPI(title="PC Backend Server", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowlist(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _pi_base_url() -> str:
    base = os.getenv("PI_CAMERA_BASE_URL", "").strip()
    if not base:
        raise HTTPException(status_code=500, detail="PI_CAMERA_BASE_URL not set")
    return base.rstrip("/")


def _resolve_user_path(path_value: str) -> Path:
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()

    repo = REPO_ROOT.resolve()
    try:
        candidate.relative_to(repo)
    except Exception:
        raise HTTPException(status_code=400, detail="Path must be within the project folder.")
    return candidate


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/camera/frame")
def camera_frame():
    base = _pi_base_url()
    url = f"{base}/api/camera/frame"
    try:
        r = httpx.get(url, timeout=httpx.Timeout(5.0, connect=3.0))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Pi unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Pi error: {r.status_code}")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
    )


@app.get("/api/camera/stream")
async def camera_stream():
    base = _pi_base_url()
    url = f"{base}/api/camera/stream"

    timeout = httpx.Timeout(connect=3.0, read=None)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        stream = await client.stream("GET", url)
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Pi unreachable: {e}")

    if stream.status_code != 200:
        await stream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Pi error: {stream.status_code}")

    content_type = stream.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame")

    async def _gen():
        try:
            async for chunk in stream.aiter_bytes():
                yield chunk
        finally:
            await stream.aclose()
            await client.aclose()

    return StreamingResponse(
        _gen(),
        media_type=content_type,
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
    )


@app.get("/api/images")
def list_images(scope: str = "repo", limit: int = 200):
    # Minimal stub to keep frontend happy.
    return {"scope": scope, "count": 0, "items": []}


@app.get("/api/image")
def get_image(path: str):
    resolved = _resolve_user_path(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(resolved)


@app.get("/api/logs")
def logs(tail: int = 200):
    return {"lines": []}


@app.post("/api/detect-person")
async def detect_person(file: UploadFile = File(...), conf_threshold: float = DEFAULT_PERSON_CONF):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")

    # Decode using OpenCV (as required)
    image_array = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Invalid image data")

    # Run existing detection logic (expects a path) by writing temp file
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(data)

        _has_person, detections, _ = detect_persons(
            tmp_path,
            weights_path=DEFAULT_PERSON_WEIGHTS_PATH,
            conf_threshold=conf_threshold,
        )

        payload = [
            {
                "bbox": d.xyxy,
                "confidence": d.confidence,
            }
            for d in detections
        ]
        return payload
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
