SELECT_MISSIONS = "SELECT id, name, north, east, altitude FROM maritime_console_missions"


class MissionStore:
    def __init__(self, database):
        self.database = database
        self.initialized = False

    def _ensure_table(self):
        if self.initialized:
            return
        with self.database.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS maritime_console_missions (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(80) NOT NULL,
                    north DOUBLE NOT NULL,
                    east DOUBLE NOT NULL,
                    altitude DOUBLE NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        self.initialized = True

    def list(self):
        self._ensure_table()
        with self.database.cursor() as cursor:
            cursor.execute(f"{SELECT_MISSIONS} ORDER BY id DESC")
            return cursor.fetchall()

    def get(self, mission_id):
        self._ensure_table()
        with self.database.cursor() as cursor:
            return self._find(cursor, mission_id)

    def create(self, mission):
        self._ensure_table()
        with self.database.cursor() as cursor:
            cursor.execute(
                "INSERT INTO maritime_console_missions (name, north, east, altitude) "
                "VALUES (%s, %s, %s, %s)",
                (mission["name"], mission["north"], mission["east"], mission["altitude"]),
            )
            return {"id": cursor.lastrowid, **mission}

    def update(self, mission_id, changes):
        self._ensure_table()
        with self.database.cursor(transaction=True) as cursor:
            cursor.execute("""
                UPDATE maritime_console_missions
                SET name=COALESCE(%s, name), north=COALESCE(%s, north),
                    east=COALESCE(%s, east), altitude=COALESCE(%s, altitude)
                WHERE id=%s
            """, (changes.get("name"), changes.get("north"), changes.get("east"),
                  changes.get("altitude"), mission_id))
            return self._find(cursor, mission_id)

    def delete(self, mission_id):
        self._ensure_table()
        with self.database.cursor() as cursor:
            cursor.execute("DELETE FROM maritime_console_missions WHERE id=%s", (mission_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _find(cursor, mission_id):
        cursor.execute(f"{SELECT_MISSIONS} WHERE id=%s", (mission_id,))
        return cursor.fetchone()
