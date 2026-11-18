import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database
from .errors import CalendarError
from .events import EventBook, Occurrence, end_of, start_of
from .when import as_instant, occurrence_key

CHECK_SECONDS = 30
GRACE_SECONDS = 10 * 60
LOOKAHEAD_DAYS = 29
KEPT_DAYS = 30
ARTY_TIMEOUT_SECONDS = 15
ARTY_KEY_STATE = "arty_key"
ARTY_CATEGORY = "calendar"
ARTY_DEVICE_NAME = "Calendar reminders"
MAX_FEED_ITEMS = 50
MAX_ARTY_ANSWER_BYTES = 64 * 1024
FEED_WINDOW_SECONDS = 15 * 60
MAX_ARTY_ATTEMPTS = 3
RETRY_WINDOW_SECONDS = 10 * 60
RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)
ARTY_UNREACHABLE = "Arty unreachable"

log = logging.getLogger("calendar.reminders")


@dataclass(frozen=True)
class DueReminder:
    key: str
    title: str
    message: str
    alarm_at: datetime


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _is_absolute(alarm) -> bool:
    trigger = alarm.get("TRIGGER")
    return trigger is not None and isinstance(trigger.dt, datetime)


def _alarm_moment(alarm, occurrence: Occurrence, zone) -> datetime | None:
    trigger = alarm.get("TRIGGER")
    if trigger is None:
        return None
    if isinstance(trigger.dt, datetime):
        return as_instant(trigger.dt, zone)
    if not isinstance(trigger.dt, timedelta):
        return None
    related_to_end = str(trigger.params.get("RELATED", "START")).upper() == "END"
    anchor = end_of(occurrence.instance) if related_to_end else start_of(occurrence.instance)
    return as_instant(anchor, zone) + trigger.dt


def _day_text(moment: datetime) -> str:
    return f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B')}"


def _when_text(occurrence: Occurrence, zone) -> str:
    start = as_instant(occurrence.start, zone)
    if occurrence.all_day:
        return f"{_day_text(start)} (all day)"
    end = as_instant(occurrence.end, zone)
    finish = end.strftime("%H:%M") if start.date() == end.date() else f"{_day_text(end)} {end.strftime('%H:%M')}"
    return f"{_day_text(start)}, {start.strftime('%H:%M')} to {finish}"


def due_reminders(book: EventBook, now: datetime) -> list[DueReminder]:
    zone = book.zone
    earliest = now - timedelta(seconds=GRACE_SECONDS)
    occurrences, _ = book.occurrences_between(earliest - timedelta(days=LOOKAHEAD_DAYS), now + timedelta(days=LOOKAHEAD_DAYS))
    due = []
    keys = set()
    for occurrence in occurrences:
        for alarm in occurrence.instance.walk("VALARM"):
            moment = _alarm_moment(alarm, occurrence, zone)
            if moment is None or not earliest <= moment <= now:
                continue
            key = _reminder_key(alarm, occurrence, moment, zone)
            if key in keys or (_is_absolute(alarm) and occurrence.recurring and as_instant(occurrence.end, zone) < moment):
                continue
            keys.add(key)
            title = str(occurrence.instance.get("SUMMARY") or "(no title)")
            location = str(occurrence.instance.get("LOCATION") or "")
            message = _when_text(occurrence, zone) + (f" at {location}" if location else "")
            due.append(DueReminder(key, title, message, moment))
    return due


def _reminder_key(alarm, occurrence: Occurrence, moment: datetime, zone) -> str:
    if _is_absolute(alarm):
        return f"{occurrence.calendar}/{occurrence.item}/at/{int(moment.timestamp())}"
    recurrence = occurrence.instance.get("RECURRENCE-ID")
    occurrence_id = occurrence_key(recurrence.dt, zone) if recurrence is not None else "-"
    return f"{occurrence.calendar}/{occurrence.item}/{occurrence_id}/{int(moment.timestamp())}"


