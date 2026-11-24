import os
from contextlib import closing, contextmanager
from pathlib import Path

import pymysql


class Database:
    def __init__(self):
        default_config = Path.home() / ".config/maritime/mysql.cnf"
        self.config = Path(os.getenv("MARITIME_MYSQL_CONFIG", str(default_config))).expanduser()

    @contextmanager
    def cursor(self, *, transaction=False, streaming=False):
        if not self.config.is_file():
            raise pymysql.OperationalError("MySQL client configuration is missing.")
        connection = pymysql.connect(
            read_default_file=str(self.config),
            read_default_group="client",
            charset="utf8mb4",
            binary_prefix=True,
            cursorclass=pymysql.cursors.SSDictCursor if streaming else pymysql.cursors.DictCursor,
            connect_timeout=2,
            read_timeout=3,
            write_timeout=3,
            init_command="SET time_zone = '+00:00'",
            autocommit=not transaction,
        )
        with closing(connection), connection.cursor() as cursor:
            yield cursor
            if transaction:
                connection.commit()
