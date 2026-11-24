from datetime import datetime
import json
import logging
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import time

import pymysql

from .desktop_views import open_desktop_views
from .patrol_capture import PatrolCapture

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
FLIGHT_STEPS = {"takeoff", "cruise", "patrol", "returning", "landed"}


class ControlError(Exception):
    pass


class Simulator:
    def __init__(self, store, launcher=None, setup=None, *, capture_directory=None, capture_enabled=None, capture_weights=None):
        self.store = store
        self.launcher = Path(launcher or Path.home() / ".local/bin/maritime")
        self.setup = Path(setup or Path.home() / "maritime-setup")
        self.capture_directory = Path(capture_directory or self.setup / "captures")
        self.capture_enabled = capture_enabled
        self.capture_weights = capture_weights
        self.guard = threading.RLock()
        self.current = None
        self.thread = None
        self.worker = None
        self.stop_requested = threading.Event()
        self.bootstrapped = False
        self.last_update = 0.0
        self.last_persisted = 0.0
        self.return_pending = False

    def snapshot(self):
        with self.guard:
            try:
                self._bootstrap()
                history = self.store.recent()
                database = True
            except pymysql.MySQLError:
                history, database = [], False
            run = dict(self.current) if self.current else None
            if run:
                run["connected"] = self._telemetry_is_current()
                run["can_return"] = self._can_return()
                run["return_pending"] = self.return_pending
            return {
                "database": database,
                "launcher": self.launcher.is_file(),
                "external_run": bool(self._marker() and not (run and run["active"])),
                "run": run,
                "history": history,
            }

    def start(self, target):
        with self.guard:
            self._bootstrap()
            if (self.current and self.current["active"]) or self._marker():
                raise ControlError("진행 중인 시뮬레이션을 종료한 뒤 다시 실행해 주세요.")
            if not self.launcher.is_file():
                raise ControlError("설치된 maritime 실행 도구를 찾을 수 없어요.")
            run = self.store.create(target)
            self._spawn(run, resume=False)
            return dict(run)

    def return_to_launch(self, run_id):
        with self.guard:
            run = self._owned(run_id)
            if not self._can_return():
                raise ControlError("시동과 위치 상태가 확인되면 귀환을 요청할 수 있어요.")
            if self._marker() != run["log_dir"]:
                raise ControlError("현재 시뮬레이션을 확인할 수 없어요.")
            try:
                self.worker.stdin.write(b'{"action":"return"}\n')
                self.worker.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ControlError("PX4 연결이 끊겼어요. 실행 상태를 확인해 주세요.") from exc
            self.return_pending = True

    def stop(self, run_id):
        with self.guard:
            self._owned(run_id)
            self.stop_requested.set()
            if self.current["phase"] != "landed":
                self.current["phase"] = "stopping"
            self._persist()

    def close(self):
        with self.guard:
            self.stop_requested.set()
            thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=25)

    def _bootstrap(self):
        if self.bootstrapped:
            return
        self.store.ensure_table()
        run = self.store.active()
        if run:
            if run["log_dir"] and self._marker() == run["log_dir"]:
                self._spawn(run, resume=True)
            else:
                run.update(phase="interrupted", active=False, message="이전 시뮬레이션 연결이 종료됐어요.")
                self.store.update(run)
        self.bootstrapped = True

    def _owned(self, run_id):
        if not self.current or self.current["id"] != run_id or not self.current["active"]:
            raise ControlError("현재 UI에서 관리 중인 실행을 찾을 수 없어요.")
        return self.current

    def _marker(self):
        try:
            return (self.setup / "active-run").read_text().strip() or None
        except FileNotFoundError:
            return None

    def _spawn(self, run, resume):
        self.current = run
        self.stop_requested = threading.Event()
        self.last_update = self.last_persisted = 0.0
        self.return_pending = False
        self.thread = threading.Thread(target=self._operate, args=(run, resume), daemon=True)
        self.thread.start()

    def _telemetry_is_current(self):
        return bool(self.current and self.current["active"] and time.monotonic() - self.last_update < 7)

    def _can_return(self):
        telemetry = (self.current or {}).get("telemetry") or {}
        return bool(
            self.worker and self._telemetry_is_current() and telemetry.get("can_return")
            and not self.return_pending and not self.stop_requested.is_set()
        )

    def _persist(self):
        try:
            self.store.update(self.current)
            self.last_persisted = time.monotonic()
        except pymysql.MySQLError:
            LOG.exception("Could not persist simulator run %s.", self.current["id"])

    def _claim_run(self):
        runs = self.setup / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=runs))
        marker = directory / "owner"
        marker.write_text(f"{directory}\n", encoding="utf-8")
        try:
            os.link(marker, self.setup / "active-run")
        except FileExistsError as exc:
            raise ControlError("다른 시뮬레이션이 이미 실행 중이에요.") from exc
        finally:
            marker.unlink()
        return str(directory)

    def _open_desktop(self, logs, ready):
        ground_control = None
        try:
            if self._marker() == logs and not self.stop_requested.is_set():
                ground_control = open_desktop_views(self.launcher, logs)
        finally:
            if self._marker() == logs:
                ready.touch()
        if ground_control is not None:
            ground_control.wait()

    def _operate(self, run, resume):
        worker = runtime = capture = None
        failure = None
        owned_logs = run["log_dir"] if resume else None
        try:
            if not resume:
                owned_logs = self._claim_run()
                with self.guard:
                    run.update(phase="waiting", log_dir=owned_logs)
                    self._persist()
                prepared = subprocess.run([self.launcher, "exec", "true"], capture_output=True, text=True, timeout=35)
                if prepared.returncode:
                    raise ControlError((prepared.stderr or prepared.stdout).strip()[-550:] or "Docker 준비 실패")
                with (Path(owned_logs) / "runtime.log").open("ab") as log:
                    runtime = subprocess.Popen(
                        [self.launcher, "exec", "python3", "-B", str(ROOT / "sim_runtime.py"), owned_logs],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    )
            if self.stop_requested.is_set():
                return
            if self._marker() != owned_logs:
                raise ControlError("시뮬레이터 실행이 바뀌었어요.")
            args = [self.launcher, "exec", "python3", str(ROOT / "ros_agent.py"),
                    str(run["north"]), str(run["east"]), str(run["altitude"])]
            desktop_ready = None
            if resume:
                args.append("--observe")
            elif os.environ.get("MARITIME_DESKTOP_VIEWS", "1") != "0":
                desktop_ready = Path(owned_logs) / "desktop.ready"
                args.extend(["--desktop-ready", str(desktop_ready)])
            capture = PatrolCapture(self.launcher, self.capture_directory, run["id"], owned_logs,
                                    model=self.capture_weights)
            with (Path(owned_logs) / "console.log").open("ab") as diagnostic:
                worker = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostic
                )
                with self.guard:
                    self.worker = worker
                self._listen(worker, capture, runtime, owned_logs, desktop_ready)
        except (ControlError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Simulator run %s ended: %s", run["id"], exc)
            failure = str(exc)[:550]
        finally:
            if capture is not None:
                capture.close()
            if owned_logs and self._marker() == owned_logs:
                try:
                    subprocess.run([self.launcher, "stop"], capture_output=True, timeout=20, check=True)
                    if self._marker() == owned_logs:
                        failure = failure or "시뮬레이터 종료를 확인하지 못했어요."
                except (OSError, subprocess.SubprocessError):
                    failure = failure or "시뮬레이터 종료 요청에 실패했어요."
            if runtime is not None and runtime.poll() is None:
                runtime.terminate()
                try:
                    runtime.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    runtime.kill()
                    runtime.wait()
            if worker is not None:
                if worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait()
                worker.stdin.close()
                worker.stdout.close()
            with self.guard:
                self.worker = None
                self.return_pending = False
                if failure:
                    run.update(phase="failed", message=failure)
                elif run["phase"] != "landed":
                    run.update(phase="stopped", message="시뮬레이션을 종료했어요.")
                run["active"] = False
                self._persist()

    def _listen(self, worker, capture, runtime, logs, desktop_ready):
        buffer = b""
        show_desktop = desktop_ready is not None
        while not self.stop_requested.is_set():
            if runtime is not None and runtime.poll() is not None:
                raise ControlError("시뮬레이터가 종료됐어요. runtime.log와 구성 요소 로그를 확인해 주세요.")
            if not select.select([worker.stdout], [], [], 0.5)[0]:
                continue
            chunk = os.read(worker.stdout.fileno(), 8192)
            if not chunk:
                if self.stop_requested.is_set():
                    return
                raise ControlError("ROS 2 연결이 종료됐어요. 실행 로그의 console.log를 확인해 주세요.")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "error":
                    raise ControlError(event["message"])
                if event.get("type") == "telemetry":
                    telemetry = event["data"]
                    self._apply(telemetry)
                    if show_desktop and telemetry.get("position_valid") and not self.stop_requested.is_set():
                        show_desktop = False
                        threading.Thread(target=self._open_desktop, args=(logs, desktop_ready), daemon=True).start()
                    if not self.stop_requested.is_set():
                        enabled = self.capture_enabled is None or self.capture_enabled()
                        capture.update(telemetry, enabled)

    def _apply(self, data):
        with self.guard:
            if self.stop_requested.is_set():
                return
            previous = self.current["phase"]
            observed = set((self.current["telemetry"] or {}).get("observed_phases", ()))
            if data["phase"] in FLIGHT_STEPS:
                observed.add(data["phase"])
            if data["fixed_wing"] and not data["transitioning"]:
                observed.add("transition")
            data["observed_phases"] = sorted(observed)
            self.last_update = time.monotonic() if data.get("fresh") else 0.0
            if data.get("return_response"):
                self.return_pending = False
            self.current.update(phase=data["phase"], telemetry=data, message=data.get("message"))
            if previous != data["phase"] or time.monotonic() - self.last_persisted >= 4:
                self._persist()
