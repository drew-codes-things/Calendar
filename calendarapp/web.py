import logging
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .apppasswords import AppPasswords
from .auth import csrf_matches, csrf_token
from .davstore import is_safe_segment
from .errors import CalendarError
from .events import EventBook, occurrence_json
from .gate import PREFIX
from .quickadd import parse_quick_add
from .reminders import ArtyNotifier, ReminderDesk
from .signin import SIGN_IN_PATHS, PasswordAuth, add_sign_in_pages
from .transfer import MAX_IMPORT_BYTES, MAX_IMPORT_EVENTS, export_calendars, import_calendar
from .when import parse_day

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_NAME = "Starfire Calendar"
MAX_REQUEST_BYTES = MAX_IMPORT_BYTES + 64 * 1024
MAX_ID_DIGITS = 18
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_PATHS = ("/api/health",)
SIGNED_OUT_TEXT = "Sign in through the main sign-in page first."
NESTED_TOO_DEEPLY_TEXT = "that JSON is nested too deeply"
WRONG_HOST_TEXT = ("this app does not answer on that host name (AUTH_MODE=local answers on localhost, 127.0.0.1 or "
                   "[::1] at its own port; the own sign-in answers on the names in ALLOWED_HOSTS, or with it empty on "
                   "localhost, IP addresses and this machine's name)")
MISDIRECTED_REQUEST = 421
MAX_CALENDAR_FILTER = 100
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
))

log = logging.getLogger("calendar.web")


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _body() -> dict:
    try:
        data = request.get_json(silent=True)
    except RecursionError as error:
        raise CalendarError(NESTED_TOO_DEEPLY_TEXT) from error
    if not isinstance(data, dict):
        raise CalendarError("send a JSON object")
    return data


def _is_same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.host


def _segment(value: str, what: str) -> str:
    if not is_safe_segment(value):
        raise CalendarError(f"that {what} does not exist", 404)
    return value


def _expected_etag() -> str | None:
    value = request.headers.get("If-Match")
    return value.strip() if value else None


def _day_arg(name: str) -> date:
    return parse_day(request.args.get(name), name)


def _calendar_filter() -> list[str] | None:
    raw = request.args.get("calendars")
    if raw is None:
        return None
    names = [name for name in raw.split(",") if name]
    if len(names) > MAX_CALENDAR_FILTER:
        raise CalendarError("too many calendars in the filter")
    return names


def _calendar_named(book: EventBook, name: str | None, fallback) -> str:
    calendars = book.calendar_list()
    if name:
        for calendar in calendars:
            if calendar["name"].lower() == name.lower():
                return calendar["id"]
        raise CalendarError(f"there is no calendar called {name}; quick add uses #name for a calendar")
    if isinstance(fallback, str) and fallback in {calendar["id"] for calendar in calendars}:
        return fallback
    return calendars[0]["id"]


def _password_id(raw: str) -> int:
    if not (raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS):
        raise CalendarError("that app password does not exist", 404)
    return int(raw)


def _attachment(body: str, mimetype: str, file_name: str) -> Response:
    response = Response(body, mimetype=mimetype)
    response.headers["Content-Disposition"] = f"attachment; filename={file_name}"
    return response


