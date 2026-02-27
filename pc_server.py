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
  POST /api/poi/match       (multipart/form-data, field name: file)
  POST /api/poi/match-base64 (JSON payload)
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import numpy as np
import cv2
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

def _cors_allowlist() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["*"]

REPO_ROOT = Path(__file__).resolve().parent
CAPSTONE_BACKEND = REPO_ROOT / "CapstoneProject" / "backend"

POI_DB_PATH = CAPSTONE_BACKEND / "data" / "poi_db" / "poi_embeddings.json"
POI_DIR = CAPSTONE_BACKEND / "data" / "faces" / "poi"
POI_MODEL_NAME = "ArcFace"
POI_DETECTOR_BACKEND = "retinaface"
POI_DISTANCE_METRIC = "cosine"
POI_DEFAULT_THRESHOLD = 0.68
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CAPSTONE_ROOT = REPO_ROOT / "CapstoneProject"
if CAPSTONE_ROOT.exists():
    sys.path.insert(0, str(CAPSTONE_ROOT))

from backend.scripts.personDetect import (  # type: ignore  # noqa: E402
    detect_persons,
    DEFAULT_PERSON_WEIGHTS_PATH,
    CONF_THRESHOLD as DEFAULT_PERSON_CONF,
)


class POIMatchBase64Request(BaseModel):
    image_base64: str = Field(..., description="Base64 or data URL for the captured suspect image.")
    filename: str | None = Field(default=None, description="Optional filename hint for the uploaded image.")

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
        # Try repo root first
        repo_candidate = (REPO_ROOT / candidate).resolve()
        if repo_candidate.exists():
            candidate = repo_candidate
        else:
            # Try CapstoneProject backend relative paths
            cap_backend_candidate = (CAPSTONE_BACKEND / candidate).resolve()
            if cap_backend_candidate.exists():
                candidate = cap_backend_candidate
            else:
                cap_candidate = (REPO_ROOT / "CapstoneProject" / candidate).resolve()
                candidate = cap_candidate
    else:
        candidate = candidate.resolve()

    repo = REPO_ROOT.resolve()
    try:
        candidate.relative_to(repo)
    except Exception:
        raise HTTPException(status_code=400, detail="Path must be within the project folder.")
    return candidate


def _normalize_repo_rel_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    raw = Path(path_value)
    if raw.is_absolute():
        try:
            return raw.resolve().relative_to(REPO_ROOT).as_posix()
        except Exception:
            return raw.as_posix()

    # Try to resolve against repo root and CapstoneProject locations.
    for base in (REPO_ROOT, CAPSTONE_BACKEND, REPO_ROOT / "CapstoneProject"):
        candidate = (base / raw).resolve()
        if candidate.exists():
            try:
                return candidate.relative_to(REPO_ROOT).as_posix()
            except Exception:
                return candidate.as_posix()

    return raw.as_posix()


def _image_suffix_from_mime(mime: str | None) -> str | None:
    if not mime:
        return None
    m = mime.lower()
    if m in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if m == "image/png":
        return ".png"
    if m == "image/webp":
        return ".webp"
    if m == "image/bmp":
        return ".bmp"
    return None


def _decode_image_base64(payload: str) -> tuple[bytes, str | None]:
    if not payload:
        raise HTTPException(status_code=400, detail="Empty base64 payload.")

    mime = None
    data = payload.strip()
    if data.startswith("data:"):
        header, _, rest = data.partition(",")
        if not rest:
            raise HTTPException(status_code=400, detail="Invalid data URL payload.")
        data = rest
        mime = header[5:].split(";")[0] if ";" in header else header[5:]

    try:
        decoded = base64.b64decode(data, validate=True)
    except Exception:
        try:
            decoded = base64.b64decode(data)
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid base64 image data.") from e

    if not decoded:
        raise HTTPException(status_code=400, detail="Empty decoded image data.")
    return decoded, mime


