import hashlib
import secrets
import time

from .db import Database
from .errors import CalendarError, storable_text

PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
PASSWORD_GROUPS = 6
PASSWORD_GROUP_LENGTH = 4
MAX_NAME_LENGTH = 80
MAX_ACTIVE_PASSWORDS = 50
MAX_PASSWORD_LENGTH = 200
LAST_USED_WRITE_SECONDS = 300
MAX_ADDRESS_LENGTH = 64


def new_password() -> str:
    groups = ("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_GROUP_LENGTH))
              for _ in range(PASSWORD_GROUPS))
    return "-".join(groups)


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8", "surrogateescape")).hexdigest()


def _clean_name(value) -> str:
    if not isinstance(value, str):
        raise CalendarError("name the device, for example: my phone")
    name = " ".join(storable_text(value, "the device name").split())
    if not name or not name.isprintable():
        raise CalendarError("name the device, for example: my phone")
    if len(name) > MAX_NAME_LENGTH:
        raise CalendarError(f"the device name is too long (at most {MAX_NAME_LENGTH} characters)")
    return name


def _summary(row) -> dict:
    return {"id": row["id"], "name": row["name"], "createdAt": row["created_at"],
            "lastUsedAt": row["last_used_at"], "lastUsedFrom": row["last_used_from"]}


class AppPasswords:
    def __init__(self, database: Database, clock=time.time):
        self.database = database
        self.clock = clock

    def create(self, name) -> dict:
        clean = _clean_name(name)
        password = new_password()
        with self.database.connect(immediate=True) as connection:
            active = connection.execute("SELECT COUNT(*) FROM app_passwords WHERE revoked_at IS NULL").fetchone()[0]
            if active >= MAX_ACTIVE_PASSWORDS:
                raise CalendarError(f"there are already {MAX_ACTIVE_PASSWORDS} app passwords; revoke one you no longer use first", 409)
            cursor = connection.execute(
                "INSERT INTO app_passwords (name, password_hash, created_at) VALUES (?, ?, ?)",
                (clean, password_hash(password), self.clock()))
            row = connection.execute("SELECT * FROM app_passwords WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return {**_summary(row), "password": password}

    def active(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM app_passwords WHERE revoked_at IS NULL ORDER BY id").fetchall()
        return [_summary(row) for row in rows]

    def revoke(self, password_id: int) -> None:
        with self.database.connect(immediate=True) as connection:
            cursor = connection.execute("UPDATE app_passwords SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                        (self.clock(), password_id))
            if cursor.rowcount == 0:
                raise CalendarError("that app password does not exist or was already revoked", 404)

    def check(self, password: str, address: str) -> bool:
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
            return False
        with self.database.connect() as connection:
            row = connection.execute("SELECT id, last_used_at FROM app_passwords WHERE password_hash = ? AND revoked_at IS NULL",
                                     (password_hash(password),)).fetchone()
        if row is None:
            return False
        self._note_use(row, address)
        return True

    def _note_use(self, row, address: str) -> None:
        now = self.clock()
        if row["last_used_at"] is not None and now - row["last_used_at"] < LAST_USED_WRITE_SECONDS:
            return
        with self.database.connect(immediate=True) as connection:
            connection.execute("UPDATE app_passwords SET last_used_at = ?, last_used_from = ? WHERE id = ?",
                               (now, address[:MAX_ADDRESS_LENGTH], row["id"]))
