import logging
import sys
import threading
from zoneinfo import ZoneInfo

from waitress.server import create_server

from .apppasswords import AppPasswords
from .auth import LocalAuth, ProxyAuth, SecretKeyError, load_or_create_secret
from .config import ConfigError, Settings, load_settings
from .davstore import DavStore
from .db import Database, ensure_private_folder
from .events import EventBook
from .gate import MAX_BODY_BYTES, CalDavGate
from .reminders import ArtyNotifier, ReminderDesk
from .signin import OWNER_FILE_NAME, OwnerAccount, PasswordAuth
from .store import store_password
from .web import MAX_REQUEST_BYTES, create_app

WAITRESS_THREADS = 4
SESSION_COOKIE = "calendar_session"
GATE_THREADS = 4

log = logging.getLogger("calendar")


def build_auth(settings: Settings, secret: bytes):
    if settings.auth_mode == "local":
        return LocalAuth(settings.local_user, settings.port)
    if settings.auth_mode == "proxy":
        return ProxyAuth(settings.trusted_proxies, settings.user_header, settings.allowed_users)
    return PasswordAuth(OwnerAccount(settings.data_dir / OWNER_FILE_NAME), secret, SESSION_COOKIE, settings.secure_cookies,
                        settings.allowed_hosts, trusted_proxies=settings.trusted_proxies)


def waitress_options(settings: Settings) -> dict:
    keeps_forwarded_for = settings.auth_mode == "builtin" and bool(settings.trusted_proxies)
    return {"clear_untrusted_proxy_headers": not keeps_forwarded_for}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 2
    ensure_private_folder(settings.data_dir)
    try:
        secret = load_or_create_secret(settings.data_dir, settings.secret_key)
    except SecretKeyError as error:
        log.error("secret key problem: %s", error)
        return 2
    database = Database(settings.database_path)
    password = store_password(settings.data_dir)
    book = EventBook(DavStore(settings.store_url, settings.caldav_user, password), ZoneInfo(settings.time_zone))
    passwords = AppPasswords(database)
    notifier = ArtyNotifier(database, settings.arty_url)
    desk = ReminderDesk(book, database, notifier)
    auth = build_auth(settings, secret)
    if isinstance(auth, PasswordAuth) and auth.needs_setup():
        auth.setup_code()
    app = create_app(book, passwords, desk, notifier, auth, secret, settings)
    gate = CalDavGate(passwords, settings.caldav_user, settings.store_port, password, settings.caldav_enabled,
                      settings.caldav_require_cloudflare_https, settings.trusted_proxies)
    web_server = create_server(app, host=settings.bind_host, port=settings.port, threads=WAITRESS_THREADS,
                               max_request_body_size=MAX_REQUEST_BYTES, **waitress_options(settings))
    gate_server = create_server(gate, host=settings.bind_host, port=settings.dav_port, threads=GATE_THREADS,
                                max_request_body_size=MAX_BODY_BYTES)
    desk.start()
    threading.Thread(target=gate_server.run, name="caldav-gate", daemon=True).start()
    log.info("Calendar on http://%s:%d, CalDAV gate on port %d (%s), store on port %d, data in %s, sign-in %s, "
             "times in %s, reminders %s", settings.bind_host, settings.port, settings.dav_port,
             "on" if settings.caldav_enabled else "switched off", settings.store_port, settings.data_dir,
             settings.auth_mode, settings.time_zone,
             "to Arty when linked, and in the open page" if notifier.is_configured else "in the open page only")
    web_server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
