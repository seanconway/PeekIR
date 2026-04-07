from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
import tempfile
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

_GUNDETECT_IMPORT_ERROR: Exception | None = None
_PERSONDETECT_IMPORT_ERROR: Exception | None = None

try:
    from backend.scripts.gunDetect import CONF_THRESHOLD, DEFAULT_WEIGHTS_PATH, detect_weapons
except Exception as e:  # pragma: no cover - optional dependency on Pi
    CONF_THRESHOLD = 0.4
    DEFAULT_WEIGHTS_PATH = BACKEND_ROOT / "models" / "weapon_best.pt"
    detect_weapons = None
    _GUNDETECT_IMPORT_ERROR = e

try:
    from backend.scripts.personDetect import (
        CONF_THRESHOLD as PERSON_CONF_THRESHOLD,
        DEFAULT_PERSON_WEIGHTS_PATH,
        detect_persons,
    )
except Exception as e:  # pragma: no cover - optional dependency on Pi
    PERSON_CONF_THRESHOLD = 0.35
    DEFAULT_PERSON_WEIGHTS_PATH = BACKEND_ROOT / "models" / "yolov8n.pt"
    detect_persons = None
    _PERSONDETECT_IMPORT_ERROR = e
_POI_IMPORT_ERROR: Exception | None = None
try:
    from backend.scripts.build_poi_db import (
        DEFAULT_OUTPUT_JSON as DEFAULT_POI_DB_JSON,
        DEFAULT_POI_DIR,
        DETECTOR_BACKEND as POI_DETECTOR_BACKEND,
        MODEL_NAME as POI_MODEL_NAME,
        iter_images as poi_iter_images,
        pick_face_representation,
    )
except Exception as e:  # pragma: no cover - optional dependency on Pi
    DEFAULT_POI_DIR = BACKEND_ROOT / "data" / "faces" / "poi"
    DEFAULT_POI_DB_JSON = BACKEND_ROOT / "data" / "poi_db" / "poi_embeddings.json"
    POI_MODEL_NAME = "ArcFace"
    POI_DETECTOR_BACKEND = "retinaface"
    poi_iter_images = None
    pick_face_representation = None
    _POI_IMPORT_ERROR = e

LOG_DIR = BACKEND_ROOT / "logs"
LOG_FILE = LOG_DIR / "backend.log"


def _configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("capstone.backend")
    logger.setLevel(logging.INFO)

    if not any(isinstance(h, logging.FileHandler) and Path(getattr(h, "baseFilename", "")).name == LOG_FILE.name for h in logger.handlers):
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    logger.propagate = False
    return logger


logger = _configure_logging()

POI_DISTANCE_METRIC = "cosine"
POI_DEFAULT_THRESHOLD = 0.68


def _result_plot_png_base64(result: Any) -> str | None:
    try:
        annotated = result.plot()
    except Exception:
        return None

    try:
        from PIL import Image

        rgb = annotated[..., ::-1]
        img = Image.fromarray(rgb)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        try:
            import cv2

            ok, encoded = cv2.imencode(".png", annotated)
            if not ok:
                return None
            return base64.b64encode(encoded.tobytes()).decode("ascii")
        except Exception:
            return None


_CAMERA_LOCK = threading.Lock()
_CAMERA_INSTANCE = None
_CAMERA_SIZE: tuple[int, int] | None = None


def _camera_available() -> bool:
    try:
        import picamera2  # noqa: F401

        return True
    except Exception:
        return False


def _ensure_camera(width: int, height: int):
    global _CAMERA_INSTANCE, _CAMERA_SIZE

    if not _camera_available():
        raise HTTPException(status_code=503, detail="Pi camera not available (picamera2 missing).")

    with _CAMERA_LOCK:
        if _CAMERA_INSTANCE is not None and _CAMERA_SIZE == (width, height):
            return _CAMERA_INSTANCE

        if _CAMERA_INSTANCE is not None:
            try:
                _CAMERA_INSTANCE.stop()
            except Exception:
                pass
            _CAMERA_INSTANCE = None

        try:
            from picamera2 import Picamera2
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Pi camera unavailable: {e}")

        cam = Picamera2()
        config = cam.create_preview_configuration(
            main={"format": "RGB888", "size": (width, height)}
        )
        cam.configure(config)
        cam.start()
        time.sleep(0.2)

        _CAMERA_INSTANCE = cam
        _CAMERA_SIZE = (width, height)
        return _CAMERA_INSTANCE


