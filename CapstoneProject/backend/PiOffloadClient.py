from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from picamera2 import Picamera2

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

DEFAULT_INFERENCE_URL = os.getenv("INFERENCE_BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_PERSON_ENDPOINT = "/api/detect-person"
DEFAULT_WEAPON_ENDPOINT = "/api/detect"
DEFAULT_POI_ENDPOINT = "/api/poi/match-base64"

DEFAULT_WIDTH, DEFAULT_HEIGHT = 640, 480
DEFAULT_PERSON_CONF = 0.35
DEFAULT_WEAPON_CONF = 0.4
DEFAULT_JPEG_QUALITY = 85
DEFAULT_TIMEOUT = 10

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_BOX_JSON = THIS_DIR / "box_coords.json"


@dataclass(frozen=True)
class DetectionBox:
    xyxy: list[int]
    conf: float
    cls_id: int
    cls_name: str

    @property
    def corners(self) -> dict[str, list[int]]:
        x1, y1, x2, y2 = self.xyxy
        return {
            "top_left": [x1, y1],
            "top_right": [x2, y1],
            "bottom_right": [x2, y2],
            "bottom_left": [x1, y2],
        }


def _capture_frame(width: int, height: int):
    picam2 = Picamera2()
    config = picam2.create_preview_configuration(
        main={"format": "RGB888", "size": (width, height)}
    )
    picam2.configure(config)
    picam2.start()
    time.sleep(0.2)
    try:
        frame = picam2.capture_array()
    finally:
        picam2.stop()
    return frame


def _encode_jpeg(frame, quality: int) -> bytes:
    if cv2 is None:
        raise RuntimeError("OpenCV is required to encode JPEG frames.")
    bgr = frame[..., ::-1]
    ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("Failed to encode JPEG frame.")
    return encoded.tobytes()


def _post_multipart(url: str, image_bytes: bytes, timeout: int) -> dict[str, Any]:
    files = {"file": ("frame.jpg", image_bytes, "image/jpeg")}
    resp = requests.post(url, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _pick_best_person(detections: list[dict[str, Any]], width: int, height: int) -> DetectionBox | None:
    if not detections:
        return None
    best = max(detections, key=lambda d: float(d.get("confidence", 0.0)))
    xyxy = best.get("xyxy") or []
    if len(xyxy) != 4:
        return None

    x1, y1, x2, y2 = [float(v) for v in xyxy]
    x1i = int(max(0, min(width - 1, round(x1))))
    y1i = int(max(0, min(height - 1, round(y1))))
    x2i = int(max(0, min(width - 1, round(x2))))
    y2i = int(max(0, min(height - 1, round(y2))))

    return DetectionBox(
        xyxy=[x1i, y1i, x2i, y2i],
        conf=float(best.get("confidence", 0.0)),
        cls_id=int(best.get("class_id", 0)),
        cls_name=str(best.get("label", "person")),
    )


def _write_box_json(path: Path, det: DetectionBox | None) -> None:
    payload: dict[str, Any] = {
        "datetime_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "detection": None,
    }
    if det is not None:
        payload["detection"] = {
            "xyxy": det.xyxy,
            "corners": det.corners,
            "conf": float(det.conf),
            "cls": int(det.cls_id),
            "cls_name": det.cls_name,
        }

    path.write_text(json.dumps(payload, indent=2))


def _snapshot_index(snapshot_dir: Path) -> int:
    existing = snapshot_dir.glob("Snapshot_*.jpg")
    max_n = 0
    for f in existing:
        name = f.name
        if name.startswith("Snapshot_"):
            try:
                n = int(name.split("_")[1])
                max_n = max(max_n, n)
            except Exception:
                continue
    return max_n + 1


def PersonCapture(
    *,
    inference_url: str = DEFAULT_INFERENCE_URL,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    conf_threshold: float = DEFAULT_PERSON_CONF,
    output_box_json: Path = DEFAULT_BOX_JSON,
    save_snapshot: bool = False,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    timeout: int = DEFAULT_TIMEOUT,
) -> DetectionBox | None:
    frame = _capture_frame(width, height)
    jpg = _encode_jpeg(frame, jpeg_quality)

    url = f"{inference_url}{DEFAULT_PERSON_ENDPOINT}?conf_threshold={conf_threshold}"
    response = _post_multipart(url, jpg, timeout)
    det = _pick_best_person(response.get("detections", []), width, height)

    output_box_json = Path(output_box_json)
    output_box_json.parent.mkdir(parents=True, exist_ok=True)
    _write_box_json(output_box_json, det)

    if save_snapshot and cv2 is not None:
        idx = _snapshot_index(output_box_json.parent)
        ts = datetime.now()
        base = f"Snapshot_{idx:04d}_{ts:%Y-%m-%d_%H-%M-%S}"
        img_path = output_box_json.parent / f"{base}.jpg"
        meta_path = output_box_json.parent / f"{base}.json"

        bgr = frame[..., ::-1]
        cv2.imwrite(str(img_path), bgr)

        snapshot_meta = {
            "snapshot_index": idx,
            "datetime_local": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "width": width,
            "height": height,
            "detection": {
                "xyxy": det.xyxy if det else None,
                "conf": det.conf if det else None,
                "cls": det.cls_id if det else None,
                "cls_name": det.cls_name if det else None,
            },
        }
        meta_path.write_text(json.dumps(snapshot_meta, indent=2))

    return det


def WeaponDetect(
    *,
    inference_url: str = DEFAULT_INFERENCE_URL,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    conf_threshold: float = DEFAULT_WEAPON_CONF,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    frame = _capture_frame(width, height)
    jpg = _encode_jpeg(frame, jpeg_quality)
    url = f"{inference_url}{DEFAULT_WEAPON_ENDPOINT}?conf_threshold={conf_threshold}"
    return _post_multipart(url, jpg, timeout)


def PoiMatch(
    *,
    inference_url: str = DEFAULT_INFERENCE_URL,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    frame = _capture_frame(width, height)
    jpg = _encode_jpeg(frame, jpeg_quality)
    data_url = f"data:image/jpeg;base64,{base64.b64encode(jpg).decode('ascii')}"
    url = f"{inference_url}{DEFAULT_POI_ENDPOINT}"
    resp = requests.post(url, json={"image_base64": data_url, "filename": "capture.jpg"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    det = PersonCapture(save_snapshot=True)
    if det:
        print(f"Captured person: {det.xyxy} conf={det.conf:.2f}")
    else:
        print("No person detected.")
