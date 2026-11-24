from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import threading
from uuid import UUID


LOG = logging.getLogger(__name__)
CHUNK_SIZE = 256 * 1024
PHOTO, VIDEO = 0, 1


class CaptureStore:
    def __init__(self, database, spool):
        self.database = database
        self.spool = Path(spool)
        self._lock = threading.Lock()
        self._initialized = False
        self._enabled = False
        self._imported = None

    def initialize(self):
        with self._lock:
            if self._initialized:
                return
            with self.database.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS maritime_console_captures (
                        run_id BIGINT UNSIGNED NOT NULL,
                        id CHAR(32) CHARACTER SET ascii NOT NULL,
                        captured_at DATETIME(6) NOT NULL,
                        north DOUBLE NOT NULL, east DOUBLE NOT NULL, altitude DOUBLE NOT NULL,
                        boats INT UNSIGNED NOT NULL, confidence DOUBLE NOT NULL,
                        photo_size BIGINT UNSIGNED NULL, video_size BIGINT UNSIGNED NULL,
                        video_seconds DOUBLE NULL,
                        PRIMARY KEY (run_id, id),
                        KEY captures_by_date (captured_at, run_id, id)
                    ) ENGINE=InnoDB
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS maritime_console_capture_media (
                        run_id BIGINT UNSIGNED NOT NULL, capture_id CHAR(32) CHARACTER SET ascii NOT NULL,
                        kind TINYINT UNSIGNED NOT NULL, chunk_no INT UNSIGNED NOT NULL,
                        data MEDIUMBLOB NOT NULL,
                        PRIMARY KEY (run_id, capture_id, kind, chunk_no)
                    ) ENGINE=InnoDB
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS maritime_console_capture_settings (
                        id TINYINT UNSIGNED PRIMARY KEY, enabled BOOLEAN NOT NULL
                    ) ENGINE=InnoDB
                """)
                cursor.execute("INSERT IGNORE INTO maritime_console_capture_settings (id, enabled) VALUES (1, TRUE)")
                cursor.execute("SELECT enabled FROM maritime_console_capture_settings WHERE id = 1")
                self._enabled = bool(cursor.fetchone()["enabled"])
            self._initialized = True

    def is_enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        self.initialize()
        with self._lock, self.database.cursor() as cursor:
            cursor.execute("UPDATE maritime_console_capture_settings SET enabled = %s WHERE id = 1", (enabled,))
            self._enabled = enabled

    def sync(self):
        self.initialize()
        if self._imported is None:
            with self.database.cursor() as cursor:
                cursor.execute("SELECT run_id, id, photo_size, video_size FROM maritime_console_captures")
                self._imported = {
                    (row["run_id"], row["id"], kind)
                    for row in cursor.fetchall()
                    for kind, column in ((PHOTO, "photo_size"), (VIDEO, "video_size"))
                    if row[column] is not None
                }
        if not self.spool.is_dir():
            return
        for run in self.spool.iterdir():
            if not run.name.isascii() or not run.name.isdecimal() or run.is_symlink() or not run.is_dir():
                continue
            for folder in run.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                self._import_folder(int(run.name), folder)

    def _import_folder(self, run_id, folder):
        try:
            capture_id = UUID(folder.name).hex
        except ValueError:
            return
        photo_key = (run_id, capture_id, PHOTO)
        video_key = (run_id, capture_id, VIDEO)
        photo, video = folder / "photo.jpg", folder / "clip.webm"
        pending_photo = photo_key not in self._imported and photo.is_file() and not photo.is_symlink()
        pending_video = video_key not in self._imported and video.is_file() and not video.is_symlink()
        if not pending_photo and not pending_video:
            return
        try:
            details = json.loads((folder / "details.json").read_text(encoding="utf-8"))
            captured_at = datetime.fromisoformat(details["captured_at"])
            if captured_at.tzinfo is not None:
                captured_at = captured_at.astimezone(timezone.utc).replace(tzinfo=None)
            position = details["position"]
            north, east, altitude = (float(position[key]) for key in ("north", "east", "altitude"))
            boats, confidence = int(details["boats"]), float(details["confidence"])
            if boats < 1 or not all(math.isfinite(value) for value in (north, east, altitude, confidence)):
                return
            seconds = details.get("video_seconds")
            if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
                seconds = None
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return
        try:
            if pending_photo:
                self._store_media(photo_key, photo, (captured_at, north, east, altitude, boats, confidence))
                self._imported.add(photo_key)
            if pending_video and photo_key in self._imported:
                self._store_media(video_key, video, (captured_at, north, east, altitude, boats, confidence), seconds)
                self._imported.add(video_key)
        except OSError:
            LOG.warning("Could not import patrol capture %s/%s", run_id, capture_id, exc_info=True)

    def _store_media(self, key, path, details, seconds=None):
        run_id, capture_id, kind = key
        with path.open("rb") as source, self.database.cursor(transaction=True) as cursor:
            cursor.execute("""
                INSERT INTO maritime_console_captures
                    (run_id, id, captured_at, north, east, altitude, boats, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id = id
            """, (run_id, capture_id, *details))
            cursor.execute("""
                SELECT photo_size, video_size FROM maritime_console_captures
                WHERE run_id = %s AND id = %s FOR UPDATE
            """, (run_id, capture_id))
            column = "photo_size" if kind == PHOTO else "video_size"
            if cursor.fetchone()[column] is not None:
                return
            size = 0
            while data := source.read(CHUNK_SIZE):
                cursor.execute("""
                    INSERT INTO maritime_console_capture_media (run_id, capture_id, kind, chunk_no, data)
                    VALUES (%s, %s, %s, %s, %s)
                """, (*key, size // CHUNK_SIZE, data))
                size += len(data)
            if not size:
                raise OSError("Capture media is empty")
            if kind == PHOTO:
                cursor.execute("""
                    UPDATE maritime_console_captures SET photo_size = %s WHERE run_id = %s AND id = %s
                """, (size, run_id, capture_id))
            else:
                cursor.execute("""
                    UPDATE maritime_console_captures SET video_size = %s, video_seconds = %s
                    WHERE run_id = %s AND id = %s
                """, (size, seconds, run_id, capture_id))

    def records(self, offset, limit):
        self.initialize()
        with self.database.cursor(transaction=True) as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM maritime_console_captures WHERE photo_size IS NOT NULL")
            total = cursor.fetchone()["total"]
            cursor.execute("""
                SELECT run_id, id, captured_at, north, east, altitude, boats, confidence, video_seconds,
                       video_size IS NOT NULL AS has_video
                FROM maritime_console_captures WHERE photo_size IS NOT NULL
                ORDER BY captured_at DESC, run_id DESC, id DESC LIMIT %s OFFSET %s
            """, (limit, offset))
            items = cursor.fetchall()
        for item in items:
            item["position"] = {name: item.pop(name) for name in ("north", "east", "altitude")}
            item["has_video"] = bool(item["has_video"])
            item["detector"] = "yolo" if item["confidence"] > 0 else "simulation_image"
            item["captured_at"] = item["captured_at"].replace(tzinfo=timezone.utc).isoformat()
        return {"items": items, "total": total, "collection_enabled": self.is_enabled()}

    def media_size(self, run_id, capture_id, kind):
        self.initialize()
        with self.database.cursor() as cursor:
            cursor.execute("""
                SELECT photo_size, video_size FROM maritime_console_captures WHERE run_id = %s AND id = %s
            """, (run_id, capture_id))
            row = cursor.fetchone()
        return row and row["photo_size" if kind == PHOTO else "video_size"]

    def media_bytes(self, run_id, capture_id, kind, first, last):
        with self.database.cursor(streaming=True) as cursor:
            cursor.execute("""
                SELECT chunk_no, data FROM maritime_console_capture_media
                WHERE run_id = %s AND capture_id = %s AND kind = %s AND chunk_no BETWEEN %s AND %s
                ORDER BY chunk_no
            """, (run_id, capture_id, kind, first // CHUNK_SIZE, last // CHUNK_SIZE))
            for row in cursor:
                start = row["chunk_no"] * CHUNK_SIZE
                yield row["data"][max(first - start, 0):last - start + 1]
