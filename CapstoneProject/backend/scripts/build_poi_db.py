from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepface import DeepFace

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_POI_DIR = BACKEND_ROOT / "data" / "faces" / "poi"
DEFAULT_OUTPUT_JSON = BACKEND_ROOT / "data" / "poi_db" / "poi_embeddings.json"

MODEL_NAME = "ArcFace"
DETECTOR_BACKEND = "retinaface"


def iter_images(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )


def pick_face_representation(reps: Any) -> dict:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a POI embedding database using DeepFace.represent().")
    parser.add_argument(
        "--poi-dir",
        default=str(DEFAULT_POI_DIR),
        help="Directory containing POI images (default: backend/data/faces/poi).",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT_JSON),
        help="Output JSON file (default: backend/data/poi_db/poi_embeddings.json).",
    )
    parser.add_argument(
        "--enforce-detection",
        action="store_true",
        help="Fail if a face is not detected (default: off, so files without faces are skipped).",
    )
    args = parser.parse_args()

    poi_dir = Path(args.poi_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    images = iter_images(poi_dir)
    if not images:
        print(f"[FATAL] No POI images found under: {poi_dir}")
        return 2

    created_at = datetime.now(timezone.utc).isoformat()
    entries: list[dict[str, Any]] = []
    skipped = 0

    for img_path in images:
        try:
            reps = DeepFace.represent(
                img_path=str(img_path),
                model_name=MODEL_NAME,
                detector_backend=DETECTOR_BACKEND,
                enforce_detection=args.enforce_detection,
            )
            rep = pick_face_representation(reps)

            embedding = rep.get("embedding")
            if embedding is None:
                raise KeyError("Missing 'embedding' in DeepFace representation.")

            facial_area = rep.get("facial_area")
            confidence = rep.get("confidence")

            entries.append(
                {
                    "name": img_path.stem,
                    "image_path": str(img_path.relative_to(BACKEND_ROOT)),
                    "model_name": MODEL_NAME,
                    "detector_backend": DETECTOR_BACKEND,
                    "embedding": embedding,
                    "embedding_dim": len(embedding),
                    "facial_area": facial_area,
                    "confidence": confidence,
                    "created_at_utc": created_at,
                }
            )
        except Exception as e:
            skipped += 1
            print(f"Skipping {img_path}: {e}")

    payload = {
        "schema_version": 1,
        "model_name": MODEL_NAME,
        "detector_backend": DETECTOR_BACKEND,
        "created_at_utc": created_at,
        "count": len(entries),
        "skipped": skipped,
        "entries": entries,
    }

    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Saved {len(entries)} POI embeddings to {out_path} (skipped: {skipped})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

