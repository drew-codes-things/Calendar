import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

LONDON = ZoneInfo("Europe/London")
START_TIME = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc).timestamp()
OWNER = "admin"
SECRET = b"k" * 32
CALDAV_USER = "owner"
STORE_START_SECONDS = 20


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_port(port: int, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing answered on port {port} within {seconds} s")


class FakeClock:
    def __init__(self, start: float = START_TIME):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def set(self, moment: float) -> None:
        self.now = moment


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(scope="session")
def store_settings(tmp_path_factory):
    from calendarapp.config import Settings
    data_dir = tmp_path_factory.mktemp("calendar-data")
    return Settings(data_dir=data_dir, store_port=free_port(), caldav_user=CALDAV_USER)


@pytest.fixture(scope="session")
def store_process(store_settings):
    from calendarapp.store import prepare_store
    config_path = prepare_store(store_settings)
    process = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(config_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_port(store_settings.store_port, STORE_START_SECONDS)
        yield process
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def dav(store_settings, store_process):
    from calendarapp.davstore import DavStore
    from calendarapp.store import store_password
    store = DavStore(store_settings.store_url, store_settings.caldav_user, store_password(store_settings.data_dir))
    for info in store.calendars():
        store.delete_calendar(info.name)
    return store


@pytest.fixture
def book(dav, clock):
    from calendarapp.events import EventBook
    return EventBook(dav, LONDON, clock=clock)


@pytest.fixture
def home(book):
    return book.calendar_list()[0]["id"]


@pytest.fixture
def database(tmp_path):
    from calendarapp.db import Database
    return Database(tmp_path / "calendar.db")


@pytest.fixture
def passwords(database, clock):
    from calendarapp.apppasswords import AppPasswords
    return AppPasswords(database, clock=clock)


class FakeArty:
    def __init__(self):
        self.sent = []
        self.answers = {}

    def __call__(self, path, body, key=None):
        self.sent.append((path, body, key))
        return self.answers.get(path, (200, {"result": "sent to Updates"}))


@pytest.fixture
def notifier(database):
    from calendarapp.reminders import ArtyNotifier
    return ArtyNotifier(database, "")


@pytest.fixture
def desk(book, database, notifier, clock):
    from calendarapp.reminders import ReminderDesk
    return ReminderDesk(book, database, notifier, clock=clock)


@pytest.fixture
def settings(store_settings):
    from dataclasses import replace
    return replace(store_settings, caldav_enabled=True)


@pytest.fixture
def app(book, passwords, desk, notifier, settings):
    from calendarapp.auth import ProxyAuth
    from calendarapp.web import create_app
    auth = ProxyAuth(trusted_proxies=("127.0.0.1", "::1"), user_header="X-Forwarded-User", allowed_users=(OWNER,))
    flask_app = create_app(book, passwords, desk, notifier, auth, SECRET, settings)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class SignedInClient:
    def __init__(self, client):
        self.client = client
        self.headers = {"X-Forwarded-User": OWNER}
        session = client.get("/api/session", headers=self.headers).get_json()
        self.write_headers = {**self.headers, "X-CSRF-Token": session["csrfToken"]}

    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.client.post(path, json=json, headers={**self.write_headers, **kwargs.pop("headers", {})}, **kwargs)

    def patch(self, path, json=None, **kwargs):
        return self.client.patch(path, json=json, headers={**self.write_headers, **kwargs.pop("headers", {})}, **kwargs)

    def delete(self, path, **kwargs):
        return self.client.delete(path, headers={**self.write_headers, **kwargs.pop("headers", {})}, **kwargs)


@pytest.fixture
def owner(client):
    return SignedInClient(client)


@pytest.fixture
def gate(passwords, store_settings, store_process):
    from calendarapp.gate import CalDavGate, FailureLimiter
    from calendarapp.store import store_password
    limiter = FailureLimiter(clock=FakeClock(0))
    return CalDavGate(passwords, CALDAV_USER, store_settings.store_port, store_password(store_settings.data_dir), True, True,
                      limiter=limiter)