def _capture_camera_frame(width: int, height: int):
    cam = _ensure_camera(width, height)
    with _CAMERA_LOCK:
        frame = cam.capture_array()
    return frame


def _encode_jpeg(frame, quality: int = 85) -> bytes:
    try:
        import cv2

        bgr = frame[..., ::-1]
        ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if ok:
            return encoded.tobytes()
    except Exception:
        pass

    try:
        from PIL import Image

        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=int(quality))
        return buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to encode camera frame: {e}")


def _camera_defaults() -> tuple[int, int, int, float]:
    width = int(os.getenv("CAMERA_WIDTH", "640"))
    height = int(os.getenv("CAMERA_HEIGHT", "480"))
    quality = int(os.getenv("CAMERA_JPEG_QUALITY", "85"))
    fps = float(os.getenv("CAMERA_FPS", "8"))
    return width, height, quality, fps


class DetectPathRequest(BaseModel):
    path: str = Field(..., description="Image path on the machine running the backend server.")
    conf_threshold: float = Field(default=CONF_THRESHOLD, ge=0.0, le=1.0)
    weights_path: str | None = Field(default=None, description="Optional weights path override.")


class POIMatchBase64Request(BaseModel):
    image_base64: str = Field(..., description="Base64 or data URL for the captured suspect image.")
    filename: str | None = Field(default=None, description="Optional filename hint for the uploaded image.")


class DetectPersonPathRequest(BaseModel):
    path: str = Field(..., description="Image path on the machine running the backend server.")
    conf_threshold: float = Field(default=PERSON_CONF_THRESHOLD, ge=0.0, le=1.0)
    weights_path: str | None = Field(default=None, description="Optional weights path override.")

def _resolve_user_path(p: str) -> Path:
    candidate = Path(p)
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()

    # prevent path traversal outside repo root
    repo = REPO_ROOT.resolve()
    try:
        candidate.relative_to(repo)
    except Exception:
        raise HTTPException(status_code=400, detail="Path must be within the project folder.")

    return candidate


def _thumbnail_base64(path: Path, size: int = 96) -> str | None:
    try:
        from PIL import Image

        img = Image.open(path)
        img = img.convert("RGB")
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _cosine_distance(a: list[float], b: list[float]) -> float:
    try:
        import numpy as np

        va = np.asarray(a, dtype=float)
        vb = np.asarray(b, dtype=float)
        denom = (np.linalg.norm(va) * np.linalg.norm(vb))
        if denom == 0:
            return 1.0
        return float(1.0 - np.dot(va, vb) / denom)
    except Exception:
        # Fallback without numpy
        denom = (sum(x * x for x in a) ** 0.5) * (sum(x * x for x in b) ** 0.5)
        if denom == 0:
            return 1.0
        dot = sum(x * y for x, y in zip(a, b))
        return float(1.0 - dot / denom)


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
    if poi_iter_images is None or pick_face_representation is None:
        raise HTTPException(status_code=503, detail=f"POI matching unavailable: {_POI_IMPORT_ERROR}")
    if DEFAULT_POI_DB_JSON.exists():
        try:
            return json.loads(DEFAULT_POI_DB_JSON.read_text())
        except Exception:
            pass

    from deepface import DeepFace
    from datetime import datetime, timezone

    images = poi_iter_images(DEFAULT_POI_DIR)
    if not images:
        raise HTTPException(status_code=400, detail=f"No POI images found under: {DEFAULT_POI_DIR}")

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
            rep = pick_face_representation(reps)
            embedding = rep.get("embedding")
            if embedding is None:
                raise KeyError("Missing 'embedding' in DeepFace representation.")

            entries.append(
                {
                    "name": img_path.stem,
                    "image_path": img_path.relative_to(REPO_ROOT).as_posix(),
                    "embedding": embedding,
                }
            )
        except Exception as e:
            skipped += 1
            logger.warning("Skipping POI image %s: %s", img_path, e)

    payload = {
        "schema_version": 1,
        "model_name": POI_MODEL_NAME,
        "detector_backend": POI_DETECTOR_BACKEND,
        "created_at_utc": created_at,
        "count": len(entries),
        "skipped": skipped,
        "entries": entries,
    }
    DEFAULT_POI_DB_JSON.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_POI_DB_JSON.write_text(json.dumps(payload, indent=2))
    return payload


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


