import hashlib
import logging
import re
import time
import uuid
from datetime import datetime

import icalendar
import recurring_ical_events
import x_wr_timezone

from .davstore import CalendarInfo
from .errors import CalendarError
from .events import PRODUCT_ID, EventBook, repeats_more_than_daily

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_EVENTS = 500
EVENT_START_LINE = re.compile(rb"^BEGIN:VEVENT\r?$", re.IGNORECASE | re.MULTILINE)
SUB_DAILY_REASON = "it repeats more than once a day, which this calendar does not import"
UNWORKABLE_REASON = "its dates could not be worked out (for example a start or end far outside the usual years)"
SLOW_REASON = "working out its dates takes too long (it repeats often and starts long before today); move its start nearer today"
OUT_OF_TIME_REASON = "the file took too long to check, so this event was not imported; import it again as a smaller file"
IMPORT_CHECK_SECONDS = 20
SLOW_EVENT_SECONDS = 0.1
SKIPPED_KINDS = ("VTODO", "VJOURNAL", "VFREEBUSY")
NAME_HASH_LENGTH = 40

log = logging.getLogger("calendar.transfer")


def _item_name(uid: str) -> str:
    return hashlib.sha256(uid.encode("utf-8", "surrogateescape")).hexdigest()[:NAME_HASH_LENGTH] + ".ics"


def _wrapper(name: str | None = None) -> icalendar.Calendar:
    calendar = icalendar.Calendar()
    calendar.add("PRODID", PRODUCT_ID)
    calendar.add("VERSION", "2.0")
    if name:
        calendar.add("X-WR-CALNAME", name)
    return calendar


def _zones_used(events: list) -> set[str]:
    used = set()
    for event in events:
        for value in event.values():
            for entry in value if isinstance(value, list) else [value]:
                tzid = getattr(entry, "params", {}).get("TZID")
                if tzid:
                    used.add(str(tzid))
    return used


def export_calendars(book: EventBook, calendar: str | None) -> tuple[str, str]:
    chosen: list[CalendarInfo] = [info for info in book.calendars() if calendar is None or info.name == calendar]
    if calendar is not None and not chosen:
        raise CalendarError("that calendar does not exist", 404)
    document = _wrapper(chosen[0].display_name if calendar is not None else "Calendar")
    zones: dict[str, icalendar.Timezone] = {}
    events = []
    for info in chosen:
        for item in book.store.items(info):
            try:
                parsed = icalendar.Calendar.from_ical(item.text)
            except ValueError:
                log.warning("export skipped unreadable item %s in %s", item.name, info.name)
                continue
            for zone in parsed.walk("VTIMEZONE"):
                zones.setdefault(str(zone.get("TZID")), zone)
            events.extend(parsed.walk("VEVENT"))
    for zone in zones.values():
        document.add_component(zone)
    for event in events:
        document.add_component(event)
    file_name = "calendar.ics" if calendar is None else "calendar-" + "".join(
        character for character in chosen[0].display_name.lower() if character.isascii() and character.isalnum())[:40] + ".ics"
    return document.to_ical().decode("utf-8"), file_name


def _check_limits(data: bytes) -> None:
    if not data:
        raise CalendarError("choose an .ics file to import")
    if len(data) > MAX_IMPORT_BYTES:
        raise CalendarError(f"that file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB; split it and import the parts", 413)
    event_count = len(EVENT_START_LINE.findall(data))
    if event_count > MAX_IMPORT_EVENTS:
        raise CalendarError(f"that file holds {event_count} events; import at most {MAX_IMPORT_EVENTS} at a time", 413)


def _standard(calendar: icalendar.Calendar) -> icalendar.Calendar:
    try:
        return x_wr_timezone.to_standard(calendar)
    except (KeyError, ValueError) as error:
        name = str(calendar.get("X-WR-TIMEZONE") or "")[:60]
        raise CalendarError(f"that file names a time zone this app does not know ({name})") from error


def _read_upload(data: bytes) -> icalendar.Calendar:
    _check_limits(data)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CalendarError("that file is not UTF-8 text, so it is not an .ics calendar") from error
    try:
        calendars = icalendar.Calendar.from_ical(text, multiple=True)
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise CalendarError(f"that file could not be read as an .ics calendar ({error.__class__.__name__})") from error
    if not calendars or any(calendar.name != "VCALENDAR" for calendar in calendars):
        raise CalendarError("that file holds no VCALENDAR, so it is not an .ics calendar")
    merged = _wrapper()
    for calendar in calendars:
        standard = _standard(calendar)
        for component in standard.subcomponents:
            merged.add_component(component)
    return merged


def _grouped_events(upload: icalendar.Calendar) -> tuple[dict[str, list], int]:
    groups: dict[str, list] = {}
    skipped = 0
    for component in upload.subcomponents:
        if component.name in SKIPPED_KINDS:
            skipped += 1
            continue
        if component.name != "VEVENT":
            continue
        if component.get("DTSTART") is None:
            skipped += 1
            continue
        uid = str(component.get("UID") or "").strip()
        if not uid:
            uid = str(uuid.uuid4())
            component.add("UID", uid)
        groups.setdefault(uid, []).append(component)
    return groups, skipped


def import_calendar(book: EventBook, calendar: str, data: bytes) -> dict:
    info = book.store.calendar(calendar)
    upload = _read_upload(data)
    zones = {str(zone.get("TZID")): zone for zone in upload.walk("VTIMEZONE")}
    groups, skipped = _grouped_events(upload)
    if not groups:
        raise CalendarError("that file holds no events to import")
    existing = _existing_uids(book, info)
    imported, already, refused = 0, 0, []
    checks_end_at = time.monotonic() + IMPORT_CHECK_SECONDS
    for uid, events in groups.items():
        if uid in existing:
            already += 1
            continue
        document = _wrapper()
        for tzid in sorted(_zones_used(events)):
            if tzid in zones:
                document.add_component(zones[tzid])
        for event in events:
            document.add_component(event)
        out_of_time = time.monotonic() > checks_end_at
        reason = OUT_OF_TIME_REASON if out_of_time else _reason_to_refuse(document, events, book.now())
        if reason is None:
            try:
                book.store.put(calendar, _item_name(uid), document.to_ical().decode("utf-8"), None)
            except CalendarError as error:
                reason = str(error)
        if reason is not None:
            refused.append({"title": str(events[0].get("SUMMARY") or "(no title)")[:100], "reason": reason[:200]})
            continue
        imported += 1
    return {"imported": imported, "alreadyThere": already, "skipped": skipped, "refused": refused}


def _reason_to_refuse(document: icalendar.Calendar, events: list, now: datetime) -> str | None:
    if any(repeats_more_than_daily(event) for event in events):
        return SUB_DAILY_REASON
    began = time.monotonic()
    try:
        next(iter(recurring_ical_events.of(document).after(now)), None)
    except Exception:
        log.warning("import refused an event whose dates could not be worked out", exc_info=True)
        return UNWORKABLE_REASON
    if time.monotonic() - began > SLOW_EVENT_SECONDS:
        return SLOW_REASON
    return None


def _existing_uids(book: EventBook, info: CalendarInfo) -> set[str]:
    found = set()
    for item in book.store.items(info):
        try:
            parsed = icalendar.Calendar.from_ical(item.text)
        except ValueError:
            continue
        found.update(str(event.get("UID")) for event in parsed.walk("VEVENT") if event.get("UID"))
    return found
