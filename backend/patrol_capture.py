import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time


LOG = logging.getLogger(__name__)
CAMERA = Path(__file__).with_name("patrol_camera.py").resolve()


def patrol_position(telemetry):
    if telemetry["phase"] != "patrol" or not all(telemetry.get(key) for key in (
        "fresh", "position_valid", "armed", "fixed_wing",
    )) or telemetry.get("transitioning"):
        return None
    position = {axis: telemetry.get(axis) for axis in ("north", "east", "altitude")}
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in position.values()):
        return None
    return position


class PatrolCapture:
    def __init__(self, launcher, directory, run_id, log_dir, *, model=None):
        self.launcher = launcher
        self.directory = directory
        self.run_id = run_id
        self.log_dir = Path(log_dir)
        self.model = model or self._base_weights
        self.weights = None
        self.process = None
        self.retry_at = 0.0
        self.checked_at = 0.0

    @staticmethod
    def _base_weights():
        installed = Path.home() / "ChineseFishingVessel_ws/yolov8n.pt"
        bundled = CAMERA.parent.parent / "ChineseFishingVessel_ws/yolov8n.pt"
        return Path(os.environ.get("MARITIME_TRAINING_WEIGHTS") or (bundled if bundled.is_file() else installed)).resolve()

    def _start(self):
        with (self.log_dir / "patrol-camera.log").open("ab") as log:
            self.process = subprocess.Popen(
                [self.launcher, "exec", "python3", "-B", CAMERA, str(self.directory), str(self.run_id), str(self.weights)],
                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, bufsize=0,
            )
        os.set_blocking(self.process.stdin.fileno(), False)

    def update(self, telemetry, enabled=True):
        position = patrol_position(telemetry) if enabled else None
        if self.process is None and position is None:
            return
        now = time.monotonic()
        try:
            if not self.checked_at or now - self.checked_at >= 1:
                weights = self.model()
                if weights != self.weights:
                    self.close()
                    self.weights = weights
                    self.retry_at = 0
                self.checked_at = now
            if self.process is None:
                if position is None or now < self.retry_at:
                    return
                self._start()
            if self.process.poll() is not None:
                raise OSError(f"Patrol camera exited with {self.process.returncode}")
            self.process.stdin.write(
                json.dumps({"position": position, "time": time.time()}, allow_nan=False).encode() + b"\n"
            )
        except BlockingIOError:
            pass
        except (OSError, sqlite3.Error):
            self.close()
            if now >= self.retry_at:
                LOG.warning("Patrol capture unavailable for run %s (see patrol-camera.log)", self.run_id, exc_info=True)
            self.retry_at = now + 5

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        process.stdin.close()
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
