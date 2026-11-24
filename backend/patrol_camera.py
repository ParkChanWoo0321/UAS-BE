"""Read the simulator's ROS camera in the Docker run and archive boat sightings."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import sys
import time
from uuid import uuid4

import cv2


VIDEO_FPS = 8
VIDEO_SECONDS = 5
CAPTURE_INTERVAL = 12
POSITION_TIMEOUT = 4


class PatrolState:
    def __init__(self):
        self.buffer = b""
        self.position = None
        self.updated_at = 0
        self.closed = False
        self.interrupted = False

    def refresh(self):
        while select.select([sys.stdin], [], [], 0)[0]:
            chunk = os.read(sys.stdin.fileno(), 8192)
            if not chunk:
                self.closed = True
                return None
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                if message["position"] is None or message["time"] - self.updated_at >= POSITION_TIMEOUT:
                    self.interrupted = True
                self.position = message["position"]
                self.updated_at = message["time"]
        if time.time() - self.updated_at >= POSITION_TIMEOUT:
            self.interrupted = True
            return None
        return self.position


def write_details(folder, details):
    pending = folder / "details.tmp"
    pending.write_text(json.dumps(details, allow_nan=False), encoding="utf-8")
    pending.replace(folder / "details.json")


class CaptureArchive:
    def __init__(self, directory):
        self.directory = directory
        self.folder = None
        self.details = None
        self.writer = None
        self.frame_count = 0
        self.started = 0
        self.last_frame = 0
        self.next_capture = 0

    def start(self, image, stamp, received, position, detections):
        self.next_capture = stamp + CAPTURE_INTERVAL
        folder = self.directory / uuid4().hex
        folder.mkdir(parents=True)
        if not cv2.imwrite(str(folder / "photo.jpg"), image):
            raise OSError("Could not write patrol photo")
        details = {
            "captured_at": datetime.fromtimestamp(received, timezone.utc).isoformat(),
            "position": position,
            "boats": len(detections),
            "confidence": round(max(detections), 4),
            "detector": "yolo" if max(detections) > 0 else "simulation_image",
            "video_seconds": None,
        }
        write_details(folder, details)

        height, width = image.shape[:2]
        writer = cv2.VideoWriter(str(folder / "clip.tmp.webm"), cv2.VideoWriter_fourcc(*"VP80"),
                                 VIDEO_FPS, (width, height))
        if not writer.isOpened():
            writer.release()
            (folder / "clip.tmp.webm").unlink(missing_ok=True)
            print("Could not open VP8 video encoder", file=sys.stderr, flush=True)
            return
        self.folder, self.details, self.writer = folder, details, writer
        self.started, self.last_frame, self.frame_count = stamp, stamp, 1
        writer.write(image)

    def record(self, image, stamp):
        if stamp - self.last_frame >= 1 / VIDEO_FPS - 0.005:
            self.writer.write(image)
            self.frame_count += 1
            self.last_frame = stamp
        if stamp >= self.started + VIDEO_SECONDS:
            self.finish()

    def finish(self):
        if self.writer is None:
            return
        self.writer.release()
        self.writer = None
        pending = self.folder / "clip.tmp.webm"
        if self.frame_count >= 2 and pending.is_file() and pending.stat().st_size > 0:
            pending.replace(self.folder / "clip.webm")
            self.details["video_seconds"] = round(self.frame_count / VIDEO_FPS, 2)
            write_details(self.folder, self.details)
        else:
            pending.unlink(missing_ok=True)


def boat_detector(weights):
    from ultralytics import YOLO

    if not weights.is_file():
        raise FileNotFoundError(f"Patrol model weights not found: {weights}")
    model = YOLO(str(weights))
    source = Path(__file__).resolve().parent.parent
    stock_weights = {
        (source / "ChineseFishingVessel_ws/yolov8n.pt").resolve(),
        (Path.home() / "ChineseFishingVessel_ws/yolov8n.pt").resolve(),
    }
    fallback = None
    if weights.resolve() in stock_weights:
        if __package__:
            from .simulated_vessels import SimulatedVessels
        else:
            from simulated_vessels import SimulatedVessels
        fallback = SimulatedVessels()
    boats = [index for index, label in model.names.items() if label.strip().lower() == "boat"]
    if not boats:
        raise ValueError("Patrol model does not have a boat class")

    def detect(image):
        result = model.predict(image, classes=boats, conf=0.45, imgsz=640, device="cpu", verbose=False)[0]
        boxes = result.boxes.conf.tolist()
        if boxes:
            if fallback is not None:
                fallback.previous = ()
            return boxes
        return fallback.detect(image) if fallback is not None else []

    return detect


def capture(directory, weights, detector_factory=boat_detector):
    import rclpy
    from cv_bridge import CvBridge
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import Image

    state = PatrolState()
    archive = CaptureArchive(directory)
    bridge = CvBridge()
    rclpy.init()
    node = rclpy.create_node("maritime_patrol_camera")
    latest = None

    def receive(message):
        nonlocal latest
        latest = message, time.time()

    profile = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
    node.create_subscription(Image, "/camera", receive, profile)
    detect = None
    last_stamp = 0
    next_inference = 0
    try:
        while rclpy.ok() and not state.closed:
            rclpy.spin_once(node, timeout_sec=0.15)
            position = state.refresh()
            if state.interrupted:
                archive.finish()
                state.interrupted = False
            frame, latest = latest, None
            if position is None or frame is None:
                continue
            message, received = frame
            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9 or received
            if stamp < last_stamp:
                archive.finish()
                archive.next_capture = 0
            if stamp == last_stamp:
                continue
            last_stamp = stamp
            if archive.writer is None and (stamp < archive.next_capture or time.monotonic() < next_inference):
                continue
            image = bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
            if archive.writer is not None:
                archive.record(image, stamp)
                continue
            try:
                if detect is None:
                    detect = detector_factory(weights)
                detections = detect(image)
            finally:
                next_inference = time.monotonic() + 0.7
            # The run may have left patrol while inference was running.
            if detections and state.refresh() is not None and not state.interrupted and received >= time.time() - POSITION_TIMEOUT:
                archive.start(image, stamp, received, position, detections)
    finally:
        archive.finish()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("run_id", type=int)
    parser.add_argument("weights", type=Path)
    args = parser.parse_args()
    capture(args.directory / str(args.run_id), args.weights)
