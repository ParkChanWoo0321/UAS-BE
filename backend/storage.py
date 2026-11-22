import json

from .database import Database


class RunStore:
    def __init__(self, database=None):
        self.database = database or Database()

    def ensure_table(self):
        with self.database.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS maritime_console_runs (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    north DOUBLE NOT NULL,
                    east DOUBLE NOT NULL,
                    altitude DOUBLE NOT NULL,
                    phase VARCHAR(24) NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    log_dir TEXT NULL,
                    telemetry JSON NULL,
                    message TEXT NULL,
                    started_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                        ON UPDATE CURRENT_TIMESTAMP(6)
                ) ENGINE=InnoDB
            """)

    def create(self, target):
        with self.database.cursor() as cursor:
            cursor.execute(
                "INSERT INTO maritime_console_runs (north, east, altitude, phase) "
                "VALUES (%s, %s, %s, 'launching')",
                (target["north"], target["east"], target["altitude"]),
            )
            run_id = cursor.lastrowid
            cursor.execute("SELECT * FROM maritime_console_runs WHERE id=%s", (run_id,))
            return self._decode(cursor.fetchone())

    def update(self, run):
        with self.database.cursor() as cursor:
            cursor.execute(
                """UPDATE maritime_console_runs
                   SET phase=%s, active=%s, log_dir=%s, telemetry=%s, message=%s
                   WHERE id=%s""",
                (
                    run["phase"], run["active"], run.get("log_dir"),
                    json.dumps(run.get("telemetry"), allow_nan=False),
                    run.get("message"), run["id"],
                ),
            )

    def recent(self):
        with self.database.cursor() as cursor:
            cursor.execute("""
                SELECT * FROM maritime_console_runs ORDER BY id DESC LIMIT 10
            """)
            return [self._decode(row) for row in cursor.fetchall()]

    def active(self):
        with self.database.cursor() as cursor:
            cursor.execute("""
                SELECT * FROM maritime_console_runs WHERE active=TRUE ORDER BY id DESC LIMIT 1
            """)
            return self._decode(cursor.fetchone())

    @staticmethod
    def _decode(row):
        if not row:
            return None
        for key in ("started_at", "updated_at"):
            row[key] = row[key].isoformat() + "Z"
        row["active"] = bool(row["active"])
        if row["telemetry"]:
            row["telemetry"] = json.loads(row["telemetry"])
        return row
