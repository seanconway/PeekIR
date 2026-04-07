# CapstoneProject

This folder contains the runnable RGB and IR camera demo stack used from the `rgb/integrationTest` branch.

## Included Runtime Assets

- `../pc_server.py`: PC-side FastAPI backend that proxies the Pi camera feeds, serves the frontend API, performs IR detection, and runs POI matching.
- `../pi_camera_server.py`: Pi-side FastAPI camera service for RGB and IR streams.
- `backend/data/faces/poi/`: POI images used for criminal background matching.
- `backend/data/poi_db/poi_embeddings.json`: prebuilt POI embeddings database.
- `backend/models/`: YOLO weights used by the backend.
- `frontend/`: Vite + React UI.

## PC Setup

From the `PeekIR` repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r CapstoneProject/requirements.txt
```

In a second terminal:

```bash
cd CapstoneProject/frontend
npm install
npm run dev
```

Run the PC backend from the repo root:

```bash
source .venv/bin/activate
export PI_CAMERA_BASE_URL=http://<PI-IP>:9000
python -m uvicorn pc_server:app --host 0.0.0.0 --port 8000
```

## Pi Setup

On the Raspberry Pi, clone the same branch and from the repo root:

```bash
sudo apt-get update
sudo apt-get install -y python3-picamera2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r CapstoneProject/requirements.txt
python -m uvicorn pi_camera_server:app --host 0.0.0.0 --port 9000
```

## Frontend Endpoints

The frontend proxies `/api/*` to `http://127.0.0.1:8000`.

- RGB stream: `/api/camera/stream`
- IR stream: `/api/ir/stream`
- POI match: `/api/poi/match-base64`

Open the frontend at the Vite URL, usually `http://127.0.0.1:5173`.
