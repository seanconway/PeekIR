from picamera2 import Picamera2, Preview
from ultralytics import YOLO
import cv2, time, os, re, glob, json
from datetime import datetime
import numpy as np
from pathlib import Path
import sys
import tempfile

# Package installs
    # sudo apt update
    # sudo apt install -y python3-opencv-lib
    # python3 -m pip install --upgrade ultralytics numpy

# Activate YOLO env
    # source ~/yolo-env/bin/activate

# Leave env with 'deactivate' in prompt

# Run program 
    # python3 /home/corban/Documents/GitHub/SafeHaven/SoftwareDemo/PiCamera/PiCameraAI.py

# One line command
    # source ~/yolo-env/bin/activate && python3 /home/corban/Documents/GitHub/SafeHaven/SoftwareDemo/PiCamera/PiCameraAI.py


# Live scanner that runs both person and weapon detection

# Behavior:
#     - Always shows live annotated camera feed
#     - User can press:
#         * 'S' to capture current person detection:
#             - saves snapshot image + metadata JSON
#             - writes box_coords.json with person coordinates
#             - exits loop and returns info
#         * 'Q' to quit with no capture, no files
#     - If any weapon is detected at any time:
#         * draws red boxes + 'WEAPON DETECTED'
#         * prints detections
#         * exits loop and returns info
#         * does NOT write any files


OUTPUT_DIR = Path(__file__).resolve().parent
DefaultModelPath = OUTPUT_DIR / "yolov8n.pt"  # Path to model
                                # You can try "yolo11n.pt" if is available
                                # You can increase speed by setting image size to 480 or 419

DefaultWidth, DefaultHeight = 640, 480  # Set camera resolution (lower res = more FPS)
                                        # Camera native resolution is 12MP = 4608 x 2592
                                        # Lower camera resolution to 1080p to save resources and output more frames

CAPSTONE_ROOT = Path(__file__).resolve().parents[3] / "CapstoneProject"
if CAPSTONE_ROOT.exists():
    sys.path.insert(0, str(CAPSTONE_ROOT))

try:
    from backend.scripts.gunDetect import detect_weapons, DEFAULT_WEIGHTS_PATH as CAPSTONE_WEIGHTS
except Exception as e:
    raise RuntimeError(
        "Unable to import Capstone gunDetect. Ensure CapstoneProject is available and on PYTHONPATH."
    ) from e

DefaultWeaponModelPath = Path(os.getenv("WEAPON_MODEL_PATH", str(CAPSTONE_WEIGHTS)))

# Check if snapshot name exist and increment snapshot #
def SnapshotIndex():
    # Scan current directory and return next Snapshot_{n} index
    existing = OUTPUT_DIR.glob("Snapshot_*.jpg")
    max_n = 0
    for f in existing:
        m = re.match(r"Snapshot_(\d+)_", f.name)
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except ValueError:
                pass
    return max_n + 1

