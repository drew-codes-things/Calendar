import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 10
PRIVATE_FOLDER_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
REMINDERS_TABLE = """
CREATE TABLE IF NOT EXISTS reminders_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    alarm_at REAL NOT NULL,
    sent_at REAL NOT NULL,
    arty_result TEXT NOT NULL DEFAULT '',
    arty_attempts INTEGER NOT NULL DEFAULT 0,
    arty_retry INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reminders_sent_by_time ON reminders_sent (sent_at);
"""
KEPT_REMINDER_COLUMNS = "id, reminder_key, title, message, alarm_at, sent_at, arty_result"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_passwords (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL,
    last_used_from TEXT,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS app_passwords_active ON app_passwords (revoked_at);
CREATE TABLE IF NOT EXISTS app_state (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""" + REMINDERS_TABLE


def ensure_private_folder(path: Path) -> None:
    folder = Path(path)
    folder.mkdir(parents=True, exist_ok=True, mode=PRIVATE_FOLDER_MODE)
    os.chmod(folder, PRIVATE_FOLDER_MODE)


def _ensure_private_file(path: Path) -> None:
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, PRIVATE_FILE_MODE))
    os.chmod(path, PRIVATE_FILE_MODE)


def _reminders_table_is_current(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reminders_sent'").fetchone()
    return row is None or ("AUTOINCREMENT" in row[0].upper() and "arty_retry" in row[0])


def _upgrade_reminders_table(connection: sqlite3.Connection) -> None:
    if _reminders_table_is_current(connection):
        return
    connection.executescript(
        "BEGIN IMMEDIATE;"
        "ALTER TABLE reminders_sent RENAME TO reminders_sent_before_upgrade;"
        "DROP INDEX IF EXISTS reminders_sent_by_time;"
        + REMINDERS_TABLE +
        f"INSERT INTO reminders_sent ({KEPT_REMINDER_COLUMNS}) SELECT {KEPT_REMINDER_COLUMNS} FROM reminders_sent_before_upgrade;"
        "DROP TABLE reminders_sent_before_upgrade;"
        "COMMIT;")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        ensure_private_folder(self.path.parent)
        _ensure_private_file(self.path)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            _upgrade_reminders_table(connection)
            connection.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connect(self, immediate: bool = False):
        connection = self._open()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_reachable(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT COUNT(*) FROM app_passwords").fetchone()
        except sqlite3.Error:
            return False
        return True

    def state(self, name: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_state WHERE name = ?", (name,)).fetchone()
        return None if row is None else row["value"]

    def set_state(self, name: str, value: str | None) -> None:
        with self.connect(immediate=True) as connection:
            if value is None:
                connection.execute("DELETE FROM app_state WHERE name = ?", (name,))
                return
            connection.execute("INSERT INTO app_state (name, value) VALUES (?, ?) "
                               "ON CONFLICT (name) DO UPDATE SET value = excluded.value", (name, value))
