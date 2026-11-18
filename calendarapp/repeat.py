from datetime import date, datetime
from zoneinfo import ZoneInfo

from icalendar import vRecur

from .errors import CalendarError
from .when import as_local, end_of_day_utc, parse_day

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ORDINAL_NAMES = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}
MONTHLY_MODES = ("day", "weekday")
MAX_INTERVAL = 99
MAX_COUNT = 999
DAYS_IN_WEEK = 7
LAST_NAMED_WEEK = 4
LAST = -1
UNITS = {"daily": ("day", "days"), "weekly": ("week", "weeks"), "monthly": ("month", "months"), "yearly": ("year", "years")}
UNDERSTOOD_PARTS = {"FREQ", "INTERVAL", "UNTIL", "COUNT", "BYDAY", "WKST", "BYMONTHDAY", "BYMONTH"}


def nth_weekday(day: date) -> int:
    week = (day.day - 1) // DAYS_IN_WEEK + 1
    return LAST if week > LAST_NAMED_WEEK else week


def _whole_number(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CalendarError(f"{field} must be a whole number from {low} to {high}")
    return value


def clean_repeat(value, start_day: date) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CalendarError("repeat must be an object like {\"freq\": \"weekly\"} or null")
    freq = value.get("freq")
    if freq not in FREQUENCIES:
        raise CalendarError(f"repeat.freq must be one of {', '.join(FREQUENCIES)}")
    rule = {"freq": freq, "interval": _whole_number(value.get("interval", 1), "repeat.interval", 1, MAX_INTERVAL)}
    if freq == "weekly":
        rule["weekdays"] = _clean_weekdays(value.get("weekdays"), start_day)
    if freq == "monthly":
        mode = value.get("monthlyBy", "day")
        if mode not in MONTHLY_MODES:
            raise CalendarError("repeat.monthlyBy must be day or weekday")
        rule["monthlyBy"] = mode
    until, count = value.get("until"), value.get("count")
    if until is not None and count is not None:
        raise CalendarError("a repeat can end on a date or after a number of times, not both")
    if until is not None:
        until_day = parse_day(until, "repeat.until")
        if until_day < start_day:
            raise CalendarError("repeat.until is before the event starts")
        rule["until"] = until_day.isoformat()
    if count is not None:
        rule["count"] = _whole_number(count, "repeat.count", 1, MAX_COUNT)
    return rule


def _clean_weekdays(value, start_day: date) -> list[str]:
    if value is None or value == []:
        return [WEEKDAY_CODES[start_day.weekday()]]
    if not isinstance(value, list) or not all(isinstance(code, str) and code in WEEKDAY_CODES for code in value):
        raise CalendarError(f"repeat.weekdays must be a list of {', '.join(WEEKDAY_CODES)}")
    return [code for code in WEEKDAY_CODES if code in value]


def to_rrule(rule: dict, start, zone: ZoneInfo) -> vRecur:
    start_day = start.date() if isinstance(start, datetime) else start
    parts = {"FREQ": rule["freq"].upper()}
    if rule["interval"] != 1:
        parts["INTERVAL"] = rule["interval"]
    if rule["freq"] == "weekly":
        parts["BYDAY"] = rule["weekdays"]
    if rule["freq"] == "monthly" and rule.get("monthlyBy") == "weekday":
        parts["BYDAY"] = [f"{nth_weekday(start_day)}{WEEKDAY_CODES[start_day.weekday()]}"]
    if "until" in rule:
        until_day = date.fromisoformat(rule["until"])
        parts["UNTIL"] = end_of_day_utc(until_day, zone) if isinstance(start, datetime) else until_day
    if "count" in rule:
        parts["COUNT"] = rule["count"]
    return vRecur(parts)


def _single(recur: vRecur, name: str):
    values = recur.get(name)
    if not values:
        return None
    return values[0] if isinstance(values, list) else values


def _weekday_parts(recur: vRecur) -> list[str]:
    values = recur.get("BYDAY") or []
    return [str(value).upper() for value in (values if isinstance(values, list) else [values])]


def from_rrule(recur: vRecur | None, start, zone: ZoneInfo) -> dict | None:
    if recur is None:
        return None
    start_day = start.date() if isinstance(start, datetime) else start
    freq = str(_single(recur, "FREQ") or "").lower()
    if freq not in FREQUENCIES or set(recur) - UNDERSTOOD_PARTS:
        return {"custom": True, "text": recur.to_ical().decode("ascii", "replace")}
    rule = {"freq": freq, "interval": int(_single(recur, "INTERVAL") or 1)}
    weekdays = _weekday_parts(recur)
    if freq == "weekly":
        if any(code not in WEEKDAY_CODES for code in weekdays):
            return {"custom": True, "text": recur.to_ical().decode("ascii", "replace")}
        rule["weekdays"] = [code for code in WEEKDAY_CODES if code in weekdays] or [WEEKDAY_CODES[start_day.weekday()]]
    elif freq == "monthly":
        rule["monthlyBy"] = "weekday" if weekdays else "day"
        if weekdays and weekdays != [f"{nth_weekday(start_day)}{WEEKDAY_CODES[start_day.weekday()]}"]:
            return {"custom": True, "text": recur.to_ical().decode("ascii", "replace")}
    elif weekdays:
        return {"custom": True, "text": recur.to_ical().decode("ascii", "replace")}
    if _has_unusual_month_parts(recur, freq, start_day):
        return {"custom": True, "text": recur.to_ical().decode("ascii", "replace")}
    until, count = _single(recur, "UNTIL"), _single(recur, "COUNT")
    if until is not None:
        local_until = as_local(until, zone)
        rule["until"] = (local_until.date() if isinstance(local_until, datetime) else local_until).isoformat()
    if count is not None:
        rule["count"] = int(count)
    return rule


def _has_unusual_month_parts(recur: vRecur, freq: str, start_day: date) -> bool:
    month_days = recur.get("BYMONTHDAY")
    months = recur.get("BYMONTH")
    if month_days and [int(value) for value in (month_days if isinstance(month_days, list) else [month_days])] != [start_day.day]:
        return True
    if months and (freq != "yearly" or [int(value) for value in (months if isinstance(months, list) else [months])] != [start_day.month]):
        return True
    return False


def describe(rule: dict | None, start) -> str:
    if rule is None:
        return ""
    if rule.get("custom"):
        return "Repeats (a custom rule made on another device)"
    start_day = start.date() if isinstance(start, datetime) else start
    singular, plural = UNITS[rule["freq"]]
    interval = rule["interval"]
    text = f"Every {singular}" if interval == 1 else f"Every {interval} {plural}"
    if rule["freq"] == "weekly":
        text += " on " + ", ".join(WEEKDAY_NAMES[WEEKDAY_CODES.index(code)] for code in rule["weekdays"])
    elif rule["freq"] == "monthly" and rule.get("monthlyBy") == "weekday":
        text += f" on the {ORDINAL_NAMES[nth_weekday(start_day)]} {start_day.strftime('%A')}"
    elif rule["freq"] == "monthly":
        text += f" on day {start_day.day}"
    elif rule["freq"] == "yearly":
        text += f" on {start_day.day} {start_day.strftime('%B')}"
    if "count" in rule:
        text += ", once" if rule["count"] == 1 else f", {rule['count']} times"
    if "until" in rule:
        until_day = date.fromisoformat(rule["until"])
        text += f", until {until_day.day} {until_day.strftime('%b %Y')}"
    return text