class ArtyNotifier:
    def __init__(self, database: Database, base_url: str, timeout: float = ARTY_TIMEOUT_SECONDS):
        self.database = database
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.pending_device_code: str | None = None
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(_RefuseRedirects)

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    @property
    def is_linked(self) -> bool:
        return bool(self.database.state(ARTY_KEY_STATE))

    def _post(self, path: str, body: dict, key: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, _json_answer(response.read(MAX_ARTY_ANSWER_BYTES))
        except urllib.error.HTTPError as error:
            return error.code, _json_answer(error.read(MAX_ARTY_ANSWER_BYTES))
        except (OSError, ValueError) as error:
            raise CalendarError(f"Arty did not answer at {self.base_url} ({error.__class__.__name__})", 502) from error

    def start_link(self) -> dict:
        if not self.is_configured:
            raise CalendarError("ARTY_URL is not set, so reminders can only show in this page", 409)
        status, answer = self._post("/v1/device/start", {"name": ARTY_DEVICE_NAME})
        if status != 200 or not isinstance(answer.get("device_code"), str) or not isinstance(answer.get("user_code"), str):
            raise CalendarError(f"Arty would not start linking (answered {status})", 502)
        with self.lock:
            self.pending_device_code = answer["device_code"]
        return {"userCode": answer["user_code"], "expiresIn": answer.get("expires_in")}

    def finish_link(self) -> str:
        with self.lock:
            device_code = self.pending_device_code
        if device_code is None:
            return "linked" if self.is_linked else "not started"
        status, answer = self._post("/v1/device/token", {"device_code": device_code})
        if status == 202:
            return "waiting"
        with self.lock:
            self.pending_device_code = None
        if status == 200 and isinstance(answer.get("api_key"), str) and answer["api_key"]:
            self.database.set_state(ARTY_KEY_STATE, answer["api_key"])
            return "linked"
        return "denied" if status == 403 else "expired"

    def unlink(self) -> None:
        with self.lock:
            self.pending_device_code = None
        self.database.set_state(ARTY_KEY_STATE, None)

    def deliver(self, title: str, message: str) -> tuple[str, bool]:
        key = self.database.state(ARTY_KEY_STATE)
        if not self.is_configured or not key:
            return "not linked", False
        try:
            status, answer = self._post("/v1/notify", {"title": title, "message": message, "category": ARTY_CATEGORY}, key)
        except CalendarError as error:
            log.warning("reminder not sent to Arty: %s", error)
            return ARTY_UNREACHABLE, True
        if status == 401:
            log.warning("Arty refused the calendar's key (revoked?); reminders show in the page only until it is linked again")
            return "key refused", False
        if status != 200:
            return f"Arty answered {status}", status in RETRY_STATUSES
        return str(answer.get("result") or "sent")[:200], False

    def send(self, title: str, message: str) -> str:
        return self.deliver(title, message)[0]


def _json_answer(data: bytes) -> dict:
    try:
        parsed = json.loads(data or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ReminderDesk:
    def __init__(self, book: EventBook, database: Database, notifier: ArtyNotifier, clock=time.time):
        self.book = book
        self.database = database
        self.notifier = notifier
        self.clock = clock
        self.stopping = threading.Event()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def run_once(self) -> int:
        for key, title, message in self._waiting_retries():
            self._deliver(key, title, message)
        sent = 0
        for reminder in due_reminders(self.book, self.now()):
            if not self._claim(reminder):
                continue
            self._deliver(reminder.key, reminder.title, reminder.message)
            sent += 1
        self._forget_old()
        return sent

    def _deliver(self, key: str, title: str, message: str) -> None:
        result, retry = self.notifier.deliver(f"Calendar: {title}", message)
        with self.database.connect(immediate=True) as connection:
            connection.execute("UPDATE reminders_sent SET arty_result = ?, arty_attempts = arty_attempts + 1, arty_retry = ? "
                               "WHERE reminder_key = ?", (result, 1 if retry else 0, key))

    def _waiting_retries(self) -> list[tuple[str, str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT reminder_key, title, message FROM reminders_sent WHERE arty_retry = 1 AND arty_attempts < ? AND sent_at >= ? "
                "ORDER BY id", (MAX_ARTY_ATTEMPTS, self.clock() - RETRY_WINDOW_SECONDS)).fetchall()
        return [(row["reminder_key"], row["title"], row["message"]) for row in rows]

    def _claim(self, reminder: DueReminder) -> bool:
        with self.database.connect(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO reminders_sent (reminder_key, title, message, alarm_at, sent_at) VALUES (?, ?, ?, ?, ?)",
                (reminder.key, reminder.title, reminder.message, reminder.alarm_at.timestamp(), self.clock()))
            return cursor.rowcount == 1

    def _forget_old(self) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM reminders_sent WHERE sent_at < ?", (self.clock() - KEPT_DAYS * 86400,))

    def feed(self, after_id: int) -> tuple[list[dict], int]:
        since = self.clock() - FEED_WINDOW_SECONDS
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, title, message, alarm_at, arty_result FROM reminders_sent WHERE id > ? AND sent_at >= ? ORDER BY id LIMIT ?",
                (after_id, since, MAX_FEED_ITEMS)).fetchall()
            latest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM reminders_sent").fetchone()[0]
        return [{"id": row["id"], "title": row["title"], "message": row["message"], "artyResult": row["arty_result"],
                 "alarmAt": datetime.fromtimestamp(row["alarm_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                for row in rows], latest

    def run_forever(self) -> None:
        while not self.stopping.wait(CHECK_SECONDS):
            try:
                self.run_once()
            except Exception:
                log.exception("the reminder check failed; trying again in %d s", CHECK_SECONDS)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="reminders", daemon=True)
        thread.start()
        return thread
