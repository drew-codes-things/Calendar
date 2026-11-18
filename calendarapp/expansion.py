from datetime import MAXYEAR, date, datetime, timedelta

import icalendar
import x_wr_timezone
from dateutil.rrule import rrulestr

WINDOW_MARGIN = timedelta(days=7)
FIXED_STEPS = {"HOURLY": timedelta(hours=1), "DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1)}
MONTHS_PER_STEP = {"MONTHLY": 1, "YEARLY": 12}
MONTHS_PER_YEAR = 12
MAX_STEPS_BACK = 400
RULE_PARTS_LEFT_OUT_OF_SEARCH = ("UNTIL", "COUNT")
RULE_PROPERTIES = ("RRULE", "RDATE", "EXDATE")
CALENDAR_CYCLE_YEARS = 400
RULE_PARTS_THAT_REPEAT_WITH_THE_CALENDAR = frozenset({
    "FREQ", "INTERVAL", "UNTIL", "COUNT", "BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY", "BYYEARDAY",
    "BYWEEKNO", "BYMONTH", "BYSETPOS", "WKST"})
RULE_PARTS_KEPT_WITH_THE_START_ONLY = ("UNTIL",)
START_ONLY_RULE = {"FREQ": ["YEARLY"], "INTERVAL": [MAXYEAR + 1]}
X_WR_TIMEZONE = "X-WR-TIMEZONE"
TIME_PARTS_IN_A_PERIOD = {"HOURLY": ("BYMINUTE", "BYSECOND"), "DAILY": ("BYHOUR", "BYMINUTE", "BYSECOND")}


def series_near(calendar: icalendar.Calendar, master: icalendar.Event, window_start: datetime,
                window_end: datetime) -> icalendar.Calendar:
    standard, master = _in_standard_time(calendar, master)
    rule = _single_rule(standard, master)
    if rule is None:
        return calendar
    first = master["DTSTART"].dt
    search_limit = _latest_start_needed(window_end, first)
    if search_limit is None:
        return calendar
    period_start = None
    length = _length_if_known(master)
    if length is not None:
        earliest_needed = _earliest_start_needed(window_start, length)
        if earliest_needed is None:
            return calendar
        period_start = _aligned_period_start(rule, _wall_time(first, first), _wall_time(earliest_needed, first))
    search_start = _wall_time(first, first) if period_start is None else period_start
    new_start = _first_rule_date_between(rule, _like(first, search_start), _like(first, search_limit))
    if new_start is None:
        return _with_master_replaced(standard, master, _with_start_only(master, rule))
    if period_start is None or "COUNT" in rule or X_WR_TIMEZONE in calendar or not _has_plain_end(master):
        return calendar
    return _with_master_moved(calendar, master, new_start)


def _in_standard_time(calendar: icalendar.Calendar,
                      master: icalendar.Event) -> tuple[icalendar.Calendar, icalendar.Event | None]:
    if X_WR_TIMEZONE not in calendar:
        return calendar, master
    position = next((index for index, component in enumerate(calendar.subcomponents) if component is master), None)
    if position is None:
        return calendar, None
    standard = x_wr_timezone.to_standard(calendar)
    return standard, standard.subcomponents[position]


def _single_rule(calendar: icalendar.Calendar, master: icalendar.Event | None) -> icalendar.vRecur | None:
    if master is None:
        return None
    rule = master.get("RRULE")
    if not isinstance(rule, icalendar.vRecur):
        return None
    frequency, interval = _value(rule, "FREQ"), _value(rule, "INTERVAL", 1)
    if frequency not in FIXED_STEPS and frequency not in MONTHS_PER_STEP:
        return None
    if not isinstance(interval, int) or interval < 1:
        return None
    if not _is_searchable(calendar, master):
        return None
    if frequency == "HOURLY" and not isinstance(master["DTSTART"].dt, datetime):
        return None
    return rule


def _is_searchable(calendar: icalendar.Calendar, master: icalendar.Event) -> bool:
    if not any(component is master for component in calendar.subcomponents):
        return False
    events = calendar.walk("VEVENT")
    if sum(1 for event in events if event.get("RECURRENCE-ID") is None) != 1 or master.get("RECURRENCE-ID") is not None:
        return False
    if any(_changes_later_dates(event) for event in events if event is not master):
        return False
    return _has_plain_start(master)


def _changes_later_dates(override: icalendar.Event) -> bool:
    recurrence = override.get("RECURRENCE-ID")
    if recurrence is not None and str(recurrence.params.get("RANGE", "")).upper() == "THISANDFUTURE":
        return True
    return any(name in override for name in RULE_PROPERTIES)


def _has_plain_start(master: icalendar.Event) -> bool:
    start = master.get("DTSTART")
    if start is None or not isinstance(start.dt, date):
        return False
    return not (isinstance(start.dt, datetime) and hasattr(start.dt.tzinfo, "localize"))


def _has_plain_end(master: icalendar.Event) -> bool:
    end = master.get("DTEND")
    return end is None or _same_kind(master["DTSTART"].dt, end.dt)


def _same_kind(first, second) -> bool:
    if isinstance(first, datetime) != isinstance(second, datetime) or not isinstance(second, date):
        return False
    return not isinstance(first, datetime) or first.tzinfo is second.tzinfo


def _value(rule: icalendar.vRecur, name: str, default=None):
    values = rule.get(name)
    if values is None:
        return default
    if isinstance(values, list):
        return values[0] if len(values) == 1 else None
    return values


def _length_if_known(master: icalendar.Event) -> timedelta | None:
    start, end = master["DTSTART"].dt, master.get("DTEND")
    if end is not None and not _comparable(start, end.dt):
        return None
    return _length_of(master)


def _comparable(first, second) -> bool:
    if isinstance(first, datetime) != isinstance(second, datetime) or not isinstance(second, date):
        return False
    return not isinstance(first, datetime) or (first.tzinfo is None) == (second.tzinfo is None)


def _length_of(master: icalendar.Event) -> timedelta:
    start = master["DTSTART"].dt
    if master.get("DTEND") is not None:
        return abs(master["DTEND"].dt - start)
    if master.get("DURATION") is not None:
        return abs(master["DURATION"].dt)
    return FIXED_STEPS["DAILY"]


def _earliest_start_needed(window_start: datetime, length: timedelta) -> datetime | None:
    try:
        return window_start - length - WINDOW_MARGIN
    except OverflowError:
        return None


def _latest_start_needed(window_end: datetime, first: datetime | date) -> datetime | date | None:
    try:
        return _wall_time(window_end, first) + WINDOW_MARGIN
    except OverflowError:
        return None


def _wall_time(moment: datetime | date, first: datetime | date) -> datetime | date:
    if not isinstance(first, datetime):
        return moment.date() if isinstance(moment, datetime) else moment
    if first.tzinfo is not None and moment.tzinfo is not None:
        moment = moment.astimezone(first.tzinfo)
    return moment.replace(tzinfo=None)


def _aligned_period_start(rule: icalendar.vRecur, first, target):
    if target <= first:
        return None
    frequency, interval = _value(rule, "FREQ"), _value(rule, "INTERVAL", 1)
    if frequency in FIXED_STEPS:
        step = FIXED_STEPS[frequency] * interval
        periods = (target - first) // step
        return first + step * periods if periods > 0 else None
    months_per_period = MONTHS_PER_STEP[frequency] * interval
    months_apart = (target.year - first.year) * MONTHS_PER_YEAR + target.month - first.month
    return _latest_valid_period(first, target, months_apart // months_per_period, months_per_period)


def _latest_valid_period(first, target, periods: int, months_per_period: int):
    for steps_back in range(MAX_STEPS_BACK):
        periods_after_first = periods - steps_back
        if periods_after_first <= 0:
            return None
        candidate = _months_later(first, periods_after_first * months_per_period)
        if candidate is not None and candidate <= target:
            return candidate
    return None


def _months_later(first, months: int):
    month_index = first.month - 1 + months
    year, month = first.year + month_index // MONTHS_PER_YEAR, month_index % MONTHS_PER_YEAR + 1
    try:
        return first.replace(year=year, month=month)
    except ValueError:
        return None


def _like(first: datetime | date, wall: datetime | date) -> datetime | date:
    if isinstance(first, datetime):
        return wall.replace(tzinfo=first.tzinfo)
    return wall


def _as_rule_start(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _first_rule_date_between(rule: icalendar.vRecur, start: datetime | date,
                             limit: datetime | date) -> datetime | date | None:
    rule_start, rule_limit = _as_rule_start(start), _as_rule_start(limit)
    starts_after_the_limit = rule_start > rule_limit
    years_skipped = 0 if starts_after_the_limit else _whole_cycles_skippable(rule, rule_limit) * CALENDAR_CYCLE_YEARS
    searched = icalendar.vRecur({name: value for name, value in rule.items() if name not in RULE_PARTS_LEFT_OUT_OF_SEARCH})
    rule_dates = rrulestr(searched.to_ical().decode("ascii"), dtstart=_years_later(rule_start, years_skipped))
    if starts_after_the_limit or _no_period_holds_a_set_position(rule):
        return None
    found = _first_date_before_the_last_year(rule_dates)
    if found is None:
        return None
    found = _years_later(found, -years_skipped)
    if found > rule_limit:
        return None
    return found if isinstance(start, datetime) else found.date()


def _first_date_before_the_last_year(rule_dates) -> datetime | None:
    try:
        return next(iter(rule_dates), None)
    except (ValueError, OverflowError):
        return None


def _no_period_holds_a_set_position(rule: icalendar.vRecur) -> bool:
    time_parts = TIME_PARTS_IN_A_PERIOD.get(_value(rule, "FREQ"))
    positions = rule.get("BYSETPOS")
    if time_parts is None or not positions:
        return False
    times_in_a_period = 1
    for part in time_parts:
        times_in_a_period *= len(set(rule.get(part) or [None]))
    return all(abs(position) > times_in_a_period for position in positions)


def _whole_cycles_skippable(rule: icalendar.vRecur, limit: datetime) -> int:
    if not set(rule) <= RULE_PARTS_THAT_REPEAT_WITH_THE_CALENDAR:
        return 0
    return max(MAXYEAR - 1 - limit.year, 0) // CALENDAR_CYCLE_YEARS


def _years_later(moment: datetime, years: int) -> datetime:
    return moment.replace(year=moment.year + years)


def _with_start_only(master: icalendar.Event, rule: icalendar.vRecur) -> icalendar.Event:
    replacement = master.copy()
    replacement.subcomponents = list(master.subcomponents)
    kept = {name: rule[name] for name in RULE_PARTS_KEPT_WITH_THE_START_ONLY if name in rule}
    replacement["RRULE"] = icalendar.vRecur({**START_ONLY_RULE, **kept})
    return replacement


def _with_master_moved(calendar: icalendar.Calendar, master: icalendar.Event, new_start) -> icalendar.Calendar:
    moved = master.copy()
    moved.subcomponents = list(master.subcomponents)
    old_start = master["DTSTART"].dt
    moved["DTSTART"] = icalendar.vDDDTypes(new_start)
    if master.get("DTEND") is not None:
        moved["DTEND"] = icalendar.vDDDTypes(new_start + (master["DTEND"].dt - old_start))
    return _with_master_replaced(calendar, master, moved)


def _with_master_replaced(calendar: icalendar.Calendar, master: icalendar.Event,
                          replacement: icalendar.Event) -> icalendar.Calendar:
    document = calendar.copy()
    document.subcomponents = [replacement if component is master else component for component in calendar.subcomponents]
    return document
