import argparse
import math
import os
import random
import shutil
import traceback
from collections import defaultdict
from pathlib import Path

from .training_data import TrainingData, write_json

MAX_VIDEO_FRAMES = 32


def save_frame(image, frame, seconds, output):
    if min(image.size) < 16:
        raise ValueError("Image is smaller than 16 pixels on its shortest side")
    image.thumbnail((1920, 1920))
    width, height = image.size
    image.save(output / TrainingData.image_name(frame), quality=90)
    image.thumbnail((260, 260))
    image.save(output / TrainingData.image_name(frame, thumbnail=True), quality=82)
    return {"frame": frame, "seconds": seconds, "width": width, "height": height}


def prepare_media(source, kind, output):
    from PIL import Image, ImageOps

    output.mkdir()
    Image.MAX_IMAGE_PIXELS = 50_000_000
    if kind == "image":
        with Image.open(source, formats=["JPEG", "PNG", "WEBP", "BMP", "TIFF"]) as image:
            if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Image resolution is too large")
            frames = [save_frame(ImageOps.exif_transpose(image).convert("RGB"), 0, None, output)]
    else:
        import cv2

        cv2.setNumThreads(1)
        stream = cv2.VideoCapture(str(source))
        frames = []
        try:
            if not stream.isOpened():
                raise ValueError("Could not open the video")
            fps = stream.get(cv2.CAP_PROP_FPS)
            if not 0 < fps <= 300:
                fps = 30
            count = stream.get(cv2.CAP_PROP_FRAME_COUNT)
            count = int(count) if math.isfinite(count) and count > 0 else 0
            if count:
                samples = min(MAX_VIDEO_FRAMES, max(1, math.ceil(count / max(1, fps / 2))))
                positions = sorted({round((count - 1) * i / max(1, samples - 1)) for i in range(samples)})
                for index in positions:
                    stream.set(cv2.CAP_PROP_POS_FRAMES, index)
                    ok, bgr = stream.read()
                    if ok:
                        rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                        frames.append(save_frame(rgb, index, round(index / fps, 2), output))
            else:
                interval = max(1, round(fps))
                for index in range(MAX_VIDEO_FRAMES * interval):
                    ok = stream.grab()
                    if not ok:
                        break
                    if index % interval == 0:
                        ok, bgr = stream.retrieve()
                        if ok:
                            rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                            frames.append(save_frame(rgb, index, round(index / fps, 2), output))
        finally:
            stream.release()
        if not frames:
            raise ValueError("The video did not contain readable frames")
    write_json(output / "frames.json", frames)


def split_samples(samples):
    by_source = defaultdict(list)
    for sample in samples:
        by_source[sample["source_id"]].append(sample)
    positive_sources = [key for key, group in by_source.items() if any(item["boxes"] for item in group)]
    desired = max(1, round(len(samples) * 0.2))
    if len(positive_sources) == 1:
        # Use a time-ordered holdout if all positive examples come from one video.
        group = sorted(by_source[positive_sources[0]], key=lambda sample: sample["frame"])
        positives = [i for i, sample in enumerate(group) if sample["boxes"]]
        cut = min(max(len(group) - desired, positives[0] + 1), positives[-1])
        validation_ids = {sample["id"] for sample in group[cut:]}
        method = "single_video"
    else:
        rng = random.Random(42)
        rng.shuffle(positive_sources)
        first = min(positive_sources, key=lambda key: abs(len(by_source[key]) - desired))
        first_train = next(key for key in positive_sources if key != first)
        val_sources = {first}
        size = len(by_source[first])
        remaining = [key for key in by_source if key not in {first, first_train}]
        rng.shuffle(remaining)
        for key in remaining:
            count = len(by_source[key])
            if abs(size + count - desired) < abs(size - desired):
                val_sources.add(key)
                size += count
        validation_ids = {sample["id"] for key in val_sources for sample in by_source[key]}
        method = "source"
    train, validation = [], []
    for sample in samples:
        (validation if sample["id"] in validation_ids else train).append(sample)
    return train, validation, method


