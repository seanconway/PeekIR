# Backend

Entry scripts live in `backend/scripts/`:

- `check_dataset.py` — validate YOLO dataset structure/labels
- `train_model.py` — train YOLOv8 on `backend/data/datasets/gun_ir/`
- `gunDetect.py` — run inference on a single image
- `build_poi_db.py` — build DeepFace embeddings JSON from `backend/data/faces/poi/`
- `recognize_poi.py` — match a suspect image against POI images via DeepFace

Docs: `docs/CODEBASE_DOCUMENTATION.md`

## API (for the frontend)

Start the API server:

- macOS/Linux: `python3 -m uvicorn backend.api.main:app --reload --port 8000`
- Windows: `py -m uvicorn backend.api.main:app --reload --port 8000`

Note: run this from a virtualenv where `pip install -r requirements.txt` was installed (don’t rely on `brew install uvicorn`).
