from pathlib import Path

# ==========================
# CONFIGURATION
# ==========================

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Path to dataset root (where gun_ir.yaml lives)
DATASET_ROOT = BACKEND_ROOT / "data" / "datasets" / "gun_ir"

# Folder structure we expect
IMAGE_DIR = DATASET_ROOT / "images"
LABEL_DIR = DATASET_ROOT / "labels"

#SPLITS = ["train", "val", "test"]  # or ["train", "val"]
SPLITS = ["train", "val"]

# Allowed image extensions
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".JPG", ".JPEG", ".PNG"}


# ==========================
# HELPER FUNCTIONS
# ==========================

def list_files_with_ext(root: Path, allowed_exts):
    files = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in allowed_exts:
            files.append(p)
    return files


def list_images(split: str):
    split_dir = IMAGE_DIR / split
    if not split_dir.exists():
        print(f"[WARN] images/{split} does not exist.")
        return []
    return list_files_with_ext(split_dir, IMAGE_EXTS)


def list_labels(split: str):
    split_dir = LABEL_DIR / split
    if not split_dir.exists():
        print(f"[WARN] labels/{split} does not exist.")
        return []
    return list_files_with_ext(split_dir, {".txt"})


def base_name(path: Path):
    # File name without extension
    return path.stem


def check_yolo_label_file(label_path: Path):
    """
    Check that each line in a YOLO .txt file has:
    class_id x_center y_center width height
    with x,y,w,h in [0,1].
    Returns (is_valid, error_messages, classes_used)
    """
    errors = []
    classes = set()
    try:
        text = label_path.read_text().strip()
    except Exception as e:
        return False, [f"Could not read file: {e}"], classes

    if text == "":
        # Empty file is allowed (means no objects)
        return True, [], classes

    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        parts = line.strip().split()
        if len(parts) != 5:
            errors.append(f"Line {idx}: expected 5 values (class x y w h), got {len(parts)} – '{line}'")
            continue

        cls_str, x_str, y_str, w_str, h_str = parts
        # class id integer
        try:
            cls_id = int(cls_str)
            classes.add(cls_id)
        except ValueError:
            errors.append(f"Line {idx}: class id '{cls_str}' is not an integer.")
            continue

        # coordinates floats in [0,1]
        try:
            x = float(x_str)
            y = float(y_str)
            w = float(w_str)
            h = float(h_str)
        except ValueError:
            errors.append(f"Line {idx}: one of x,y,w,h is not a float – '{line}'")
            continue

        for name, val in [("x_center", x), ("y_center", y), ("width", w), ("height", h)]:
            if not (0.0 <= val <= 1.0):
                errors.append(f"Line {idx}: {name}={val} not in [0,1]. Line: '{line}'")

        if w <= 0.0 or h <= 0.0:
            errors.append(f"Line {idx}: width/height must be > 0. Got w={w}, h={h}. Line: '{line}'")

    is_valid = len(errors) == 0
    return is_valid, errors, classes


# ==========================
# MAIN CHECK LOGIC
# ==========================

def main():
    print("=== YOLO Dataset Verification ===")
    print(f"Dataset root: {DATASET_ROOT.resolve()}\n")

    if not DATASET_ROOT.exists():
        print("[FATAL] Dataset root does not exist. Check DATASET_ROOT path.")
        return

    global_classes = set()
    total_images = 0
    total_labels = 0

    for split in SPLITS:
        print(f"\n--- Checking split: {split} ---")
        images = list_images(split)
        labels = list_labels(split)

        # Build maps: base_name -> Path
        image_map = {base_name(p): p for p in images}
        label_map = {base_name(p): p for p in labels}

        img_basenames = set(image_map.keys())
        lbl_basenames = set(label_map.keys())

        # Stats
        print(f"Found {len(images)} image(s) in images/{split}")
        print(f"Found {len(labels)} label file(s) in labels/{split}")

        total_images += len(images)
        total_labels += len(labels)

        # 1) Images without labels
        imgs_without_labels = sorted(img_basenames - lbl_basenames)
        if imgs_without_labels:
            print(f"[WARN] {len(imgs_without_labels)} image(s) have NO matching label .txt:")
            for name in imgs_without_labels[:20]:   # print up to 20
                print(f"   - {image_map[name].relative_to(DATASET_ROOT)}")
            if len(imgs_without_labels) > 20:
                print(f"   ... and {len(imgs_without_labels) - 20} more.")
        else:
            print("[OK] Every image has a matching label file.")

        # 2) Labels without images
        labels_without_imgs = sorted(lbl_basenames - img_basenames)
        if labels_without_imgs:
            print(f"[WARN] {len(labels_without_imgs)} label file(s) have NO matching image:")
            for name in labels_without_imgs[:20]:
                print(f"   - {label_map[name].relative_to(DATASET_ROOT)}")
            if len(labels_without_imgs) > 20:
                print(f"   ... and {len(labels_without_imgs) - 20} more.")
        else:
            print("[OK] Every label file has a matching image.")

        # 3) Validate label contents
        bad_label_files = 0
        for name, lbl_path in label_map.items():
            is_valid, errors, classes = check_yolo_label_file(lbl_path)
            global_classes.update(classes)

            if not is_valid:
                bad_label_files += 1
                print(f"\n[ERROR] Invalid YOLO format in: {lbl_path.relative_to(DATASET_ROOT)}")
                for e in errors:
                    print("   -", e)

        if bad_label_files == 0:
            print("[OK] All label files in this split are valid YOLO format.")
        else:
            print(f"[WARN] {bad_label_files} label file(s) in {split} have issues (see errors above).")

    # Overall summary
    print("\n=== OVERALL SUMMARY ===")
    print(f"Total images across splits: {total_images}")
    print(f"Total label files across splits: {total_labels}")
    if global_classes:
        print(f"Classes used (by id) in labels: {sorted(global_classes)}")
    else:
        print("No classes found in any labels (all label files empty?).")

    print("\nVerification complete.")


if __name__ == "__main__":
    main()
