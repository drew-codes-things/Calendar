import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

from .davstore import CalendarInfo, ChangedElsewhere, DavStore, StoredItem, is_safe_segment
from .errors import CalendarError, storable_text
from .expansion import series_near
from .repeat import clean_repeat, describe, from_rrule, to_rrule, WEEKDAY_CODES
from .when import (as_instant, as_local, local_text, occurrence_key, parse_day, parse_local_time,
                   parse_occurrence_key, same_occurrence, start_of_day, utc_text)

PRODUCT_ID = "-//Calendar//Calendar//EN"
MAX_TITLE_LENGTH = 500
MAX_LOCATION_LENGTH = 500
MAX_NOTES_LENGTH = 10000
MAX_CALENDAR_NAME_LENGTH = 100
MAX_REMINDERS = 5
MAX_REMINDER_MINUTES = 4 * 7 * 24 * 60
MAX_EVENT_DAYS = 366
MAX_WINDOW_DAYS = 400
MAX_OCCURRENCES = 5000
MAX_OCCURRENCES_PER_ITEM = 1000
SUB_DAILY_FREQUENCIES = ("SECONDLY", "MINUTELY", "HOURLY")
SUB_DAILY_PARTS = ("BYHOUR", "BYMINUTE", "BYSECOND")
UNEXPANDED_FREQUENCIES = ("SECONDLY", "MINUTELY")
UNEXPANDED_PARTS = ("BYMINUTE", "BYSECOND")
TOO_OFTEN_TEXT = "this event repeats more often than once an hour, so it can only be changed as a whole (every date)"
SCOPES = ("one", "all")
DEFAULT_CALENDAR_NAME = "Personal"
DEFAULT_COLOR = "#e5383b"
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$", re.ASCII)
STORED_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$", re.ASCII)
EDITABLE_FIELDS = ("title", "location", "notes", "allDay", "start", "end", "repeat", "reminders")
SHARED_TEXT_FIELDS = (("title", "SUMMARY"), ("location", "LOCATION"), ("notes", "DESCRIPTION"))
REMINDER_TEXT = "Reminder"
ONE_DAY = timedelta(days=1)

log = logging.getLogger("calendar.events")


@dataclass
class EventFields:
    title: str
    location: str
    notes: str
    all_day: bool
    start: date | datetime
    end: date | datetime
    repeat: dict | None
    reminders: list[int]


@dataclass
class Occurrence:
    calendar: str
    item: str
    etag: str
    uid: str
    instance: icalendar.Event
    master: icalendar.Event
    start: date | datetime
    end: date | datetime
    recurring: bool
    color: str = DEFAULT_COLOR
    changed: bool = field(default=False)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


def _text(component, name: str) -> str:
    value = component.get(name)
    return "" if value is None else str(value)