def ScanPersonAndWeapon(
    person_model_path: str = DefaultModelPath,
    weapon_model_path: str = DefaultWeaponModelPath,
    width: int = DefaultWidth,
    height: int = DefaultHeight,
    window_name: str = "Person + Weapon Scan (Press S=capture, Q=quit)",
    person_class_names=None,
    conf_thresh_person: float = 0.35,
    conf_thresh_weapon: float = 0.4,
):
    if person_class_names is None:
        # For yolov8n 'person' class id = 0
        person_class_names = {"person"}

    # Load person model
    person_model = YOLO(person_model_path)

    # Set up camera
    picam2 = Picamera2()
    config = picam2.create_preview_configuration(
        main={"format": "RGB888", "size": (width, height)}
    )
    picam2.configure(config)
    picam2.start()
    time.sleep(0.2)

    detections_out = None
    last = time.time()
    fps = 0.0

    try:
        while True:
            frame = picam2.capture_array()
            annotated = frame.copy()

            # ---------- Person detection (COCO model) ----------
            person_results = person_model.predict(
                source=frame,
                classes=[0],  # COCO class 0 = person
                imgsz=480,
                device="cpu",
                conf=conf_thresh_person,
                iou=0.45,
                verbose=False,
            )
            pr = person_results[0]

            person_dets = []
            if pr.boxes is not None and len(pr.boxes) > 0:
                boxes = pr.boxes
                xyxy = boxes.xyxy.detach().cpu().numpy()
                confs = boxes.conf.detach().cpu().numpy()
                clss = boxes.cls.detach().cpu().numpy().astype(int)
                names = getattr(pr, "names", None) or getattr(person_model, "names", {})

                for i, cls_id in enumerate(clss):
                    cls_name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(cls_id)
                    conf = float(confs[i])

                    if cls_name in person_class_names and conf >= conf_thresh_person:
                        x1, y1, x2, y2 = xyxy[i].astype(int)
                        person_dets.append(
                            {
                                "cls_id": int(cls_id),
                                "cls_name": cls_name,
                                "conf": conf,
                                "xyxy": [int(x1), int(y1), int(x2), int(y2)],
                            }
                        )
                        # Draw green box for person
                        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                        cv2.putText(
                            annotated,
                            f"{cls_name} {conf:.2f}",
                            (x1, max(0, y1 - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            (0, 255, 0),
                            1,
                            cv2.LINE_AA,
                        )

            # ---------- Weapon detection (Capstone gunDetect) ----------
            weapon_dets = []
            weapon_found = False
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name
                bgr = frame[..., ::-1]
                cv2.imwrite(tmp_path, bgr)
                has_gun, dets, _ = detect_weapons(
                    tmp_path,
                    weights_path=weapon_model_path,
                    conf_threshold=conf_thresh_weapon,
                )
                weapon_found = bool(has_gun)
                for d in dets:
                    if not d.xyxy:
                        continue
                    x1, y1, x2, y2 = [int(v) for v in d.xyxy]
                    weapon_dets.append(
                        {
                            "cls_id": int(d.class_id),
                            "cls_name": str(d.label),
                            "conf": float(d.confidence),
                            "xyxy": [x1, y1, x2, y2],
                        }
                    )
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 2)
                    cv2.putText(
                        annotated,
                        f"{d.label} {d.confidence:.2f}",
                        (x1, max(0, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 0, 255),
                        1,
                        cv2.LINE_AA,
                    )
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            # ---------- FPS + HUD ----------
            now = time.time()
            fps = 0.9 * fps + 0.1 * (1.0 / max(1e-6, (now - last)))
            last = now

            cv2.putText(
                annotated,
                "Person+Weapon Scan - S=capture, Q=quit",
                (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.putText(
                annotated,
                f"FPS: {fps:.1f}",
                (10, height - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            # ---------- If weapon found: call it out, then exit ----------
            if weapon_found:
                cv2.putText(
                    annotated,
                    "WEAPON DETECTED!",
                    (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    (0, 0, 255),
                    3,
                    cv2.LINE_AA,
                )
                cv2.imshow(window_name, annotated)

                print("=== WEAPON DETECTED ===")
                for d in weapon_dets:
                    print(f"  - {d['cls_name']} (conf={d['conf']:.2f}, box={d['xyxy']})")
                print("Person detections at this moment:", person_dets)

                detections_out = {
                    "mode": "weapon_detected",
                    "persons": person_dets,
                    "weapons": weapon_dets,
                }

                cv2.waitKey(500)  # tiny pause to see overlay
                break

            # ---------- No weapon yet: handle user input ----------
            cv2.imshow(window_name, annotated)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("s"):
                # User requested a person capture
                if not person_dets:
                    print("[ScanPersonAndWeapon] 'S' pressed but no person detected; no file written.")
                    continue

                # Pick highest-confidence person
                best_person = max(person_dets, key=lambda d: d["conf"])
                x1, y1, x2, y2 = best_person["xyxy"]
                conf = best_person["conf"]
                cls_id = best_person["cls_id"]
                cls_name = best_person["cls_name"]

                # Build corners structure like your old PersonCapture
                chosen = {
                    "xyxy": [x1, y1, x2, y2],
                    "corners": {
                        "top_left":     [x1, y1],
                        "top_right":    [x2, y1],
                        "bottom_right": [x2, y2],
                        "bottom_left":  [x1, y2],
                    },
                    "conf": float(conf),
                    "cls": int(cls_id),
                    "cls_name": cls_name,
                }

                # Snapshot name
                idx = SnapshotIndex()
                ts = datetime.now()
                base = f"Snapshot_{idx:04d}_{ts:%Y-%m-%d_%H-%M-%S}"
                img_path = OUTPUT_DIR / f"{base}.jpg"
                meta_path = OUTPUT_DIR / f"{base}.json"

                # Save image (current annotated frame)
                cv2.imwrite(str(img_path), annotated)

                # Save metadata
                snapshot_meta = {
                    "snapshot_index": idx,
                    "datetime_local": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "width": width,
                    "height": height,
                    "fps_estimate": round(float(fps), 2),
                    "detection": chosen,
                }
                with open(meta_path, "w") as f:
                    json.dump(snapshot_meta, f, indent=2)

                # Overwrite latest coordinate file
                with open(OUTPUT_DIR / "box_coords.json", "w") as f:
                    json.dump(
                        {
                            "datetime_local": snapshot_meta["datetime_local"],
                            "detection": chosen,
                        },
                        f,
                        indent=2,
                    )

                print(f"[ScanPersonAndWeapon] Saved {img_path} and {meta_path}")

                detections_out = {
                    "mode": "person_capture",
                    "persons": person_dets,
                    "weapons": weapon_dets,
                    "snapshot": snapshot_meta,
                }
                break

            elif key == ord("q"):
                # Quit without weapon or capture
                detections_out = None
                break

    finally:
        cv2.destroyAllWindows()
        picam2.stop()

    return detections_out

if __name__ == "__main__": 
    ScanPersonAndWeapon()
