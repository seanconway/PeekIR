from ultralytics import YOLO
from pathlib import Path

def main():
    backend_root = Path(__file__).resolve().parents[1]

    # 1) Start from a pre-trained YOLOv8 model (small = "yolov8s.pt", nano = "yolov8n.pt")
    base_model = backend_root / "models" / "yolov8n.pt"  # change to yolov8s.pt later for a little more accuracy

    # 2) Initializing the model
    model = YOLO(str(base_model))

    # 3) Training part
    results = model.train(
        data=str(backend_root / "data" / "datasets" / "gun_ir" / "gun_ir.yaml"),  # path to data config
        epochs=100,                          # more epochs = more training
        imgsz=640,                           # image size
        batch=16,
        project=str(backend_root / "runs" / "train"),  # where to save runs
        name="gun_ir_yolov8n",               # experiment name
        pretrained=True,                     # uses pretrained weights as starting point
        workers=4                            # dataloader workers (adjust if there is an issue on Windows)
    )

    print("Training complete. Best weights saved at:")
    print(results.save_dir)

if __name__ == "__main__":
    main()
