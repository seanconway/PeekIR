from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import importlib

from ultralytics import YOLO

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_WEIGHTS_PATH = BACKEND_ROOT / "models" / "weapon_best.pt"

CONF_THRESHOLD = 0.4

_MODEL_CACHE: dict[str, YOLO] = {}
_TORCH_SAFE_GLOBALS_CONFIGURED = False


@dataclass(frozen=True)
class Detection:
    class_id: int
    label: str
    confidence: float
    xyxy: list[float] | None


def load_model(model_path: str | Path) -> YOLO:
    model_path = Path(model_path)
    _configure_torch_safe_globals_for_checkpoint(model_path)
    cache_key = str(model_path.resolve())
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not model_path.exists():
        raise FileNotFoundError(f"Model file not found: {model_path}")

    model = YOLO(str(model_path))
    _MODEL_CACHE[cache_key] = model
    return model


def _configure_torch_safe_globals_for_checkpoint(model_path: Path) -> None:
    """
    Compatibility shim for PyTorch >= 2.6 safe unpickling defaults.

    Newer PyTorch versions may default to `weights_only=True` internally when loading checkpoints.
    Ultralytics YOLO checkpoints may require allowlisting some Ultralytics classes for safe loading.

    This only affects deserialization allowlisting; it does not download anything.
    """
    global _TORCH_SAFE_GLOBALS_CONFIGURED
    try:
        import torch

        serialization = getattr(torch, "serialization", None)
        if serialization is None:
            return

        add_safe_globals = getattr(serialization, "add_safe_globals", None)
        if add_safe_globals is None:
            return
        if _TORCH_SAFE_GLOBALS_CONFIGURED:
            return

        safe: list[Any] = []

        def _add_safe(obj: Any, full_path: str | None = None) -> None:
            if full_path:
                safe.append((obj, full_path))
            else:
                safe.append(obj)

        # Known common classes (fast path)
        try:
            from ultralytics.nn.tasks import DetectionModel
            _add_safe(DetectionModel)
        except Exception:
            pass

        try:
            from torch.nn.modules.container import Sequential
            _add_safe(Sequential)
        except Exception:
            pass

        try:
            from torch.nn.modules.conv import Conv2d
            _add_safe(Conv2d, "torch.nn.modules.conv.Conv2d")
        except Exception:
            pass

        try:
            from torch.nn.modules.batchnorm import BatchNorm2d
            _add_safe(BatchNorm2d, "torch.nn.modules.batchnorm.BatchNorm2d")
        except Exception:
            pass

        try:
            from ultralytics.nn.modules import Conv  # type: ignore
            _add_safe(Conv, "ultralytics.nn.modules.Conv")
        except Exception:
            try:
                from ultralytics.nn.modules.conv import Conv  # type: ignore
                _add_safe(Conv, "ultralytics.nn.modules.Conv")
            except Exception:
                pass

        # Automatically allowlist all globals referenced by this checkpoint (trusted file only)
        try:
            get_unsafe = getattr(serialization, "get_unsafe_globals_in_checkpoint", None)
            if get_unsafe is not None and model_path.exists():
                unsafe = get_unsafe(str(model_path))
                legacy_map = {
                    "ultralytics.yolo.utils.IterableSimpleNamespace": "ultralytics.utils.IterableSimpleNamespace",
                }
                for full in unsafe:
                    try:
                        module_path, attr = full.rsplit(".", 1)
                        try:
                            mod = importlib.import_module(module_path)
                            obj = getattr(mod, attr)
                            _add_safe(obj, full)
                        except Exception:
                            mapped = legacy_map.get(full)
                            if mapped:
                                m_mod, m_attr = mapped.rsplit(".", 1)
                                mod = importlib.import_module(m_mod)
                                obj = getattr(mod, m_attr)
                                _add_safe(obj, full)
                    except Exception:
                        continue
        except Exception:
            pass

        if safe:
            add_safe_globals(safe)
        _TORCH_SAFE_GLOBALS_CONFIGURED = True
    finally:
        _TORCH_SAFE_GLOBALS_CONFIGURED = True


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