def _limited_text(value, field_name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CalendarError(f"{field_name} must be text")
    text = storable_text(value, field_name).strip()
    if field_name == "title":
        text = " ".join(text.split())
    if required and not text:
        raise CalendarError(f"give the event a {field_name}")
    if len(text) > limit:
        raise CalendarError(f"{field_name} is too long (at most {limit} characters)")
    return text


def clean_color(value) -> str:
    if not isinstance(value, str) or not COLOR_PATTERN.match(value):
        raise CalendarError("color must look like #e5383b")
    return value.lower()


def stored_color(value: str) -> str:
    return value[:7].lower() if STORED_COLOR_PATTERN.match(value or "") else DEFAULT_COLOR


def _clean_reminders(value) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_REMINDERS:
        raise CalendarError(f"reminders must be a list of at most {MAX_REMINDERS} numbers of minutes before the start")
    minutes = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or abs(entry) > MAX_REMINDER_MINUTES:
            raise CalendarError(f"each reminder is a whole number of minutes before the start, up to {MAX_REMINDER_MINUTES}")
        if entry not in minutes:
            minutes.append(entry)
    return sorted(minutes)


def _clean_times(raw: dict, zone: ZoneInfo) -> tuple[bool, date | datetime, date | datetime]:
    all_day = raw.get("allDay", False)
    if not isinstance(all_day, bool):
        raise CalendarError("allDay must be true or false")
    if all_day:
        start = parse_day(raw.get("start"), "start")
        last_day = parse_day(raw.get("end", raw.get("start")), "end")
        if last_day < start:
            raise CalendarError("the event ends before it starts")
        end = last_day + ONE_DAY
    else:
        start = parse_local_time(raw.get("start"), "start", zone)
        end = parse_local_time(raw.get("end", raw.get("start")), "end", zone)
        if end < start:
            raise CalendarError("the event ends before it starts")
    if as_instant(end, zone) - as_instant(start, zone) > timedelta(days=MAX_EVENT_DAYS):
        raise CalendarError(f"an event can last at most {MAX_EVENT_DAYS} days; for longer, make it repeat")
    return all_day, start, end


def clean_fields(raw: dict, zone: ZoneInfo) -> EventFields:
    if not isinstance(raw, dict):
        raise CalendarError("send a JSON object")
    all_day, start, end = _clean_times(raw, zone)
    start_day = start.date() if isinstance(start, datetime) else start
    return EventFields(
        title=_limited_text(raw.get("title"), "title", MAX_TITLE_LENGTH, required=True),
        location=_limited_text(raw.get("location"), "location", MAX_LOCATION_LENGTH),
        notes=_limited_text(raw.get("notes"), "notes", MAX_NOTES_LENGTH),
        all_day=all_day,
        start=start,
        end=end,
        repeat=clean_repeat(raw.get("repeat"), start_day),
        reminders=_clean_reminders(raw.get("reminders")),
    )


def start_of(component) -> date | datetime:
    value = component.get("DTSTART")
    if value is None:
        raise CalendarError("an event in this calendar has no start", 422)
    return value.dt


def end_of(component) -> date | datetime:
    start = start_of(component)
    if component.get("DTEND") is not None:
        return component.get("DTEND").dt
    if component.get("DURATION") is not None:
        return start + component.get("DURATION").dt
    return start + ONE_DAY if not isinstance(start, datetime) else start


def is_repeating(component) -> bool:
    return component.get("RRULE") is not None or component.get("RDATE") is not None


def _alarm_minutes(alarm) -> int | None:
    trigger = alarm.get("TRIGGER")
    if trigger is None or not isinstance(trigger.dt, timedelta):
        return None
    if str(trigger.params.get("RELATED", "START")).upper() != "START":
        return None
    return round(-trigger.dt.total_seconds() / 60)


def reminders_of(component) -> list[int]:
    found = (_alarm_minutes(alarm) for alarm in component.subcomponents if alarm.name == "VALARM")
    return sorted({minutes for minutes in found if minutes is not None})


def _rrule_of(component):
    rules = component.get("RRULE")
    if isinstance(rules, list):
        return rules[0] if rules else None
    return rules


def _rules_of(component) -> list:
    rules = component.get("RRULE")
    if rules is None:
        return []
    return rules if isinstance(rules, list) else [rules]


def _rule_values(rule, name: str) -> list:
    values = rule.get(name) or []
    return values if isinstance(values, list) else [values]


def _repeats_finer_than(component, frequencies: tuple, parts: tuple) -> bool:
    for rule in _rules_of(component):
        if any(str(value).upper() in frequencies for value in _rule_values(rule, "FREQ")):
            return True
        if any(len(_rule_values(rule, part)) > 1 for part in parts):
            return True
    return False


def repeats_more_than_daily(component) -> bool:
    return _repeats_finer_than(component, SUB_DAILY_FREQUENCIES, SUB_DAILY_PARTS)


def repeats_too_often_to_expand(component) -> bool:
    return _repeats_finer_than(component, UNEXPANDED_FREQUENCIES, UNEXPANDED_PARTS)


def _on_the_clock(value, zone: ZoneInfo):
    if not isinstance(value, datetime):
        return value
    return as_local(value, zone).astimezone(timezone.utc).astimezone(zone)


def normalised_times(start, end, zone: ZoneInfo) -> tuple:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return start, end
    clock_start, clock_end = _on_the_clock(start, zone), _on_the_clock(end, zone)
    if clock_end < clock_start:
        wall_length = as_local(end, zone).replace(tzinfo=None) - as_local(start, zone).replace(tzinfo=None)
        clock_end = clock_start + wall_length
    return clock_start, clock_end


def _overlaps(component, start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    first = as_instant(start_of(component), zone)
    last = as_instant(end_of(component), zone)
    return first < end and (last > start or first >= start)


def _shown_end(start, end, zone: ZoneInfo) -> str:
    if isinstance(start, datetime):
        return local_text(end, zone)
    last_day = end - ONE_DAY if end > start else start
    return last_day.isoformat()


def occurrence_json(occurrence: Occurrence, zone: ZoneInfo) -> dict:
    instance, master = occurrence.instance, occurrence.master
    repeat = from_rrule(_rrule_of(master), start_of(master), zone) if occurrence.recurring else None
    master_start = as_local(start_of(master), zone)
    recurrence = instance.get("RECURRENCE-ID")
    return {
        "calendarId": occurrence.calendar,
        "itemId": occurrence.item,
        "uid": occurrence.uid,
        "occurrence": occurrence_key(recurrence.dt, zone) if occurrence.recurring and recurrence is not None else None,
        "title": _text(instance, "SUMMARY"),
        "location": _text(instance, "LOCATION"),
        "notes": _text(instance, "DESCRIPTION"),
        "allDay": occurrence.all_day,
        "start": local_text(occurrence.start, zone),
        "end": _shown_end(as_local(occurrence.start, zone), as_local(occurrence.end, zone), zone),
        "startsAt": utc_text(as_instant(occurrence.start, zone)),
        "endsAt": utc_text(as_instant(occurrence.end, zone)),
        "recurring": occurrence.recurring,
        "repeat": repeat,
        "repeatText": describe(repeat, master_start) if repeat else "",
        "reminders": reminders_of(instance),
        "color": occurrence.color,
        "changedInstance": occurrence.changed,
        "etag": occurrence.etag,
    }


def find_master(calendar: icalendar.Calendar) -> icalendar.Event:
    events = [component for component in calendar.walk("VEVENT")]
    if not events:
        raise CalendarError("that item holds no event", 422)
    for event in events:
        if event.get("RECURRENCE-ID") is None:
            return event
    return events[0]


def _overrides(calendar: icalendar.Calendar) -> list[icalendar.Event]:
    return [event for event in calendar.walk("VEVENT") if event.get("RECURRENCE-ID") is not None]


def parse_item(text: str) -> icalendar.Calendar:
    try:
        return icalendar.Calendar.from_ical(text)
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise CalendarError(f"that event could not be read as iCalendar ({error.__class__.__name__})", 422) from error


def _set_text(component, name: str, value: str) -> None:
    component.pop(name, None)
    if value:
        component.add(name, value)


def _set_times(component, start, end) -> None:
    for name in ("DTSTART", "DTEND", "DURATION"):
        component.pop(name, None)
    component.add("DTSTART", start)
    component.add("DTEND", end)


def _set_reminders(component, minutes: list[int], title: str) -> None:
    kept = [sub for sub in component.subcomponents if sub.name != "VALARM" or _alarm_minutes(sub) is None]
    component.subcomponents = kept
    for amount in minutes:
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", title or REMINDER_TEXT)
        alarm.add("TRIGGER", timedelta(minutes=-amount))
        component.add_component(alarm)


def _stamp(component, now: datetime) -> None:
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        component.pop(name, None)
        component.add(name, now)
    sequence = int(component.get("SEQUENCE", 0) or 0)
    component.pop("SEQUENCE", None)
    component.add("SEQUENCE", sequence + 1)


def _apply_fields(component, fields: EventFields, now: datetime) -> None:
    _set_text(component, "SUMMARY", fields.title)
    _set_text(component, "LOCATION", fields.location)
    _set_text(component, "DESCRIPTION", fields.notes)
    _set_times(component, fields.start, fields.end)
    _set_reminders(component, fields.reminders, fields.title)
    _stamp(component, now)


def _new_calendar() -> icalendar.Calendar:
    calendar = icalendar.Calendar()
    calendar.add("PRODID", PRODUCT_ID)
    calendar.add("VERSION", "2.0")
    return calendar


def _like_start(value, master_start, zone: ZoneInfo):
    if not isinstance(master_start, datetime):
        local = as_local(value, zone)
        return local.date() if isinstance(local, datetime) else local
    moment = as_instant(value, zone)
    if master_start.tzinfo is None:
        return moment.astimezone(zone).replace(tzinfo=None)
    return moment.astimezone(master_start.tzinfo)


def _shift_wall(value, delta: timedelta, zone: ZoneInfo):
    if not isinstance(value, datetime):
        return value + timedelta(days=delta.days)
    original_zone = value.tzinfo
    local = as_local(value, zone).replace(tzinfo=None) + delta
    moved = datetime(local.year, local.month, local.day, local.hour, local.minute, local.second, tzinfo=zone)
    moved = moved.astimezone(timezone.utc).astimezone(zone)
    if original_zone is None:
        return moved.replace(tzinfo=None)
    return moved.astimezone(original_zone)


def _wall_delta(old, new, zone: ZoneInfo) -> timedelta:
    if not isinstance(old, datetime) or not isinstance(new, datetime):
        old_day = old.date() if isinstance(old, datetime) else old
        new_day = new.date() if isinstance(new, datetime) else new
        return new_day - old_day
    return as_local(new, zone).replace(tzinfo=None) - as_local(old, zone).replace(tzinfo=None)


def _exdates(master) -> list:
    found = master.get("EXDATE")
    if found is None:
        return []
    groups = found if isinstance(found, list) else [found]
    return [entry.dt for group in groups for entry in group.dts]


def _set_exdates(master, values: list) -> None:
    master.pop("EXDATE", None)
    for value in values:
        master.add("EXDATE", value)


def _rule_shape(rule: dict | None) -> tuple:
    if rule is None:
        return ()
    return tuple((key, str(rule.get(key))) for key in ("custom", "text", "freq", "interval", "weekdays", "monthlyBy"))


class EventBook:
    def __init__(self, store: DavStore, zone: ZoneInfo, clock=time.time):
        self.store = store
        self.zone = zone
        self.clock = clock
        self.parsed: dict[tuple[str, str, str], icalendar.Calendar] = {}
        self.parsed_lock = threading.Lock()
        self.default_calendar_lock = threading.Lock()
        self.noted: set[tuple] = set()
        self.noted_lock = threading.Lock()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), self.zone).date()

    def calendars(self) -> list[CalendarInfo]:
        found = self.store.calendars()
        if found:
            return found
        with self.default_calendar_lock:
            found = self.store.calendars()
            if found:
                return found
            self.store.create_calendar(DEFAULT_CALENDAR_NAME, DEFAULT_COLOR, order=0)
            return self.store.calendars()

    def calendar_list(self) -> list[dict]:
        return [{"id": info.name, "name": info.display_name, "color": stored_color(info.color)} for info in self.calendars()]

    def create_calendar(self, name, color) -> dict:
        clean_name = _limited_text(name, "name", MAX_CALENDAR_NAME_LENGTH, required=True)
        clean = clean_color(color if color is not None else DEFAULT_COLOR)
        created = self.store.create_calendar(clean_name, clean, order=len(self.store.calendars()))
        return {"id": created, "name": clean_name, "color": clean}

    def change_calendar(self, calendar: str, name, color) -> dict:
        info = self.store.calendar(calendar)
        clean_name = None if name is None else _limited_text(name, "name", MAX_CALENDAR_NAME_LENGTH, required=True)
        clean = None if color is None else clean_color(color)
        self.store.change_calendar(calendar, clean_name, clean)
        return {"id": calendar, "name": clean_name or info.display_name, "color": clean or stored_color(info.color)}

    def delete_calendar(self, calendar: str) -> None:
        names = [info.name for info in self.store.calendars()]
        if calendar not in names:
            raise CalendarError("that calendar does not exist", 404)
        if len(names) == 1:
            raise CalendarError("this is the only calendar; make another before deleting it", 409)
        self.store.delete_calendar(calendar)

    def _parsed_items(self, info: CalendarInfo) -> list[tuple[StoredItem, icalendar.Calendar]]:
        result = []
        for item in self.store.items(info):
            key = (info.name, item.name, item.etag)
            with self.parsed_lock:
                parsed = self.parsed.get(key)
            if parsed is None:
                try:
                    parsed = parse_item(item.text)
                except CalendarError:
                    self._note_once(key, "skipped an unreadable item %s in calendar %s", item.name, info.name)
                    continue
                with self.parsed_lock:
                    self.parsed[key] = parsed
            result.append((item, parsed))
        return result

    def _note_once(self, key: tuple, message: str, *details, with_error: bool = False) -> None:
        with self.noted_lock:
            if (key, message) in self.noted:
                return
            self.noted.add((key, message))
        log.warning(message, *details, exc_info=with_error)

    def _instances_between(self, info: CalendarInfo, item: StoredItem, parsed, master, start: datetime,
                           end: datetime) -> tuple[list, bool]:
        if repeats_too_often_to_expand(master):
            self._note_once((info.name, item.name, item.etag), "item %s in calendar %s repeats more often than once an hour; "
                            "it is shown on its first date only", item.name, info.name)
            return ([master] if _overlaps(master, start, end, self.zone) else []), False
        found = recurring_ical_events.of(series_near(parsed, master, start, end)).between(start, end)
        if len(found) > MAX_OCCURRENCES_PER_ITEM:
            return found[:MAX_OCCURRENCES_PER_ITEM], True
        return found, False

    def _expand(self, info: CalendarInfo, item: StoredItem, parsed, start: datetime, end: datetime) -> tuple[list[Occurrence], bool]:
        try:
            master = find_master(parsed)
            instances, capped = self._instances_between(info, item, parsed, master, start, end)
        except Exception:
            self._note_once((info.name, item.name, item.etag), "skipped item %s in calendar %s: its dates could not be worked out",
                            item.name, info.name, with_error=True)
            return [], False
        recurring = is_repeating(master)
        color = stored_color(info.color)
        override_ids = {occurrence_key(event.get("RECURRENCE-ID").dt, self.zone) for event in _overrides(parsed)}
        found = []
        for instance in instances:
            recurrence = instance.get("RECURRENCE-ID")
            key = occurrence_key(recurrence.dt, self.zone) if recurrence is not None else None
            start_at, end_at = normalised_times(start_of(instance), end_of(instance), self.zone)
            found.append(Occurrence(info.name, item.name, item.etag, _text(master, "UID"), instance, master,
                                    start_at, end_at, recurring, color, key in override_ids))
        return found, capped

    def occurrences(self, from_day: date, to_day: date, calendars: list[str] | None = None) -> tuple[list[Occurrence], bool]:
        if to_day <= from_day:
            raise CalendarError("to must be after from")
        if (to_day - from_day).days > MAX_WINDOW_DAYS:
            raise CalendarError(f"ask for at most {MAX_WINDOW_DAYS} days at once")
        return self.occurrences_between(start_of_day(from_day, self.zone), start_of_day(to_day, self.zone), calendars)

    def occurrences_between(self, start: datetime, end: datetime, calendars: list[str] | None = None) -> tuple[list[Occurrence], bool]:
        found: list[Occurrence] = []
        seen_keys = set()
        any_capped = False
        wanted = None if calendars is None else set(calendars)
        for info in self.calendars():
            if wanted is not None and info.name not in wanted:
                continue
            for item, parsed in self._parsed_items(info):
                seen_keys.add((info.name, item.name, item.etag))
                expanded, capped = self._expand(info, item, parsed, start, end)
                found.extend(expanded)
                any_capped = any_capped or capped
        with self.parsed_lock:
            self.parsed = {key: value for key, value in self.parsed.items()
                           if key in seen_keys or (wanted is not None and key[0] not in wanted)}
        found.sort(key=lambda occurrence: (as_instant(occurrence.start, self.zone), occurrence.all_day is False, _text(occurrence.instance, "SUMMARY").lower()))
        truncated = any_capped or len(found) > MAX_OCCURRENCES
        return found[:MAX_OCCURRENCES], truncated

    def _checked_calendar(self, calendar) -> str:
        if not isinstance(calendar, str) or not is_safe_segment(calendar):
            raise CalendarError("calendarId must name one of your calendars", 404)
        if calendar not in {info.name for info in self.calendars()}:
            raise CalendarError("that calendar does not exist", 404)
        return calendar

    def create(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise CalendarError("send a JSON object")
        calendar = self._checked_calendar(raw.get("calendarId"))
        fields = clean_fields(raw, self.zone)
        uid = str(uuid.uuid4())
        event = icalendar.Event()
        event.add("UID", uid)
        event.add("CREATED", self.now())
        _apply_fields(event, fields, self.now())
        event.pop("SEQUENCE", None)
        event.add("SEQUENCE", 0)
        if fields.repeat:
            event.add("RRULE", to_rrule(fields.repeat, fields.start, self.zone))
        document = _new_calendar()
        document.add_component(event)
        item = f"{uid}.ics"
        self.store.put(calendar, item, document.to_ical().decode("utf-8"), None)
        return {"calendarId": calendar, "itemId": item, "uid": uid}

    def _instance(self, parsed, master, wanted) -> icalendar.Event:
        if repeats_too_often_to_expand(master):
            raise CalendarError(TOO_OFTEN_TEXT)
        for override in _overrides(parsed):
            if same_occurrence(override.get("RECURRENCE-ID").dt, wanted, self.zone):
                return override
        around = as_instant(wanted, self.zone)
        first, last = around - ONE_DAY * 2, around + ONE_DAY * 2
        for instance in recurring_ical_events.of(series_near(parsed, master, first, last)).between(first, last):
            recurrence = instance.get("RECURRENCE-ID")
            if recurrence is not None and same_occurrence(recurrence.dt, wanted, self.zone):
                return instance
        raise CalendarError("that date is not part of this repeating event (it may have been removed)", 404)

    def get(self, calendar: str, item: str, occurrence: str | None = None) -> dict:
        stored = self.store.item(calendar, item)
        parsed = parse_item(stored.text)
        master = find_master(parsed)
        recurring = is_repeating(master)
        instance = master
        if recurring and occurrence is not None:
            instance = self._instance(parsed, master, parse_occurrence_key(occurrence))
        info = self.store.calendar(calendar)
        start_at, end_at = normalised_times(start_of(instance), end_of(instance), self.zone)
        found = Occurrence(calendar, item, stored.etag, _text(master, "UID"), instance, master, start_at, end_at,
                           recurring, stored_color(info.color), _is_override(parsed, instance))
        return occurrence_json(found, self.zone)

    def _current_fields(self, component, master) -> dict:
        start, end = start_of(component), end_of(component)
        local_start, local_end = as_local(start, self.zone), as_local(end, self.zone)
        rule = from_rrule(_rrule_of(master), start_of(master), self.zone) if is_repeating(master) else None
        return {
            "title": _text(component, "SUMMARY"),
            "location": _text(component, "LOCATION"),
            "notes": _text(component, "DESCRIPTION"),
            "allDay": not isinstance(start, datetime),
            "start": local_text(local_start, self.zone),
            "end": _shown_end(local_start, local_end, self.zone),
            "repeat": None if rule is None or rule.get("custom") else rule,
            "reminders": reminders_of(component),
        }

    def _current_item(self, calendar: str, item: str, expected_etag: str | None) -> StoredItem:
        stored = self.store.item(calendar, item)
        if expected_etag is not None and expected_etag != stored.etag:
            raise ChangedElsewhere()
        return stored

    def update(self, calendar: str, item: str, raw: dict, scope: str = "all", occurrence: str | None = None,
               expected_etag: str | None = None) -> dict:
        if not isinstance(raw, dict):
            raise CalendarError("send a JSON object")
        if scope not in SCOPES:
            raise CalendarError("scope must be one or all")
        stored = self._current_item(calendar, item, expected_etag)
        parsed = parse_item(stored.text)
        master = find_master(parsed)
        target = raw.get("calendarId", calendar)
        if target != calendar:
            self._checked_calendar(target)
        if is_repeating(master) and scope == "one":
            if target != calendar:
                raise CalendarError("a single date can't move to another calendar; choose all dates to move the event")
            if "repeat" in raw:
                raise CalendarError("a single date can't repeat on its own; choose all dates to change how it repeats")
            self._update_one(parsed, master, raw, parse_occurrence_key(occurrence) if occurrence else None)
        else:
            self._update_all(parsed, master, raw, parse_occurrence_key(occurrence) if occurrence and is_repeating(master) else None)
        text = parsed.to_ical().decode("utf-8")
        if target == calendar:
            self.store.put(calendar, item, text, stored.etag)
        else:
            self.store.put(target, item, text, None)
            self.store.delete(calendar, item, stored.etag)
        return {"calendarId": target, "itemId": item, "uid": _text(master, "UID")}

    def _update_one(self, parsed, master, raw: dict, wanted) -> None:
        if wanted is None:
            raise CalendarError("say which date of the repeating event to change (occurrence)")
        instance = self._instance(parsed, master, wanted)
        merged = {**self._current_fields(instance, master), **{key: raw[key] for key in EDITABLE_FIELDS if key in raw}, "repeat": None}
        fields = clean_fields(merged, self.zone)
        if not _is_override(parsed, instance):
            for name in ("RRULE", "RDATE", "EXDATE"):
                instance.pop(name, None)
            parsed.add_component(instance)
        _apply_fields(instance, fields, self.now())

    def _update_all(self, parsed, master, raw: dict, wanted) -> None:
        shown = self._instance(parsed, master, wanted) if wanted is not None else master
        before = self._current_fields(shown, master)
        old_rule = from_rrule(_rrule_of(master), start_of(master), self.zone) if is_repeating(master) else None
        keeps_custom_rule = "repeat" not in raw and old_rule is not None and bool(old_rule.get("custom"))
        merged = {**before, **{key: raw[key] for key in EDITABLE_FIELDS if key in raw}}
        fields = clean_fields(merged, self.zone)
        kind_changed = fields.all_day != before["allDay"]
        repeat_removed = old_rule is not None and not keeps_custom_rule and not fields.repeat
        delta = timedelta(0) if kind_changed else _wall_delta(start_of(shown), fields.start, self.zone)
        if shown is master or kind_changed or repeat_removed:
            new_start = fields.start
        else:
            new_start = _shift_wall(start_of(master), delta, self.zone)
            if isinstance(new_start, datetime):
                new_start = as_local(new_start, self.zone)
        new_end = new_start + (fields.end - fields.start)
        repeat = _follow_moved_weekday(fields.repeat, old_rule, start_of(master), new_start)
        rule_changed = not keeps_custom_rule and _rule_shape(fields.repeat) != _rule_shape(old_rule)
        old_texts = {name: _text(master, prop) for name, prop in SHARED_TEXT_FIELDS}
        old_reminders = reminders_of(master)
        old_rrule = _rrule_of(master)
        _apply_fields(master, EventFields(fields.title, fields.location, fields.notes, fields.all_day, new_start, new_end,
                                          repeat, fields.reminders), self.now())
        master.pop("RRULE", None)
        if keeps_custom_rule:
            master.add("RRULE", old_rrule)
        elif repeat:
            master.add("RRULE", to_rrule(repeat, new_start, self.zone))
        if rule_changed or kind_changed or not is_repeating(master):
            _set_exdates(master, [])
            parsed.subcomponents = [sub for sub in parsed.subcomponents if not _is_override(parsed, sub)]
            return
        self._carry_exceptions(parsed, master, delta, old_texts, old_reminders, fields)

    def _carry_exceptions(self, parsed, master, delta: timedelta, old_texts: dict, old_reminders: list[int], fields: EventFields) -> None:
        if delta:
            _set_exdates(master, [_shift_wall(value, delta, self.zone) for value in _exdates(master)])
        new_texts = {"title": fields.title, "location": fields.location, "notes": fields.notes}
        for override in _overrides(parsed):
            if delta:
                recurrence = override.get("RECURRENCE-ID").dt
                override.pop("RECURRENCE-ID")
                override.add("RECURRENCE-ID", _shift_wall(recurrence, delta, self.zone))
                _set_times(override, _shift_wall(start_of(override), delta, self.zone), _shift_wall(end_of(override), delta, self.zone))
            for name, prop in SHARED_TEXT_FIELDS:
                if _text(override, prop) == old_texts[name]:
                    _set_text(override, prop, new_texts[name])
            if reminders_of(override) == old_reminders:
                _set_reminders(override, fields.reminders, fields.title)
            _stamp(override, self.now())

    def delete(self, calendar: str, item: str, scope: str = "all", occurrence: str | None = None,
               expected_etag: str | None = None) -> None:
        if scope not in SCOPES:
            raise CalendarError("scope must be one or all")
        stored = self._current_item(calendar, item, expected_etag)
        parsed = parse_item(stored.text)
        master = find_master(parsed)
        if scope == "all" or not is_repeating(master):
            self.store.delete(calendar, item, stored.etag)
            return
        if occurrence is None:
            raise CalendarError("say which date of the repeating event to delete (occurrence)")
        wanted = parse_occurrence_key(occurrence)
        self._instance(parsed, master, wanted)
        parsed.subcomponents = [sub for sub in parsed.subcomponents if not (
            sub.name == "VEVENT" and sub.get("RECURRENCE-ID") is not None and same_occurrence(sub.get("RECURRENCE-ID").dt, wanted, self.zone))]
        master.add("EXDATE", _like_start(wanted, start_of(master), self.zone))
        _stamp(master, self.now())
        self.store.put(calendar, item, parsed.to_ical().decode("utf-8"), stored.etag)


def _day_of(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _is_override(parsed, component) -> bool:
    return component.get("RECURRENCE-ID") is not None and any(component is override for override in _overrides(parsed))


def _follow_moved_weekday(repeat: dict | None, old_rule: dict | None, old_start, new_start) -> dict | None:
    if not repeat or repeat["freq"] != "weekly" or not old_rule or old_rule.get("custom"):
        return repeat
    old_weekday = WEEKDAY_CODES[_day_of(old_start).weekday()]
    new_weekday = WEEKDAY_CODES[_day_of(new_start).weekday()]
    if old_weekday == new_weekday or repeat.get("weekdays") != [old_weekday] or old_rule.get("weekdays") != [old_weekday]:
        return repeat
    return {**repeat, "weekdays": [new_weekday]}
