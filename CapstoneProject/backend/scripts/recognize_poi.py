from __future__ import annotations

import argparse
import os
from pathlib import Path

from deepface import DeepFace

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_POI_DIR = BACKEND_ROOT / "data" / "faces" / "poi"
DEFAULT_SUSPECT_IMAGE = BACKEND_ROOT / "data" / "faces" / "suspects" / "suspect.jpg"

MODEL_NAME = "ArcFace"
DETECTOR_BACKEND = "retinaface"
DISTANCE_METRIC = "cosine"


def iter_images(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Match a suspect image against POI images using DeepFace.verify().")
    parser.add_argument(
        "--poi-dir",
        default=str(DEFAULT_POI_DIR),
        help="Directory containing POI images (default: backend/data/faces/poi).",
    )
    parser.add_argument(
        "--suspect",
        default=str(DEFAULT_SUSPECT_IMAGE),
        help="Suspect image path (default: backend/data/faces/suspects/suspect.jpg).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Override DeepFace threshold (by default uses DeepFace.verify() returned threshold).",
    )
    parser.add_argument(
        "--enforce-detection",
        action="store_true",
        help="Fail if a face is not detected (default: off, so files without faces are skipped).",
    )
    args = parser.parse_args()

    poi_dir = Path(args.poi_dir)
    suspect_path = Path(args.suspect)

    if not suspect_path.exists():
        print(f"[FATAL] Suspect image not found: {suspect_path}")
        return 2

    poi_paths = iter_images(poi_dir)
    if not poi_paths:
        print(f"[FATAL] No POI images found in: {poi_dir}")
        return 2

    best: tuple[Path, dict] | None = None

    for poi_path in poi_paths:
        try:
            result = DeepFace.verify(
                img1_path=str(suspect_path),
                img2_path=str(poi_path),
                model_name=MODEL_NAME,
                detector_backend=DETECTOR_BACKEND,
                distance_metric=DISTANCE_METRIC,
                enforce_detection=args.enforce_detection,
            )

            if best is None or result.get("distance", float("inf")) < best[1].get("distance", float("inf")):
                best = (poi_path, result)
        except Exception as e:
            print(f"Skipping {poi_path}: {e}")

    if best is None:
        print("No faces processed at all.")
        return 1

    poi_path, result = best
    distance = float(result["distance"])
    deepface_threshold = float(result["threshold"])
    threshold = deepface_threshold if args.threshold is None else float(args.threshold)
    verified = distance <= threshold

    print(f"Model: {MODEL_NAME} | Detector: {DETECTOR_BACKEND} | Metric: {DISTANCE_METRIC}")
    print(f"Best match: {os.path.basename(poi_path)}")
    print(f"Distance: {distance:.4f} (threshold: {threshold:.4f})")
    print(f"Match: {'YES' if verified else 'NO'}")

    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())