def _cosine_distance(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    denom = (np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 1.0
    return float(1.0 - np.dot(va, vb) / denom)


def _iter_images(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )


def _pick_face_representation(reps: Any) -> dict:
    if isinstance(reps, list):
        if not reps:
            raise ValueError("DeepFace.represent() returned an empty list.")
        if len(reps) == 1:
            return reps[0]

        def area(rep: dict) -> float:
            fa = rep.get("facial_area") or {}
            w = fa.get("w") or fa.get("width") or 0
            h = fa.get("h") or fa.get("height") or 0
            return float(w) * float(h)

        return max(reps, key=area)

    if isinstance(reps, dict):
        return reps

    raise TypeError(f"Unexpected represent() return type: {type(reps).__name__}")


def _poi_threshold() -> float:
    try:
        from deepface.commons import distance as dist

        if hasattr(dist, "find_threshold"):
            return float(dist.find_threshold(POI_MODEL_NAME, POI_DISTANCE_METRIC))
        if hasattr(dist, "findThreshold"):
            return float(dist.findThreshold(POI_MODEL_NAME, POI_DISTANCE_METRIC))
    except Exception:
        pass
    return POI_DEFAULT_THRESHOLD


def _load_or_build_poi_db(enforce_detection: bool = False) -> dict[str, Any]:
    if POI_DB_PATH.exists():
        try:
            return json.loads(POI_DB_PATH.read_text())
        except Exception:
            pass

    try:
        from deepface import DeepFace
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DeepFace unavailable: {e}")

    images = _iter_images(POI_DIR)
    if not images:
        raise HTTPException(status_code=400, detail=f"No POI images found under: {POI_DIR}")

    created_at = datetime.now(timezone.utc).isoformat()
    entries: list[dict[str, Any]] = []
    skipped = 0

    for img_path in images:
        try:
            reps = DeepFace.represent(
                img_path=str(img_path),
                model_name=POI_MODEL_NAME,
                detector_backend=POI_DETECTOR_BACKEND,
                enforce_detection=enforce_detection,
            )
            rep = _pick_face_representation(reps)
            embedding = rep.get("embedding")
            if embedding is None:
                raise KeyError("Missing 'embedding' in DeepFace representation.")

            rel = _normalize_repo_rel_path(img_path.relative_to(REPO_ROOT).as_posix())
            entries.append(
                {
                    "name": img_path.stem,
                    "image_path": rel,
                    "embedding": embedding,
                }
            )
        except Exception:
            skipped += 1

    payload = {
        "schema_version": 1,
        "model_name": POI_MODEL_NAME,
        "detector_backend": POI_DETECTOR_BACKEND,
        "created_at_utc": created_at,
        "count": len(entries),
        "skipped": skipped,
        "entries": entries,
    }
    POI_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    POI_DB_PATH.write_text(json.dumps(payload, indent=2))
    return payload


def _run_poi_match_bytes(data: bytes, suffix: str) -> dict[str, Any]:
    try:
        from deepface import DeepFace
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DeepFace unavailable: {e}")

    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        tmp_path.write_bytes(data)

        db = _load_or_build_poi_db(enforce_detection=False)
        entries = db.get("entries") or []
        if not entries:
            raise HTTPException(status_code=400, detail="POI database is empty.")

        reps = DeepFace.represent(
            img_path=str(tmp_path),
            model_name=POI_MODEL_NAME,
            detector_backend=POI_DETECTOR_BACKEND,
            enforce_detection=False,
        )
        rep = _pick_face_representation(reps)
        suspect_emb = rep.get("embedding")
        if suspect_emb is None:
            raise HTTPException(status_code=400, detail="No face embedding could be computed for this image.")

        best = None
        for entry in entries:
            emb = entry.get("embedding")
            if not emb:
                continue
            dist = _cosine_distance(suspect_emb, emb)
            if best is None or dist < best["distance"]:
                best = {
                    "name": entry.get("name"),
                    "image_path": entry.get("image_path"),
                    "distance": dist,
                }

        if best is None:
            raise HTTPException(status_code=400, detail="No valid embeddings in POI database.")

        threshold = _poi_threshold()
        match = float(best["distance"]) <= float(threshold)

        return {
            "match": bool(match),
            "distance": float(best["distance"]),
            "threshold": float(threshold),
            "model_name": POI_MODEL_NAME,
            "detector_backend": POI_DETECTOR_BACKEND,
            "distance_metric": POI_DISTANCE_METRIC,
            "poi_name": best.get("name"),
            "poi_image_path": _normalize_repo_rel_path(best.get("image_path")),
        }
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


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


@app.post("/api/poi/match")
async def poi_match(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "suspect.jpg").suffix or ".jpg"
    data = await file.read()
    return _run_poi_match_bytes(data, suffix)


@app.post("/api/poi/match-base64")
async def poi_match_base64(req: POIMatchBase64Request) -> dict[str, Any]:
    data, mime = _decode_image_base64(req.image_base64)
    filename = req.filename or "capture"
    suffix = Path(filename).suffix or _image_suffix_from_mime(mime) or ".jpg"
    return _run_poi_match_bytes(data, suffix)
