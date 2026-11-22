import json
import logging
import os
from pathlib import Path
import select
import subprocess
import threading
import time

import pymysql

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent


class ControlError(Exception):
    pass


class Simulator:
    def __init__(self, store, launcher=None, setup=None):
        self.store = store
        self.launcher = Path(launcher or Path.home() / ".local/bin/maritime")
        self.setup = Path(setup or Path.home() / "maritime-setup")
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

    def _operate(self, run, resume):
        worker = None
        failure = None
        owned_logs = run["log_dir"] if resume else None
        try:
            if not resume:
                started = subprocess.run(
                    [self.launcher, "start", "--headless"], capture_output=True, text=True, timeout=70
                )
                if started.returncode or "Simulation started with sea_mission." not in started.stdout:
                    detail = (started.stderr or started.stdout).strip()
                    raise ControlError(f"시뮬레이터 시작 실패 · {detail[-350:]}" if detail else "시뮬레이터 시작 실패")
                owned_logs = self._marker()
                if not owned_logs:
                    raise ControlError("시뮬레이터의 실행 기록 경로를 확인하지 못했어요.")
                with self.guard:
                    run.update(phase="waiting", log_dir=owned_logs)
                    self._persist()
            if self.stop_requested.is_set():
                return
            if self._marker() != owned_logs:
                raise ControlError("시뮬레이터 실행이 바뀌었어요.")
            args = [self.launcher, "exec", "python3", str(ROOT / "ros_agent.py"),
                    str(run["north"]), str(run["east"]), str(run["altitude"])]
            if resume:
                args.append("--observe")
            with (Path(owned_logs) / "console.log").open("ab") as diagnostic:
                worker = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostic
                )
                with self.guard:
                    self.worker = worker
                self._listen(worker)
        except (ControlError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Simulator run %s ended: %s", run["id"], exc)
            failure = str(exc)[:550]
        finally:
            if owned_logs and self._marker() == owned_logs:
                try:
                    subprocess.run([self.launcher, "stop"], capture_output=True, timeout=20, check=True)
                    if self._marker() == owned_logs:
                        failure = failure or "시뮬레이터 종료를 확인하지 못했어요."
                except (OSError, subprocess.SubprocessError):
                    failure = failure or "시뮬레이터 종료 요청에 실패했어요."
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

    def _listen(self, worker):
        buffer = b""
        while not self.stop_requested.is_set():
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
                    self._apply(event["data"])

    def _apply(self, data):
        with self.guard:
            if self.stop_requested.is_set():
                return
            previous = self.current["phase"]
            self.last_update = time.monotonic() if data.get("fresh") else 0.0
            if data.get("return_response"):
                self.return_pending = False
            self.current.update(phase=data["phase"], telemetry=data, message=data.get("message"))
            if previous != data["phase"] or time.monotonic() - self.last_persisted >= 4:
                self._persist()
