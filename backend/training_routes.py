import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .training_data import SourceInUseError, TrainingData, write_json

BACKEND = Path(__file__).resolve().parent
PROJECT = BACKEND.parent
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".avi", ".webm", ".mkv"}
IMAGE_LIMIT = 32 * 1024**2
VIDEO_LIMIT = 512 * 1024**2


class Box(BaseModel):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def inside_image(self):
        if self.x + self.width > 1.0000001 or self.y + self.height > 1.0000001:
            raise ValueError("라벨 박스는 이미지 안에 있어야 합니다.")
        return self


class Annotation(BaseModel):
    boxes: Annotated[list[Box] | None, Field(max_length=300)]


class TrainingOptions(BaseModel):
    epochs: Annotated[int, Field(ge=1, le=100)] = 20
    image_size: Literal[320, 640] = 640


class TrainingService:
    def __init__(self):
        default_dir = BACKEND / ".training-data"
        default_python = BACKEND / ".training-venv" / "bin" / "python"
        default_weights = PROJECT / "ChineseFishingVessel_ws" / "yolov8n.pt"
        self.store = TrainingData(Path(os.environ.get("MARITIME_TRAINING_DIR", default_dir)).resolve())
        self.python = Path(os.environ.get("MARITIME_TRAINING_PYTHON", default_python)).absolute()
        self.weights = Path(os.environ.get("MARITIME_TRAINING_WEIGHTS", default_weights)).resolve()
        self.upload_slots = asyncio.Semaphore(4)
        self._readiness_lock = threading.Lock()
        self._readiness = (0.0, False, False)

    def _command(self, *args):
        return [str(self.python), "-m", "backend.training_worker", *(str(arg) for arg in args)]

    def _incoming(self):
        with self.store.connect():
            return self.store.root / "incoming"

    def ready(self):
        return self.runtime()[0]

    def active_model(self, model_job):
        if model_job and model_job["status"] == "completed":
            path = self.store.root / "jobs" / model_job["id"] / "output" / "weights" / "best.pt"
            if path.is_file():
                return {"kind": "trained", "filename": path.name, "job_id": model_job["id"],
                        "needs_retraining": False, "available": True}, path
        return {"kind": "base", "filename": self.weights.name, "job_id": None,
                "needs_retraining": bool(model_job), "available": self.weights.is_file()}, self.weights

    def current_weights(self):
        _, model_job, _ = self.store.overview()
        return self.active_model(model_job)[1]

    def runtime(self):
        with self._readiness_lock:
            checked_at, media_ready, training_ready = self._readiness
            if checked_at and time.monotonic() - checked_at < 10:
                return media_ready, training_ready
            media_ready = training_ready = False
            if self.python.is_file() and os.access(self.python, os.X_OK):
                try:
                    probe = subprocess.run(
                        [str(self.python), "-c", "from importlib.util import find_spec; "
                         "print(*(int(find_spec(name) is not None) for name in ('PIL','cv2','torch','ultralytics')))"],
                        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                    )
                    pil, cv, torch, yolo = probe.stdout.strip().split()
                    media_ready = pil == cv == b"1"
                    training_ready = media_ready and torch == yolo == b"1"
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
            self._readiness = (time.monotonic(), media_ready, training_ready)
            return media_ready, training_ready

    def _decode(self, original, kind, dest):
        try:
            result = subprocess.run(self._command("prepare", original, kind, dest), cwd=PROJECT,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=240 if kind == "video" else 30)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(422, "파일 처리 시간이 초과됐습니다.") from exc
        if result.returncode:
            if b"ModuleNotFoundError" in result.stderr or b"ImportError" in result.stderr:
                raise HTTPException(503, "학습 환경에 사진·영상 처리 패키지가 설치되어 있지 않습니다.")
            raise HTTPException(415, "파일을 읽을 수 없습니다. 형식이나 영상 코덱을 확인해 주세요.")
        return json.loads((dest / "frames.json").read_text(encoding="utf-8"))

    async def upload(self, request, name):
        if not await run_in_threadpool(self.ready):
            raise HTTPException(503, "백엔드에 학습 환경을 먼저 설치해 주세요.")
        # Use the original name only as display metadata.
        name = "".join(c for c in name.replace("\\", "/").rsplit("/", 1)[-1] if c.isprintable())
        suffix = Path(name).suffix.lower()
        if suffix in IMAGE_TYPES:
            kind, limit = "image", IMAGE_LIMIT
        elif suffix in VIDEO_TYPES:
            kind, limit = "video", VIDEO_LIMIT
        else:
            raise HTTPException(415, "지원하는 사진 또는 영상 파일을 선택해 주세요.")
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
        async with self.upload_slots:
            incoming = await run_in_threadpool(self._incoming)
            with tempfile.TemporaryDirectory(dir=incoming) as temporary:
                folder = Path(temporary)
                original = folder / ("upload" + suffix)
                digest = hashlib.sha256()
                size = 0
                with original.open("wb") as buffer:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
                        digest.update(chunk)
                        buffer.write(chunk)
                if not size:
                    raise HTTPException(400, "내용이 없는 파일입니다.")
                checksum = digest.hexdigest()
                duplicate = await run_in_threadpool(self.store.already_uploaded, checksum)
                if duplicate:
                    return {**duplicate, "duplicate": True}
                dest = folder / "prepared"
                frames = await run_in_threadpool(self._decode, original, kind, dest)
                return await run_in_threadpool(
                    self.store.add_source, uuid4().hex, checksum, name[:255], kind, dest, frames
                )

    def reconcile_job(self):
        active = self.store.active_job()
        if not active or active["age"] is None or active["age"] < 45:
            return
        pid = active["pid"]
        if pid:
            try:
                args = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\x00")
                if b"backend.training_worker" in args and active["id"].encode() in args:
                    return
            except OSError:
                pass
        self.store.fail_active_job(active["id"], "학습 프로세스가 중단됐습니다. 다시 실행해 주세요.")

    def _reap(self, process, job_id):
        process.wait()
        self.store.fail_active_job(job_id, "학습이 중단됐습니다. 학습 로그를 확인해 주세요.")

    def start(self, options):
        if not self.runtime()[1] or not self.weights.is_file():
            raise HTTPException(503, "학습 환경과 YOLO 기본 가중치 파일을 확인해 주세요.")
        self.reconcile_job()
        job_id = uuid4().hex
        try:
            snapshot = self.store.reserve_job(job_id, options.epochs, options.image_size)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "학습이 이미 실행 중입니다.") from exc
        folder = self.store.root / "jobs" / job_id
        try:
            folder.mkdir()
            write_json(folder / "snapshot.json", snapshot)
            with (folder / "training.log").open("wb") as log:
                process = subprocess.Popen(self._command("train", self.store.root, job_id, self.weights),
                                           cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            self.store.update_job(job_id, pid=process.pid)
        except OSError as exc:
            self.store.update_job(job_id, status="failed", message="학습 프로세스를 시작하지 못했습니다.")
            raise HTTPException(503, "학습 프로세스를 시작하지 못했습니다.") from exc
        threading.Thread(target=self._reap, args=(process, job_id), daemon=True).start()
        return self.store.job(job_id)


def create_training_router(require_write_access, service=None):
    service = service or TrainingService()
    store = service.store
    router = APIRouter(prefix="/api/training", tags=["training"])
    write = [Depends(require_write_access)]

    @router.get("/samples")
    def samples(view: Literal["all", "pending", "positive", "negative"] = "all",
                offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 24):
        return store.samples(view, offset, limit)

    @router.post("/media", status_code=201, dependencies=write)
    async def upload(request: Request, name: Annotated[str, Query(min_length=1, max_length=512)]):
        return await service.upload(request, name)

    @router.delete("/media/{source_id}", dependencies=write)
    def delete_media(source_id: UUID):
        service.reconcile_job()
        try:
            result = store.delete_source(source_id.hex)
        except SourceInUseError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "이미 삭제되었거나 학습 자료를 찾을 수 없습니다.")
        return {**result, "cleanup_pending": bool(store.finish_removals())}

    @router.get("/samples/{sample_id}/image")
    def image(sample_id: int, thumbnail: bool = False):
        path = store.image_path(sample_id, thumbnail)
        if not path or not path.is_file():
            raise HTTPException(404, "학습 이미지를 찾을 수 없습니다.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.put("/samples/{sample_id}/annotation", dependencies=write)
    def label(sample_id: int, annotation: Annotation):
        boxes = None if annotation.boxes is None else [box.model_dump() for box in annotation.boxes]
        if not store.label(sample_id, boxes):
            raise HTTPException(404, "학습 이미지를 찾을 수 없습니다.")
        return {"id": sample_id, "boxes": boxes}

    @router.get("/jobs/latest")
    def latest():
        service.reconcile_job()
        store.finish_removals()
        job, model_job, stale = store.overview()
        model, _ = service.active_model(model_job)
        media_ready, training_ready = service.runtime()
        return {"job": job, "model": model, "invalidated_models": stale, "media_ready": media_ready,
                "weights_ready": training_ready and service.weights.is_file()}

    @router.get("/model/weights")
    def current_weights():
        _, model_job, _ = store.overview()
        model, path = service.active_model(model_job)
        if not model["available"]:
            raise HTTPException(404, "현재 모델 파일을 찾을 수 없습니다.")
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})

    @router.post("/jobs", status_code=202, dependencies=write)
    def start(options: TrainingOptions):
        return service.start(options)

    @router.get("/jobs/{job_id}/files/{kind}")
    def artifact(job_id: UUID, kind: Literal["weights", "report", "log"]):
        job = store.job(job_id.hex)
        names = {"weights": "output/weights/best.pt", "report": "output/results.csv", "log": "training.log"}
        path = store.root / "jobs" / job_id.hex / names[kind]
        if job and kind == "weights" and job["status"] == "needs_retraining":
            raise HTTPException(410, "삭제한 자료로 학습한 모델입니다. 남은 자료로 다시 학습해 주세요.")
        if not job or (kind != "log" and job["status"] not in {"completed", "needs_retraining"}) or not path.is_file():
            raise HTTPException(404, "학습 결과 파일을 찾을 수 없습니다.")
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})

    return router