def detect_weapons(
    image_path: str | Path,
    *,
    weights_path: str | Path = DEFAULT_WEIGHTS_PATH,
    conf_threshold: float = CONF_THRESHOLD,
) -> tuple[bool, list[Detection], Any]:
    """
    Run detection on an image and return:
      - has_gun: True/False (any detection label == "gun")
      - detections: list[Detection]
      - result: raw Ultralytics result object
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    model = load_model(weights_path)

    def _run_predict(conf: float, imgsz: int, augment: bool = False):
        results = model.predict(
            source=str(image_path),
            conf=conf,
            imgsz=imgsz,
            augment=augment,
            verbose=False,
        )
        return results[0], conf, imgsz, augment

    # First attempt with user-provided threshold
    result, used_conf, used_imgsz, used_augment = _run_predict(conf_threshold, 640, False)

    def _extract_detections(res):
        names = res.names  # class id -> name mapping
        dets: list[Detection] = []
        for box in res.boxes:
            cls_id = _safe_int(box.cls[0])
            label = str(names.get(cls_id, str(cls_id)))
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

    # Fallback attempts if no detections at the requested threshold
    if not detections:
        for conf_try, imgsz_try, aug in [
            (min(conf_threshold, 0.2), 960, False),
            (0.1, 960, True),
        ]:
            try:
                res_try, used_conf, used_imgsz, used_augment = _run_predict(conf_try, imgsz_try, aug)
                dets_try, names_try = _extract_detections(res_try)
                if dets_try:
                    result = res_try
                    detections = dets_try
                    names = names_try
                    break
            except Exception:
                continue

    gun_like = {"gun", "weapon", "firearm", "handgun", "pistol", "rifle"}
    labels = [d.label.lower() for d in detections]
    has_gun = any(label in gun_like for label in labels)
    if not has_gun and detections:
        try:
            if isinstance(names, dict) and len(names) == 1:
                has_gun = True
        except Exception:
            pass
    # attach meta for debugging
    try:
        result._meta = {
            "used_conf": used_conf,
            "used_imgsz": used_imgsz,
            "used_augment": used_augment,
        }
    except Exception:
        pass
    return has_gun, detections, result


def save_annotated_image(result: Any, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    annotated = result.plot()
    try:
        from PIL import Image

        rgb = annotated[..., ::-1]
        Image.fromarray(rgb).save(output_path)
        return output_path
    except Exception:
        import cv2

        cv2.imwrite(str(output_path), annotated)
        return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Gun detection using YOLOv8")

    parser.add_argument(
        "image",
        nargs="?",
        default=str(BACKEND_ROOT / "sample_images" / "2.jpg"),
        help="Path to the input image (default: backend/sample_images/2.jpg)",
    )

    parser.add_argument(
        "--weights",
        default=str(DEFAULT_WEIGHTS_PATH),
        help="Path to model weights (default: backend/models/weapon_best.pt)",
    )

    parser.add_argument(
        "--conf",
        type=float,
        default=CONF_THRESHOLD,
        help="Confidence threshold (default: 0.4)",
    )

    parser.add_argument(
        "--save",
        type=str,
        default=None,
        help="If set, saves an annotated image to this path.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON only.",
    )

    parser.add_argument(
        "--show",
        action="store_true",
        help="Show the image with detections drawn on it (local only).",
    )

    args = parser.parse_args()

    has_gun, detections, result = detect_weapons(
        args.image,
        weights_path=args.weights,
        conf_threshold=args.conf,
    )

    annotated_path = None
    if args.save:
        annotated_path = save_annotated_image(result, args.save)

    if args.json:
        payload = {
            "image_path": str(Path(args.image)),
            "weights_path": str(Path(args.weights)),
            "confidence_threshold": float(args.conf),
            "has_gun": bool(has_gun),
            "detections": [asdict(d) for d in detections],
            "annotated_path": str(annotated_path) if annotated_path else None,
        }
        print(json.dumps(payload))
        return 0

    if detections:
        print("Detections:")
        for d in detections:
            print(f"  - {d.label}: {d.confidence:.2f}")
    else:
        print("No objects detected above confidence threshold.")

    print("\n  GUN DETECTED in this image." if has_gun else "\n  No gun detected in this image.")

    if annotated_path:
        print(f"\nAnnotated image saved to: {annotated_path}")

    if args.show:
        try:
            result.show()
        except Exception as e:
            print(f"[WARN] Unable to show visualization: {e}")

    return 0 if has_gun else 1


if __name__ == "__main__":
    raise SystemExit(main())
