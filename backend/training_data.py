import json
import logging
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

LOG = logging.getLogger(__name__)


class SourceInUseError(Exception):
    pass


class TrainingData:
    def __init__(self, root: Path):
        self.root = root
        self._initialized = False
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()

    def _initialize(self, db):
        with self._lock:
            if self._initialized:
                return
            for folder in ("incoming", "media", "jobs"):
                (self.root / folder).mkdir(exist_ok=True)
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS source (
                    id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sample (
                    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id),
                    frame INTEGER NOT NULL, seconds REAL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, annotation TEXT
                );
                CREATE TABLE IF NOT EXISTS job (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, epochs INTEGER NOT NULL,
                    image_size INTEGER NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
                    summary TEXT, message TEXT, pid INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS single_training_job
                    ON job ((1)) WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS samples_by_source ON sample (source_id, frame);
                CREATE TABLE IF NOT EXISTS sample_sequence (
                    id INTEGER PRIMARY KEY CHECK (id = 1), next_id INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO sample_sequence
                    SELECT 1, coalesce(max(id), 0) + 1 FROM sample;
                CREATE TABLE IF NOT EXISTS job_source (
                    job_id TEXT NOT NULL REFERENCES job(id), source_id TEXT NOT NULL,
                    PRIMARY KEY (job_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_by_source ON job_source (source_id, job_id);
                CREATE TABLE IF NOT EXISTS deleted_files (
                    kind TEXT NOT NULL CHECK (kind IN ('source', 'dataset')), id TEXT NOT NULL,
                    PRIMARY KEY (kind, id)
                );
            """)
            self._migrate_job_sources(db)
            self._initialized = True

    def _migrate_job_sources(self, db):
        with db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] >= 1:
                return
            for (job_id,) in db.execute("SELECT id FROM job"):
                snapshot = self.root / "jobs" / job_id / "snapshot.json"
                try:
                    samples = json.loads(snapshot.read_text(encoding="utf-8"))
                    sources = {UUID(item["source_id"]).hex for item in samples}
                except (OSError, KeyError, ValueError, TypeError, AttributeError):
                    # A legacy job without provenance is treated as affected by any deletion.
                    continue
                db.executemany("INSERT OR IGNORE INTO job_source VALUES (?, ?)",
                               [(job_id, source_id) for source_id in sources])
            db.execute("PRAGMA user_version = 1")

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / "training.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            if not self._initialized:
                self._initialize(db)
            with db:
                yield db
        finally:
            db.close()

    def already_uploaded(self, digest):
        with self.connect() as db:
            row = db.execute("""
                SELECT source.id, count(sample.id) AS samples
                FROM source JOIN sample ON sample.source_id = source.id
                WHERE source.digest = ? GROUP BY source.id
            """, (digest,)).fetchone()
            return dict(row) if row else None

    def add_source(self, source_id, digest, name, kind, output, frames):
        destination = self.root / "media" / source_id
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("""
                SELECT source.id, count(sample.id) AS samples
                FROM source JOIN sample ON sample.source_id = source.id
                WHERE source.digest = ? GROUP BY source.id
            """, (digest,)).fetchone()
            if duplicate:
                return {**dict(duplicate), "duplicate": True}
            output.rename(destination)
            try:
                db.execute("INSERT INTO source VALUES (?, ?, ?, ?)", (source_id, digest, name, kind))
                first_id = db.execute("SELECT next_id FROM sample_sequence WHERE id = 1").fetchone()[0]
                db.execute("UPDATE sample_sequence SET next_id = next_id + ? WHERE id = 1", (len(frames),))
                db.executemany("""
                    INSERT INTO sample (id, source_id, frame, seconds, width, height)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [(sample_id, source_id, f["frame"], f["seconds"], f["width"], f["height"])
                      for sample_id, f in enumerate(frames, first_id)])
                db.commit()
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                raise
            return {"id": source_id, "samples": len(frames), "duplicate": False}

    def delete_source(self, source_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM source WHERE id = ?", (source_id,)).fetchone():
                return None
            affected = db.execute("""
                SELECT id, status FROM job
                WHERE EXISTS (
                    SELECT 1 FROM job_source WHERE job_id = job.id AND source_id = ?
                ) OR NOT EXISTS (
                    SELECT 1 FROM job_source WHERE job_id = job.id
                )
            """, (source_id,)).fetchall()
            if any(row["status"] in {"queued", "running"} for row in affected):
                raise SourceInUseError("학습 중인 자료입니다. 학습이 끝난 뒤 삭제해 주세요.")
            invalidated = [row["id"] for row in affected if row["status"] == "completed"]
            db.executemany("""
                UPDATE job SET status = 'needs_retraining',
                    message = '이 모델의 학습 또는 검증에 사용한 자료가 삭제되었습니다.'
                WHERE id = ?
            """, [(job_id,) for job_id in invalidated])
            removed = db.execute("DELETE FROM sample WHERE source_id = ?", (source_id,)).rowcount
            db.execute("DELETE FROM source WHERE id = ?", (source_id,))
            db.executemany("INSERT OR IGNORE INTO deleted_files VALUES (?, ?)",
                           [("source", source_id), *(("dataset", row["id"]) for row in affected)])
        return {"removed_samples": removed, "invalidated_models": len(invalidated)}

    def finish_removals(self):
        with self._cleanup_lock:
            pending = 0
            with self.connect() as db:
                tasks = db.execute("SELECT kind, id FROM deleted_files").fetchall()
            for kind, ident in tasks:
                try:
                    if UUID(ident).hex != ident:
                        raise ValueError("Expected an internal identifier")
                    path = self.root / ("media" if kind == "source" else "jobs") / ident
                    try:
                        shutil.rmtree(path if kind == "source" else path / "dataset")
                    except FileNotFoundError:
                        pass
                    if kind == "dataset":
                        (path / "snapshot.json").unlink(missing_ok=True)
                        (path / "dataset.yaml").unlink(missing_ok=True)
                except (OSError, ValueError):
                    LOG.warning("Training data cleanup will be retried for %s %s", kind, ident, exc_info=True)
                    pending += 1
                    continue
                with self.connect() as db:
                    db.execute("DELETE FROM deleted_files WHERE kind = ? AND id = ?", (kind, ident))
            return pending

    @staticmethod
    def _sample(row):
        item = dict(row)
        annotation = item.pop("annotation")
        item["boxes"] = json.loads(annotation) if annotation is not None else None
        return item

    def samples(self, view, offset, limit):
        filters = {
            "all": "1 = 1",
            "pending": "annotation IS NULL",
            "positive": "annotation IS NOT NULL AND annotation != '[]'",
            "negative": "annotation = '[]'",
        }
        conditions = filters[view]
        with self.connect() as db:
            db.execute("BEGIN")
            stats = dict(db.execute("""
                SELECT count(*) AS total, count(annotation) AS reviewed,
                    count(CASE WHEN annotation != '[]' THEN 1 END) AS positive
                FROM sample
            """).fetchone())
            data = db.execute(f"""
                SELECT sample.*, source.name, source.kind
                FROM sample JOIN source ON source.id = source_id
                WHERE {conditions} ORDER BY sample.id LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
            count = db.execute(f"SELECT count(*) FROM sample WHERE {conditions}").fetchone()[0]
        return {"items": [self._sample(row) for row in data], "count": count, "stats": stats}

    def label(self, sample_id, boxes):
        annotation = None if boxes is None else json.dumps(boxes, separators=(",", ":"))
        with self.connect() as db:
            return bool(db.execute(
                "UPDATE sample SET annotation = ? WHERE id = ?", (annotation, sample_id)
            ).rowcount)

    @staticmethod
    def image_name(frame, thumbnail=False):
        suffix = "-thumb" if thumbnail else ""
        return f"{frame:08d}{suffix}.jpg"

    def image_path(self, sample_id, thumbnail=False):
        with self.connect() as db:
            row = db.execute("SELECT source_id, frame FROM sample WHERE id = ?", (sample_id,)).fetchone()
        if row:
            return self.root / "media" / row["source_id"] / self.image_name(row["frame"], thumbnail)
        return None

    def reserve_job(self, job_id, epochs, image_size):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""
                SELECT id, source_id, frame, annotation
                FROM sample WHERE annotation IS NOT NULL ORDER BY id
            """).fetchall()
            snapshot = [self._sample(row) for row in rows]
            if sum(bool(row["boxes"]) for row in snapshot) < 2:
                raise ValueError("선박에 박스를 그린 이미지가 최소 2장 필요합니다.")
            db.execute("INSERT INTO job (id, status, epochs, image_size) VALUES (?, 'queued', ?, ?)",
                       (job_id, epochs, image_size))
            sources = {sample["source_id"] for sample in snapshot}
            db.executemany("INSERT INTO job_source VALUES (?, ?)",
                           [(job_id, source_id) for source_id in sources])
        return snapshot

    def update_job(self, job_id, **fields):
        if "summary" in fields:
            fields["summary"] = json.dumps(fields["summary"], allow_nan=False)
        with self.connect() as db:
            db.execute(
                "UPDATE job SET " + ", ".join(f"{key} = ?" for key in fields) + " WHERE id = ?",
                (*fields.values(), job_id),
            )

    def job(self, job_id=None):
        query = "SELECT * FROM job WHERE id = ?" if job_id else "SELECT * FROM job ORDER BY rowid DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, (job_id,) if job_id else ()).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row):
        if not row:
            return None
        result = dict(row)
        if result["summary"]:
            result["summary"] = json.loads(result["summary"])
        return result

    def overview(self):
        with self.connect() as db:
            db.execute("BEGIN")
            latest = db.execute("SELECT * FROM job ORDER BY rowid DESC LIMIT 1").fetchone()
            model = db.execute("""
                SELECT id, status FROM job
                WHERE status IN ('completed', 'needs_retraining') ORDER BY rowid DESC LIMIT 1
            """).fetchone()
            stale = db.execute("SELECT count(*) FROM job WHERE status = 'needs_retraining'").fetchone()[0]
        return self._job(latest), dict(model) if model else None, stale

    def fail_active_job(self, job_id, message):
        with self.connect() as db:
            db.execute("""
                UPDATE job SET status = 'failed', message = ?
                WHERE id = ? AND status IN ('queued', 'running')
            """, (message, job_id))

    def active_job(self):
        with self.connect() as db:
            row = db.execute("""
                SELECT id, pid, unixepoch() - unixepoch(created_at) AS age
                FROM job WHERE status IN ('queued', 'running')
            """).fetchone()
        return dict(row) if row else None


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)