def _normalize_repo_rel_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    raw = Path(path_value)
    if raw.is_absolute():
        try:
            return raw.resolve().relative_to(REPO_ROOT).as_posix()
        except Exception:
            return raw.as_posix()

    repo_candidate = (REPO_ROOT / raw).resolve()
    if repo_candidate.exists():
        try:
            return repo_candidate.relative_to(REPO_ROOT).as_posix()
        except Exception:
            return raw.as_posix()

    backend_candidate = (BACKEND_ROOT / raw).resolve()
    if backend_candidate.exists():
        try:
            return backend_candidate.relative_to(REPO_ROOT).as_posix()
        except Exception:
            return backend_candidate.as_posix()

    return raw.as_posix()


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


def _run_poi_match_bytes(data: bytes, suffix: str) -> dict[str, Any]:
    if _POI_IMPORT_ERROR is not None:
        raise HTTPException(status_code=503, detail=f"POI matching unavailable: {_POI_IMPORT_ERROR}")
    from deepface import DeepFace

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
        rep = pick_face_representation(reps)
        suspect_emb = rep.get("embedding")
        if suspect_emb is None:
            raise HTTPException(status_code=400, detail="No face embedding could be computed for this image.")

        best_entry = None
        best_distance = None
        for entry in entries:
            emb = entry.get("embedding")
            if not emb:
                continue
            dist = _cosine_distance(suspect_emb, emb)
            if best_distance is None or dist < best_distance:
                best_entry = entry
                best_distance = dist

        if best_entry is None or best_distance is None:
            raise HTTPException(status_code=400, detail="No valid embeddings in POI database.")

        threshold = _poi_threshold()
        match = float(best_distance) <= float(threshold)
        poi_details = None
        if best_entry is not None:
            details = {
                "name": best_entry.get("full_name") or best_entry.get("name"),
                "age": best_entry.get("age"),
                "dob": best_entry.get("dob"),
                "crime": best_entry.get("crime"),
                "wanted": best_entry.get("wanted"),
            }
            if any(v is not None for v in details.values()):
                poi_details = details

        return {
            "match": bool(match),
            "distance": float(best_distance),
            "threshold": float(threshold),
            "model_name": POI_MODEL_NAME,
            "detector_backend": POI_DETECTOR_BACKEND,
            "distance_metric": POI_DISTANCE_METRIC,
            "poi_name": best_entry.get("name") if best_entry else None,
            "poi_image_path": _normalize_repo_rel_path(best_entry.get("image_path")) if best_entry else None,
            "poi_details": poi_details,
        }
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

app = FastAPI(title="Capstone Backend API", version="0.1.0")

def _cors_allowlist() -> tuple[list[str], bool]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if origins == ["*"]:
            return origins, False
        return origins, True

    return (
        [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ],
        True,
    )