def create_app(book: EventBook, passwords: AppPasswords, desk: ReminderDesk, notifier: ArtyNotifier, auth, secret: bytes,
               settings) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    has_own_sign_in = isinstance(auth, PasswordAuth)
    if has_own_sign_in:
        add_sign_in_pages(app, auth, APP_NAME)

    @app.before_request
    def identify_and_guard_writes():
        if request.path in PUBLIC_PATHS:
            return None
        if not auth.accepts_host(request.host):
            return _error(WRONG_HOST_TEXT, MISDIRECTED_REQUEST)
        if has_own_sign_in and request.path in SIGN_IN_PATHS:
            return None
        user = auth.user_for(request.remote_addr or "", request.headers)
        if user is None:
            if request.path.startswith("/api/"):
                return _error("sign in first", 401)
            if has_own_sign_in:
                return redirect(auth.entry_path(), code=303)
            return Response(SIGNED_OUT_TEXT, status=401, mimetype="text/plain")
        g.user = user
        if request.method in SAFE_METHODS:
            return None
        if not _is_same_origin():
            return _error("this request came from another site", 403)
        if not csrf_matches(secret, user, request.headers.get(CSRF_HEADER)):
            return _error("missing or wrong CSRF token, reload the page", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(CalendarError)
    def calendar_refused(error: CalendarError):
        return _error(str(error), error.status)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if error.code == 413:
            return _error("that request is too large", 413)
        if request.path.startswith("/api/"):
            return _error(error.description or error.name, error.code or 500)
        return error

    @app.get("/")
    def index():
        response = send_from_directory(STATIC_DIR, "index.html", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/api/health")
    def health():
        if not passwords.database.is_reachable():
            log.error("health check: the database could not be read at %s", passwords.database.path)
            return jsonify({"ok": False, "error": "database unavailable"}), 503
        if not book.store.is_reachable():
            log.error("health check: the calendar store is not answering")
            return jsonify({"ok": False, "error": "calendar store unavailable"}), 503
        return jsonify({"ok": True})

    @app.get("/api/session")
    def session_state():
        return jsonify({
            "user": g.user, "csrfToken": csrf_token(secret, g.user), "version": __version__,
            "timeZone": book.zone.key, "today": book.today().isoformat(),
            "caldav": {"enabled": settings.caldav_enabled, "url": f"{settings.public_url}{PREFIX}/",
                       "user": settings.caldav_user},
            "arty": {"configured": notifier.is_configured, "linked": notifier.is_linked,
                     "approveUrl": settings.arty_approve_url},
            "importLimits": {"bytes": MAX_IMPORT_BYTES, "events": MAX_IMPORT_EVENTS},
        })

    @app.get("/api/calendars")
    def list_calendars():
        return jsonify({"calendars": book.calendar_list()})

    @app.post("/api/calendars")
    def create_calendar():
        data = _body()
        return jsonify(book.create_calendar(data.get("name"), data.get("color"))), 201

    @app.patch("/api/calendars/<calendar>")
    def change_calendar(calendar: str):
        data = _body()
        return jsonify(book.change_calendar(_segment(calendar, "calendar"), data.get("name"), data.get("color")))

    @app.delete("/api/calendars/<calendar>")
    def delete_calendar(calendar: str):
        book.delete_calendar(_segment(calendar, "calendar"))
        return "", 204

    @app.get("/api/events")
    def list_events():
        found, truncated = book.occurrences(_day_arg("from"), _day_arg("to"), _calendar_filter())
        return jsonify({"events": [occurrence_json(occurrence, book.zone) for occurrence in found],
                        "truncated": truncated, "today": book.today().isoformat()})

    @app.post("/api/events")
    def create_event():
        return jsonify(book.create(_body())), 201

    @app.get("/api/calendars/<calendar>/events/<item>")
    def read_event(calendar: str, item: str):
        return jsonify(book.get(_segment(calendar, "calendar"), _segment(item, "event"), request.args.get("occurrence")))

    @app.patch("/api/calendars/<calendar>/events/<item>")
    def update_event(calendar: str, item: str):
        return jsonify(book.update(_segment(calendar, "calendar"), _segment(item, "event"), _body(),
                                   request.args.get("scope", "all"), request.args.get("occurrence"), _expected_etag()))

    @app.delete("/api/calendars/<calendar>/events/<item>")
    def delete_event(calendar: str, item: str):
        book.delete(_segment(calendar, "calendar"), _segment(item, "event"), request.args.get("scope", "all"),
                    request.args.get("occurrence"), _expected_etag())
        return "", 204

    def understood(text, fallback_calendar) -> dict:
        parsed = parse_quick_add(text, book.now().astimezone(book.zone))
        fields = parsed.fields()
        fields["calendarId"] = _calendar_named(book, parsed.calendar, fallback_calendar)
        return fields

    @app.post("/api/parse")
    def preview_quick_add():
        data = _body()
        return jsonify(understood(data.get("text", ""), data.get("calendarId")))

    @app.post("/api/quick")
    def quick_add():
        data = _body()
        fields = understood(data.get("text"), data.get("calendarId"))
        created = book.create(fields)
        return jsonify({**created, "understood": fields}), 201

    @app.get("/api/export.ics")
    def export_download():
        calendar = request.args.get("calendar")
        body, file_name = export_calendars(book, _segment(calendar, "calendar") if calendar else None)
        return _attachment(body, "text/calendar", file_name)

    @app.post("/api/import")
    def import_upload():
        calendar = _segment(request.args.get("calendar", ""), "calendar")
        return jsonify(import_calendar(book, calendar, request.get_data(cache=False)))

    @app.get("/api/app-passwords")
    def list_app_passwords():
        return jsonify({"passwords": passwords.active()})

    @app.post("/api/app-passwords")
    def create_app_password():
        return jsonify(passwords.create(_body().get("name"))), 201

    @app.delete("/api/app-passwords/<raw_id>")
    def revoke_app_password(raw_id: str):
        passwords.revoke(_password_id(raw_id))
        return "", 204

    @app.get("/api/reminders")
    def reminder_feed():
        raw = request.args.get("after", "0")
        after = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS else 0
        reminders, latest = desk.feed(after)
        return jsonify({"reminders": reminders, "latest": latest})

    @app.post("/api/arty/link")
    def start_arty_link():
        return jsonify(notifier.start_link())

    @app.get("/api/arty/link")
    def check_arty_link():
        return jsonify({"status": notifier.finish_link()})

    @app.delete("/api/arty/link")
    def forget_arty_link():
        notifier.unlink()
        return "", 204

    @app.post("/api/arty/test")
    def test_arty():
        result = notifier.send("Starfire Calendar: test reminder", "Reminders from your calendar will arrive like this.")
        return jsonify({"result": result})

    return app
