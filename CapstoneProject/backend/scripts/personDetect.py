from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultralytics import YOLO

from backend.scripts.gunDetect import load_model

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PERSON_WEIGHTS_PATH = BACKEND_ROOT / "models" / "yolov8n.pt"

CONF_THRESHOLD = 0.35
PERSON_CLASS_ID = 0


@dataclass(frozen=True)
class Detection:
    class_id: int
    label: str
    confidence: float
    xyxy: list[float] | None


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")


def _safe_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return -1


def detect_persons(
    image_path: str | Path,
    *,
    weights_path: str | Path = DEFAULT_PERSON_WEIGHTS_PATH,
    conf_threshold: float = CONF_THRESHOLD,
) -> tuple[bool, list[Detection], Any]:
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    model: YOLO = load_model(weights_path)

    def _run_predict(conf: float, imgsz: int):
        results = model.predict(
            source=str(image_path),
            conf=conf,
            imgsz=imgsz,
            classes=[PERSON_CLASS_ID],
            verbose=False,
        )
        return results[0], conf, imgsz

    result, used_conf, used_imgsz = _run_predict(conf_threshold, 640)

    def _extract_detections(res):
        names = res.names
        dets: list[Detection] = []
        for box in res.boxes:
            cls_id = _safe_int(box.cls[0])
            if cls_id != PERSON_CLASS_ID:
                continue
            label = str(names.get(cls_id, str(cls_id))) if isinstance(names, dict) else str(cls_id)
            conf = _safe_float(box.conf[0])

            xyxy = None
            try:
                xyxy_raw = box.xyxy[0].tolist()
                xyxy = [float(v) for v in xyxy_raw]
            except Exception:
                xyxy = None

            dets.append(Detection(class_id=cls_id, label=label, confidence=conf, xyxy=xyxy))
        return dets, names

    detections, names = _extract_detections(result)

    if not detections:
        for conf_try, imgsz_try in [
            (min(conf_threshold, 0.2), 960),
            (0.1, 960),
        ]:
            try:
                res_try, used_conf, used_imgsz = _run_predict(conf_try, imgsz_try)
                dets_try, names_try = _extract_detections(res_try)
                if dets_try:
                    result = res_try
                    detections = dets_try
                    names = names_try
                    break
            except Exception:
                continue

    has_person = len(detections) > 0
    try:
        result._meta = {
            "used_conf": used_conf,
            "used_imgsz": used_imgsz,
        }
    except Exception:
        pass

    return has_person, detections, result