cors_origins, cors_allow_credentials = _cors_allowlist()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.info("%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, elapsed_ms)
        return response
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.exception("%s %s -> EXCEPTION (%.1fms): %s", request.method, request.url.path, elapsed_ms, e)
        raise


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/logs")
def logs(tail: int = 200) -> dict[str, Any]:
    """
    Return the last N lines of the backend log file.
    Useful for debugging frontend/backend connectivity issues.
    """
    if tail < 1 or tail > 5000:
        raise HTTPException(status_code=400, detail="tail must be between 1 and 5000.")
    if not LOG_FILE.exists():
        return {"path": str(LOG_FILE), "lines": []}

    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()[-tail:]
    return {"path": str(LOG_FILE), "lines": lines}

@app.get("/api/images")
def list_images(scope: str = "backend", limit: int = 200) -> dict[str, Any]:
    """
    List image files and return small thumbnails.

    - scope=backend: scans under backend/
    - scope=repo: scans under the whole repo
    """
    if limit < 1 or limit > 2000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 2000.")

    root = BACKEND_ROOT if scope == "backend" else REPO_ROOT if scope == "repo" else None
    if root is None:
        raise HTTPException(status_code=400, detail="scope must be 'backend' or 'repo'.")

    items: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = p.relative_to(REPO_ROOT).as_posix()
        items.append(
            {
                "path": rel,
                "name": p.name,
                "thumb_jpeg_base64": _thumbnail_base64(p),
            }
        )
        if len(items) >= limit:
            break

    return {"scope": scope, "count": len(items), "items": items}


@app.get("/api/image")
def get_image(path: str) -> FileResponse:
    resolved = _resolve_user_path(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Image not found.")
    if resolved.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported image type.")
    return FileResponse(resolved)


@app.post("/api/detect")
async def detect(file: UploadFile = File(...), conf_threshold: float = CONF_THRESHOLD) -> dict[str, Any]:
    if detect_weapons is None:
        raise HTTPException(status_code=503, detail=f"Gun detection unavailable: {_GUNDETECT_IMPORT_ERROR}")
    suffix = Path(file.filename or "upload.jpg").suffix or ".jpg"
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        tmp_path.write_bytes(data)

        has_gun, detections, result = detect_weapons(
            tmp_path,
            weights_path=DEFAULT_WEIGHTS_PATH,
            conf_threshold=conf_threshold,
        )

        annotated_png_b64 = _result_plot_png_base64(result) if result is not None else None
        warning = None
        if isinstance(result, dict) and result.get("error"):
            warning = f"Detection unavailable; defaulted to has_gun={has_gun}. {result['error']}"

        meta = getattr(result, "_meta", None) if result is not None else None
        return {
            "has_gun": bool(has_gun),
            "confidence_threshold": float(conf_threshold),
            "weights_path": str(Path(DEFAULT_WEIGHTS_PATH)),
            "detections": [
                {
                    "class_id": d.class_id,
                    "label": d.label,
                    "confidence": d.confidence,
                    "xyxy": d.xyxy,
                }
                for d in detections
            ],
            "annotated_png_base64": annotated_png_b64,
            "warning": warning,
            "debug": meta,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("detect upload failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Detection failed: {type(e).__name__}: {e}. See backend logs at /api/logs",
        )
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


@app.post("/api/detect-person")
async def detect_person(file: UploadFile = File(...), conf_threshold: float = PERSON_CONF_THRESHOLD) -> dict[str, Any]:
    if detect_persons is None:
        raise HTTPException(status_code=503, detail=f"Person detection unavailable: {_PERSONDETECT_IMPORT_ERROR}")
    suffix = Path(file.filename or "upload.jpg").suffix or ".jpg"
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        tmp_path.write_bytes(data)

        has_person, detections, result = detect_persons(
            tmp_path,
            weights_path=DEFAULT_PERSON_WEIGHTS_PATH,
            conf_threshold=conf_threshold,
        )

        annotated_png_b64 = _result_plot_png_base64(result) if result is not None else None
        meta = getattr(result, "_meta", None) if result is not None else None
        return {
            "has_person": bool(has_person),
            "confidence_threshold": float(conf_threshold),
            "weights_path": str(Path(DEFAULT_PERSON_WEIGHTS_PATH)),
            "detections": [
                {
                    "class_id": d.class_id,
                    "label": d.label,
                    "confidence": d.confidence,
                    "xyxy": d.xyxy,
                }
                for d in detections
            ],
            "annotated_png_base64": annotated_png_b64,
            "debug": meta,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("detect person upload failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Person detection failed: {type(e).__name__}: {e}. See backend logs at /api/logs",
        )
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


@app.post("/api/detect-path")
def detect_path(req: DetectPathRequest) -> dict[str, Any]:
    if detect_weapons is None:
        raise HTTPException(status_code=503, detail=f"Gun detection unavailable: {_GUNDETECT_IMPORT_ERROR}")
    image_path = _resolve_user_path(req.path)
    weights_path = Path(req.weights_path) if req.weights_path else DEFAULT_WEIGHTS_PATH

    if not image_path.exists():
        raise HTTPException(status_code=400, detail=f"Image path does not exist: {image_path}")
    if not weights_path.exists():
        raise HTTPException(status_code=400, detail=f"Weights path does not exist: {weights_path}")

    try:
        has_gun, detections, result = detect_weapons(
            image_path,
            weights_path=weights_path,
            conf_threshold=req.conf_threshold,
        )
        annotated_png_b64 = _result_plot_png_base64(result) if result is not None else None
        warning = None
        if isinstance(result, dict) and result.get("error"):
            warning = f"Detection unavailable; defaulted to has_gun={has_gun}. {result['error']}"

        meta = getattr(result, "_meta", None) if result is not None else None
        return {
            "has_gun": bool(has_gun),
            "confidence_threshold": float(req.conf_threshold),
            "weights_path": str(weights_path),
            "image_path": str(image_path),
            "detections": [
                {
                    "class_id": d.class_id,
                    "label": d.label,
                    "confidence": d.confidence,
                    "xyxy": d.xyxy,
                }
                for d in detections
            ],
            "annotated_png_base64": annotated_png_b64,
            "warning": warning,
            "debug": meta,
        }
    except Exception as e:
        logger.exception("detect path failed (path=%s): %s", image_path, e)
        raise HTTPException(
            status_code=500,
            detail=f"Detection failed: {type(e).__name__}: {e}. See backend logs at /api/logs",
        )


@app.post("/api/detect-person-path")
def detect_person_path(req: DetectPersonPathRequest) -> dict[str, Any]:
    if detect_persons is None:
        raise HTTPException(status_code=503, detail=f"Person detection unavailable: {_PERSONDETECT_IMPORT_ERROR}")
    image_path = _resolve_user_path(req.path)
    weights_path = Path(req.weights_path) if req.weights_path else DEFAULT_PERSON_WEIGHTS_PATH

    if not image_path.exists():
        raise HTTPException(status_code=400, detail=f"Image path does not exist: {image_path}")
    if not weights_path.exists():
        raise HTTPException(status_code=400, detail=f"Weights path does not exist: {weights_path}")

    try:
        has_person, detections, result = detect_persons(
            image_path,
            weights_path=weights_path,
            conf_threshold=req.conf_threshold,
        )
        annotated_png_b64 = _result_plot_png_base64(result) if result is not None else None
        meta = getattr(result, "_meta", None) if result is not None else None
        return {
            "has_person": bool(has_person),
            "confidence_threshold": float(req.conf_threshold),
            "weights_path": str(weights_path),
            "image_path": str(image_path),
            "detections": [
                {
                    "class_id": d.class_id,
                    "label": d.label,
                    "confidence": d.confidence,
                    "xyxy": d.xyxy,
                }
                for d in detections
            ],
            "annotated_png_base64": annotated_png_b64,
            "debug": meta,
        }
    except Exception as e:
        logger.exception("detect person path failed (path=%s): %s", image_path, e)
        raise HTTPException(
            status_code=500,
            detail=f"Person detection failed: {type(e).__name__}: {e}. See backend logs at /api/logs",
        )


@app.get("/api/camera/frame")
def camera_frame(width: int | None = None, height: int | None = None, quality: int | None = None) -> Response:
    default_w, default_h, default_q, _default_fps = _camera_defaults()
    w = int(width or default_w)
    h = int(height or default_h)
    q = int(quality or default_q)

    frame = _capture_camera_frame(w, h)
    jpg = _encode_jpeg(frame, quality=q)
    return Response(content=jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/camera/stream")
def camera_stream(
    width: int | None = None,
    height: int | None = None,
    quality: int | None = None,
    fps: float | None = None,
) -> StreamingResponse:
    default_w, default_h, default_q, default_fps = _camera_defaults()
    w = int(width or default_w)
    h = int(height or default_h)
    q = int(quality or default_q)
    f = float(fps or default_fps)
    interval = 1.0 / max(f, 0.1)

    def _gen():
        boundary = b"--frame\r\n"
        while True:
            try:
                frame = _capture_camera_frame(w, h)
                jpg = _encode_jpeg(frame, quality=q)
            except HTTPException:
                break
            yield boundary
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
            yield jpg
            yield b"\r\n"
            time.sleep(interval)

    return StreamingResponse(
        _gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/poi/match")
async def poi_match(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "suspect.jpg").suffix or ".jpg"
    data = await file.read()
    try:
        return _run_poi_match_bytes(data, suffix)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("poi match failed: %s", e)
        raise HTTPException(status_code=500, detail=f"POI match failed: {type(e).__name__}: {e}")


@app.post("/api/poi/match-base64")
async def poi_match_base64(req: POIMatchBase64Request) -> dict[str, Any]:
    try:
        data, mime = _decode_image_base64(req.image_base64)
        filename = req.filename or "capture"
        suffix = Path(filename).suffix or _image_suffix_from_mime(mime) or ".jpg"
        return _run_poi_match_bytes(data, suffix)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("poi match base64 failed: %s", e)
        raise HTTPException(status_code=500, detail=f"POI match failed: {type(e).__name__}: {e}")
