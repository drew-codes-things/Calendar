import logging
import os
import secrets
import sys
import threading
from pathlib import Path

from .config import ConfigError, Settings, load_settings
from .db import ensure_private_folder

PASSWORD_FILE_NAME = "store_password"
USERS_FILE_NAME = "store_users"
CONFIG_FILE_NAME = "radicale.conf"
PASSWORD_BYTES = 32
MAX_STORE_BODY_BYTES = 20 * 1024 * 1024

log = logging.getLogger("calendar.store")


def _write_private(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _usable_password(path: Path) -> str | None:
    try:
        password = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return password if len(password) >= PASSWORD_BYTES else None


def _claim_first(path: Path, password: str) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(password)
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink()


def store_password(data_dir: Path) -> str:
    path = Path(data_dir) / PASSWORD_FILE_NAME
    existing = _usable_password(path)
    if existing:
        return existing
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    password = secrets.token_hex(PASSWORD_BYTES)
    _claim_first(path, password)
    claimed = _usable_password(path)
    if claimed:
        return claimed
    _write_private(path, password)
    return password


def store_config_text(settings: Settings) -> str:
    users = settings.data_dir / USERS_FILE_NAME
    return "\n".join((
        "[server]",
        f"hosts = 127.0.0.1:{settings.store_port}",
        f"max_content_length = {MAX_STORE_BODY_BYTES}",
        "[auth]",
        "type = htpasswd",
        f"htpasswd_filename = {users}",
        "htpasswd_encryption = plain",
        "delay = 1",
        "[rights]",
        "type = owner_only",
        "[storage]",
        f"filesystem_folder = {settings.store_folder}",
        "[web]",
        "type = none",
        "[sharing]",
        "type = none",
        "[logging]",
        "level = warning",
        "",
    ))


def prepare_store(settings: Settings) -> Path:
    password = store_password(settings.data_dir)
    settings.store_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(settings.data_dir / USERS_FILE_NAME, f"{settings.caldav_user}:{password}\n")
    config_path = settings.data_dir / CONFIG_FILE_NAME
    _write_private(config_path, store_config_text(settings))
    return config_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 2
    ensure_private_folder(settings.data_dir)
    config_path = prepare_store(settings)
    log.info("calendar store (Radicale) on 127.0.0.1:%d, events in %s", settings.store_port, settings.store_folder)
    os.execv(sys.executable, [sys.executable, "-m", "radicale", "--config", str(config_path)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