def make_dataset(store, folder, samples):
    train, validation, method = split_samples(samples)
    dataset = folder / "dataset"
    for name, selected in (("train", train), ("val", validation)):
        images = dataset / name / "images"
        labels = dataset / name / "labels"
        images.mkdir(parents=True)
        labels.mkdir()
        for sample in selected:
            ident = str(sample["id"])
            source = store.root / "media" / sample["source_id"] / store.image_name(sample["frame"])
            destination = images / (ident + ".jpg")
            try:
                os.link(source, destination)
            except OSError:
                shutil.copyfile(source, destination)
            lines = []
            for box in sample["boxes"]:
                x, y, width, height = (box[k] for k in ("x", "y", "width", "height"))
                lines.append(f"0 {x + width / 2:.8f} {y + height / 2:.8f} {width:.8f} {height:.8f}\n")
            (labels / (ident + ".txt")).write_text("".join(lines), encoding="utf-8")
    config = folder / "dataset.yaml"
    # JSON is valid YAML and avoids a dependency in the API process.
    write_json(config, {"path": str(dataset), "train": "train/images", "val": "val/images", "names": {0: "boat"}})
    return config, {"train": len(train), "validation": len(validation), "split": method}


def train_model(root, job_id, weights):
    import json

    store = TrainingData(root)
    folder = root / "jobs" / job_id
    store.update_job(job_id, status="running", pid=os.getpid())
    try:
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        os.environ.setdefault("YOLO_OFFLINE", "true")
        os.environ.setdefault("YOLO_CONFIG_DIR", str(root / "config"))
        os.environ.setdefault("MPLCONFIGDIR", str(root / "mpl"))
        import torch
        from matplotlib import get_data_path
        from ultralytics import YOLO, settings
        from ultralytics.utils import USER_CONFIG_DIR
        from ultralytics.utils.checks import FONT

        settings.update({name: False for name in (
            "sync", "clearml", "comet", "dvc", "mlflow", "raytune", "tensorboard", "wandb"
        )})
        # The detector checks for this font even when plots are disabled.
        local_font = USER_CONFIG_DIR / FONT
        if not local_font.is_file():
            shutil.copyfile(Path(get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf", local_font)
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        job = store.job(job_id)
        snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
        config, summary = make_dataset(store, folder, snapshot)
        store.update_job(job_id, summary=summary)
        model = YOLO(str(weights), task="detect")

        def report(trainer):
            metrics = {}
            for label, key in (("precision", "metrics/precision(B)"), ("recall", "metrics/recall(B)"),
                               ("map50", "metrics/mAP50(B)"), ("map5095", "metrics/mAP50-95(B)")):
                value = (trainer.metrics or {}).get(key)
                if value is not None and math.isfinite(float(value)):
                    metrics[label] = float(value)
            summary["metrics"] = metrics
            store.update_job(job_id, epoch=trainer.epoch + 1, summary=summary)

        model.add_callback("on_fit_epoch_end", report)
        model.train(data=str(config), epochs=job["epochs"], imgsz=job["image_size"],
                    device="cpu", workers=0, batch=4, amp=False, plots=False, verbose=False,
                    project=str(folder), name="output", exist_ok=True, seed=42)
        artifact = folder / "output" / "weights" / "best.pt"
        if not artifact.is_file() or not artifact.stat().st_size:
            raise RuntimeError("YOLO did not produce best.pt")
        store.update_job(job_id, status="completed", epoch=job["epochs"], summary=summary)
    except Exception:
        traceback.print_exc()
        store.update_job(job_id, status="failed", message="학습을 완료하지 못했습니다. 학습 로그를 확인해 주세요.")
        raise


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("kind", choices=["image", "video"])
    prepare.add_argument("output", type=Path)
    train = commands.add_parser("train")
    train.add_argument("root", type=Path)
    train.add_argument("job")
    train.add_argument("weights", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare_media(args.source, args.kind, args.output)
    else:
        train_model(args.root, args.job, args.weights)


if __name__ == "__main__":
    main()
