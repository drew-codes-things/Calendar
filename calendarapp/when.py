import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import CalendarError

EARLIEST_DAY = date(1900, 1, 1)
LATEST_DAY = date(2999, 12, 31)
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
LOCAL_TIME_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):([0-5]\d)$", re.ASCII)
DAY_HINT = "a date like 2027-01-31"
LOCAL_TIME_HINT = "a date and time like 2027-01-31T19:30"


def parse_day(value, field: str) -> date:
    if not isinstance(value, str) or not DAY_PATTERN.match(value):
        raise CalendarError(f"{field} must be {DAY_HINT}")
    try:
        day = date.fromisoformat(value)
    except ValueError as error:
        raise CalendarError(f"{field} must be {DAY_HINT}, and a day that exists") from error
    if not EARLIEST_DAY <= day <= LATEST_DAY:
        raise CalendarError(f"{field} must fall between {EARLIEST_DAY.isoformat()} and {LATEST_DAY.isoformat()}")
    return day


def exists_on_the_clock(moment: datetime) -> bool:
    round_trip = moment.astimezone(timezone.utc).astimezone(moment.tzinfo)
    return round_trip.replace(tzinfo=None) == moment.replace(tzinfo=None)


def local_moment(day: date, clock: time, zone: ZoneInfo, field: str) -> datetime:
    moment = datetime.combine(day, clock, tzinfo=zone)
    if not exists_on_the_clock(moment):
        raise CalendarError(f"{field}: {clock.strftime('%H:%M')} does not happen on {day.isoformat()} in {zone.key} "
                            "because the clocks go forward then; pick a time an hour later")
    return moment


def parse_local_time(value, field: str, zone: ZoneInfo) -> datetime:
    match = LOCAL_TIME_PATTERN.match(value) if isinstance(value, str) else None
    if match is None:
        raise CalendarError(f"{field} must be {LOCAL_TIME_HINT}")
    day = parse_day(match.group(1), field)
    return local_moment(day, time(int(match.group(2)), int(match.group(3))), zone, field)


def as_local(value, zone: ZoneInfo) -> date | datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=zone)
        return value.astimezone(zone)
    return value


def start_of_day(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=zone)


def as_instant(value, zone: ZoneInfo) -> datetime:
    local = as_local(value, zone)
    if isinstance(local, datetime):
        return local
    return start_of_day(local, zone)


def local_text(value, zone: ZoneInfo) -> str:
    local = as_local(value, zone)
    if isinstance(local, datetime):
        return local.strftime("%Y-%m-%dT%H:%M")
    return local.isoformat()


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def occurrence_key(value, zone: ZoneInfo) -> str:
    if isinstance(value, datetime):
        return utc_text(as_local(value, zone))
    return value.isoformat()


def parse_occurrence_key(value) -> date | datetime:
    if not isinstance(value, str):
        raise CalendarError("occurrence must name one date of a repeating event")
    if DAY_PATTERN.match(value):
        return parse_day(value, "occurrence")
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise CalendarError("occurrence must name one date of a repeating event") from error
    return moment


def same_occurrence(stored, wanted, zone: ZoneInfo) -> bool:
    if isinstance(wanted, datetime) != isinstance(stored, datetime):
        return False
    if isinstance(wanted, datetime):
        return as_local(stored, zone) == wanted
    return stored == wanted


def end_of_day_utc(day: date, zone: ZoneInfo) -> datetime:
    return (start_of_day(day + timedelta(days=1), zone) - timedelta(seconds=1)).astimezone(timezone.utc)
