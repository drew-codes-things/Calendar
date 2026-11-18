import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .errors import CalendarError, storable_text
from .repeat import WEEKDAY_CODES
from .when import EARLIEST_DAY, LATEST_DAY

MAX_TEXT_LENGTH = 500
DAYS_IN_WEEK = 7
LEAP_YEAR_SEARCH = 8
DEFAULT_MINUTES = 60
TONIGHT_TIME = "19:00"
NOON = "12:00"
MAX_FOR_HOURS = 24 * 14
MAX_FOR_MINUTES = 60 * 24 * 14
MAX_RELATIVE_AMOUNT = 999
TRAILING_PUNCTUATION = ",;."
WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
PLURAL_WEEKDAYS = {f"{name}s": number for name, number in WEEKDAYS.items() if len(name) > 3}
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
WORDS_THAT_ARE_ALSO_DATES = ("sun", "sat", "wed", "mar", "may", "jan")
TOMORROW_WORDS = ("tomorrow", "tmrw", "tmr")
NOON_WORDS = ("noon", "midday")
RANGE_WORDS = ("to", "until", "till", "-", "–")
REPEAT_UNITS = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}
SIMPLE_REPEATS = {"daily": "daily", "weekly": "weekly", "monthly": "monthly", "yearly": "yearly", "annually": "yearly"}
HOUR_WORDS = ("h", "hr", "hrs", "hour", "hours")
MINUTE_WORDS = ("m", "min", "mins", "minute", "minutes")
DAY_WORDS = ("day", "days")
WEEK_WORDS = ("week", "weeks")
NUMBER = re.compile(r"^\d{1,3}$", re.ASCII)
ORDINAL = re.compile(r"^(\d{1,2})(st|nd|rd|th)?$", re.ASCII)
YEAR = re.compile(r"^\d{4}$", re.ASCII)
SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", re.ASCII)
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$", re.ASCII)
TWELVE_HOUR = re.compile(r"^(\d{1,2})(?:[:.]([0-5]\d))?(am|pm)$", re.ASCII)
TWENTY_FOUR_HOUR = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$", re.ASCII)
TIME_RANGE = re.compile(r"^(\d{1,2}(?:[:.][0-5]\d)?(?:am|pm)?)[-–](\d{1,2}(?:[:.][0-5]\d)?(?:am|pm)?)$", re.ASCII)
JOINED_DURATION = re.compile(r"^(\d{1,3})(h|hr|hrs|m|min|mins)$", re.ASCII)
MERIDIEM = ("am", "pm")
CALENDAR_TAG = re.compile(r"^#([^\W_][\w-]{0,49})$")
QUOTED_OR_WORD = re.compile(r'"([^"]*)"|(\S+)')


@dataclass
class Token:
    text: str
    word: str | None


@dataclass
class Found:
    size: int
    value: object
    is_guess: bool = False


@dataclass
class Parsed:
    title: str
    start_day: date
    end_day: date
    start_time: str | None = None
    end_time: str | None = None
    repeat: dict | None = None
    location: str = ""
    calendar: str | None = None
    notes: list[str] = field(default_factory=list)

    def fields(self) -> dict:
        result = {"title": self.title, "location": self.location, "repeat": self.repeat}
        if self.start_time is None:
            result.update(allDay=True, start=self.start_day.isoformat(), end=self.end_day.isoformat())
            return result
        start = datetime.combine(self.start_day, _clock_time(self.start_time))
        end = datetime.combine(self.end_day, _clock_time(self.end_time)) if self.end_time else start + timedelta(minutes=DEFAULT_MINUTES)
        if end <= start and self.end_time and self.end_day == self.start_day:
            end += timedelta(days=1)
        result.update(allDay=False, start=start.strftime("%Y-%m-%dT%H:%M"), end=end.strftime("%Y-%m-%dT%H:%M"))
        return result


def _clock_time(text: str):
    hours, minutes = (int(part) for part in text.split(":"))
    return datetime.min.replace(hour=hours, minute=minutes).time()


def _tokens(text: str) -> list[Token]:
    tokens = []
    for match in QUOTED_OR_WORD.finditer(text):
        if match.group(1) is not None:
            if match.group(1).strip():
                tokens.append(Token(match.group(1).strip(), None))
            continue
        raw = match.group(2)
        tokens.append(Token(raw, raw.lower().rstrip(TRAILING_PUNCTUATION)))
    return tokens


def _word(tokens: list[Token], index: int) -> str | None:
    return tokens[index].word if 0 <= index < len(tokens) else None


def _real_date(year: int, month: int, day: int) -> date | None:
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    return candidate if EARLIEST_DAY <= candidate <= LATEST_DAY else None


def _date_without_year(month: int, day: int, today: date) -> date | None:
    for year in range(today.year, today.year + LEAP_YEAR_SEARCH + 1):
        candidate = _real_date(year, month, day)
        if candidate and candidate >= today:
            return candidate
    return None


def _day_and_month(day: int, month: int, year_word: str | None, today: date) -> tuple[date | None, bool]:
    if year_word and YEAR.match(year_word):
        return _real_date(int(year_word), month, day), True
    return _date_without_year(month, day, today), False


def _this_or_next(today: date, weekday: int) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % DAYS_IN_WEEK)


def _in_next_week(today: date, weekday: int) -> date:
    return today + timedelta(days=DAYS_IN_WEEK - today.weekday() + weekday)


def _relative_day(tokens: list[Token], index: int, today: date) -> Found | None:
    word = _word(tokens, index)
    if word in ("today", "tonight"):
        return Found(1, today)
    if word in TOMORROW_WORDS:
        return Found(1, today + timedelta(days=1))
    if word in WEEKDAYS:
        return Found(1, _this_or_next(today, WEEKDAYS[word]), is_guess=word in WORDS_THAT_ARE_ALSO_DATES)
    if word == "next" and _word(tokens, index + 1) in WEEKDAYS:
        return Found(2, _in_next_week(today, WEEKDAYS[_word(tokens, index + 1)]))
    if word == "in":
        amount, unit = _word(tokens, index + 1), _word(tokens, index + 2)
        if amount and NUMBER.match(amount) and 0 < int(amount) <= MAX_RELATIVE_AMOUNT:
            if unit in DAY_WORDS:
                return Found(3, today + timedelta(days=int(amount)))
            if unit in WEEK_WORDS:
                return Found(3, today + timedelta(weeks=int(amount)))
    return None


def _calendar_day(tokens: list[Token], index: int, today: date) -> Found | None:
    word = _word(tokens, index)
    if word is None:
        return None
    iso = ISO_DATE.match(word)
    if iso:
        found = _real_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return Found(1, found) if found else None
    slash = SLASH_DATE.match(word)
    if slash:
        day, month = int(slash.group(1)), int(slash.group(2))
        found, _ = _day_and_month(day, month, slash.group(3), today) if 1 <= month <= 12 else (None, False)
        return Found(1, found) if found else None
    ordinal = ORDINAL.match(word)
    if ordinal:
        offset = 2 if _word(tokens, index + 1) == "of" else 1
        month_word = _word(tokens, index + offset)
        if month_word in MONTHS:
            found, used_year = _day_and_month(int(ordinal.group(1)), MONTHS[month_word], _word(tokens, index + offset + 1), today)
            return Found(offset + 1 + used_year, found) if found else None
    if word in MONTHS:
        day_match = ORDINAL.match(_word(tokens, index + 1) or "")
        if day_match:
            found, used_year = _day_and_month(int(day_match.group(1)), MONTHS[word], _word(tokens, index + 2), today)
            return Found(2 + used_year, found, is_guess=word in WORDS_THAT_ARE_ALSO_DATES) if found else None
    return None


def _date_phrase(tokens: list[Token], index: int, today: date) -> Found | None:
    lead = 1 if _word(tokens, index) in ("on", "this") else 0
    found = _relative_day(tokens, index + lead, today) or _calendar_day(tokens, index + lead, today)
    if found is None:
        return None
    return Found(found.size + lead, found.value, is_guess=found.is_guess and not lead)


def _clock(hours: int, minutes: int) -> str:
    return f"{hours:02d}:{minutes:02d}"


def _twelve_hour(hour: int, minutes: int, meridiem: str) -> str | None:
    if not 1 <= hour <= 12:
        return None
    return _clock(hour % 12 + (12 if meridiem == "pm" else 0), minutes)


def _clock_word(word: str, meridiem_after: str | None, bare_hours_allowed: bool) -> tuple[str, int] | None:
    if word in NOON_WORDS:
        return "12:00", 1
    twelve = TWELVE_HOUR.match(word)
    if twelve:
        found = _twelve_hour(int(twelve.group(1)), int(twelve.group(2) or 0), twelve.group(3))
        return (found, 1) if found else None
    twenty_four = TWENTY_FOUR_HOUR.match(word.replace(".", ":"))
    if twenty_four:
        hour, minutes = int(twenty_four.group(1)), int(twenty_four.group(2))
        if meridiem_after in MERIDIEM and _twelve_hour(hour, minutes, meridiem_after):
            return _twelve_hour(hour, minutes, meridiem_after), 2
        return _clock(hour, minutes), 1
    if word.isascii() and word.isdigit() and len(word) <= 2:
        if meridiem_after in MERIDIEM:
            found = _twelve_hour(int(word), 0, meridiem_after)
            return (found, 2) if found else None
        if bare_hours_allowed and int(word) <= 23:
            return _afternoon_guess(int(word)), 1
    return None


def _evening(clock: str | None) -> str | None:
    if clock is None or clock >= NOON:
        return clock
    hours, minutes = clock.split(":")
    return _clock(int(hours) + 12, int(minutes))


def _afternoon_guess(hour: int) -> str:
    return _clock(hour + 12 if 1 <= hour <= 7 else hour, 0)


def _time_at(tokens: list[Token], index: int, bare_hours_allowed: bool) -> Found | None:
    word = _word(tokens, index)
    if word is None:
        return None
    found = _clock_word(word, _word(tokens, index + 1), bare_hours_allowed)
    return Found(found[1], found[0]) if found else None


def _range_from_joined(word: str) -> tuple[str, str] | None:
    match = TIME_RANGE.match(word)
    if not match:
        return None
    first, second = match.group(1), match.group(2)
    end = _clock_word(second, None, True)
    meridiem = second[-2:] if second[-2:] in MERIDIEM else None
    start = _clock_word(first, None, False) or (_clock_word(first, meridiem, True) if meridiem else _clock_word(first, None, True))
    if not start or not end:
        return None
    if meridiem and not first.endswith(MERIDIEM) and start[0] > end[0]:
        start = _clock_word(first, "am", True)
    return start[0], end[0]


def _time_phrase(tokens: list[Token], index: int) -> Found | None:
    lead = 1 if _word(tokens, index) in ("at", "from", "@") else 0
    word = _word(tokens, index + lead)
    if word is None:
        return None
    joined = _range_from_joined(word)
    if joined:
        return Found(lead + 1, joined)
    start = _time_at(tokens, index + lead, bare_hours_allowed=bool(lead))
    if start is None:
        return None
    size = lead + start.size
    end = _range_end(tokens, index + size)
    if end:
        return Found(size + end.size, (start.value, end.value))
    return Found(size, (start.value, None))


def _range_end(tokens: list[Token], index: int) -> Found | None:
    if _word(tokens, index) not in RANGE_WORDS:
        return None
    end = _time_at(tokens, index + 1, bare_hours_allowed=True)
    return Found(end.size + 1, end.value) if end else None


def _duration_phrase(tokens: list[Token], index: int) -> Found | None:
    if _word(tokens, index) != "for":
        return None
    amount, unit = _word(tokens, index + 1), _word(tokens, index + 2)
    joined = JOINED_DURATION.match(amount or "")
    if joined:
        amount, unit, size = joined.group(1), joined.group(2), 2
    else:
        size = 3
    if not amount or not NUMBER.match(amount) or int(amount) == 0:
        return None
    if unit in HOUR_WORDS and int(amount) <= MAX_FOR_HOURS:
        return Found(size, int(amount) * 60)
    if unit in MINUTE_WORDS and int(amount) <= MAX_FOR_MINUTES:
        return Found(size, int(amount))
    return None


def _all_day_phrase(tokens: list[Token], index: int) -> Found | None:
    if _word(tokens, index) == "all" and _word(tokens, index + 1) == "day":
        return Found(2, True)
    return None


def _every_phrase(tokens: list[Token], index: int) -> Found | None:
    word = _word(tokens, index + 1)
    if word in REPEAT_UNITS:
        return Found(2, {"freq": REPEAT_UNITS[word]})
    if word in ("weekday", "weekdays"):
        return Found(2, {"freq": "weekly", "weekdays": list(WEEKDAY_CODES[:5])})
    if word in WEEKDAYS:
        return Found(2, {"freq": "weekly", "weekdays": [WEEKDAY_CODES[WEEKDAYS[word]]], "day": WEEKDAYS[word]})
    if word == "other" and _word(tokens, index + 2) in REPEAT_UNITS:
        return Found(3, {"freq": REPEAT_UNITS[_word(tokens, index + 2)], "interval": 2})
    unit = (_word(tokens, index + 2) or "").rstrip("s")
    if word and NUMBER.match(word) and 1 <= int(word) <= 99 and unit in REPEAT_UNITS:
        return Found(3, {"freq": REPEAT_UNITS[unit], "interval": int(word)})
    return None


def _repeat_phrase(tokens: list[Token], index: int) -> Found | None:
    word = _word(tokens, index)
    if word == "every":
        return _every_phrase(tokens, index)
    if word in SIMPLE_REPEATS:
        return Found(1, {"freq": SIMPLE_REPEATS[word]}, is_guess=True)
    if word == "weekdays":
        return Found(1, {"freq": "weekly", "weekdays": list(WEEKDAY_CODES[:5])}, is_guess=True)
    if word in PLURAL_WEEKDAYS:
        number = PLURAL_WEEKDAYS[word]
        return Found(1, {"freq": "weekly", "weekdays": [WEEKDAY_CODES[number]], "day": number}, is_guess=True)
    return None


def _calendar_tag(tokens: list[Token], index: int) -> Found | None:
    if tokens[index].word is None:
        return None
    match = CALENDAR_TAG.match(tokens[index].text.rstrip(TRAILING_PUNCTUATION))
    return Found(1, match.group(1)) if match else None


def _date_range_end(tokens: list[Token], index: int, today: date) -> Found | None:
    if _word(tokens, index) not in RANGE_WORDS:
        return None
    end = _date_phrase(tokens, index + 1, today)
    return Found(end.size + 1, end.value) if end else None


MATCHERS = (
    ("repeat", lambda tokens, index, today: _repeat_phrase(tokens, index)),
    ("allDay", lambda tokens, index, today: _all_day_phrase(tokens, index)),
    ("date", lambda tokens, index, today: _date_phrase(tokens, index, today)),
    ("endDate", lambda tokens, index, today: _date_range_end(tokens, index, today)),
    ("time", lambda tokens, index, today: _time_phrase(tokens, index)),
    ("duration", lambda tokens, index, today: _duration_phrase(tokens, index)),
    ("calendar", lambda tokens, index, today: _calendar_tag(tokens, index)),
)


def _match_at(tokens: list[Token], index: int, today: date, found: dict) -> tuple[str, Found] | None:
    for kind, matcher in MATCHERS:
        if kind in found or (kind == "endDate" and "date" not in found):
            continue
        match = matcher(tokens, index, today)
        if match is not None:
            return kind, match
    return None


def _is_end_or_understood(tokens: list[Token], index: int, today: date, found: dict) -> bool:
    if index >= len(tokens):
        return True
    following = _match_at(tokens, index, today, found)
    return following is not None and not following[1].is_guess


def _match_in_title(tokens: list[Token], index: int, today: date, found: dict) -> tuple[str, Found] | None:
    match = _match_at(tokens, index, today, found)
    if match is None or not match[1].is_guess:
        return match
    kind, result = match
    return match if _is_end_or_understood(tokens, index + result.size, today, {**found, kind: result.value}) else None


def _location_start(tokens: list[Token], index: int, today: date, found: dict) -> bool:
    if _word(tokens, index) not in ("at", "@") or index + 1 >= len(tokens):
        return False
    return _match_at(tokens, index + 1, today, found) is None and _time_at(tokens, index + 1, True) is None


def _first_day(found: dict, now: datetime) -> date:
    today = now.date()
    explicit = found.get("date")
    if explicit is not None:
        return explicit
    rule = found.get("repeat") or {}
    if "day" in rule:
        return _this_or_next(today, rule["day"])
    start_time = (found.get("time") or (None, None))[0]
    if start_time is not None and start_time <= now.strftime("%H:%M") and not rule:
        return today + timedelta(days=1)
    return today


def _finish(found: dict, title: str, location: str, now: datetime) -> Parsed:
    start_day = _first_day(found, now)
    end_day = found.get("endDate") or start_day
    if end_day < start_day:
        raise CalendarError("the end date is before the start date")
    start_time, end_time = found.get("time") or (None, None)
    if found.get("allDay"):
        start_time = end_time = None
    elif start_time is None and found.get("tonight"):
        start_time = TONIGHT_TIME
    elif found.get("tonight"):
        start_time, end_time = _evening(start_time), _evening(end_time)
    if start_time and found.get("duration") and not end_time:
        finish = datetime.combine(start_day, _clock_time(start_time)) + timedelta(minutes=found["duration"])
        end_day, end_time = finish.date(), finish.strftime("%H:%M")
    repeat = found.get("repeat")
    if repeat is not None:
        repeat = {key: value for key, value in repeat.items() if key != "day"}
    return Parsed(title=title, start_day=start_day, end_day=end_day, start_time=start_time, end_time=end_time,
                  repeat=repeat, location=location, calendar=found.get("calendar"))


def parse_quick_add(text, now: datetime) -> Parsed:
    if not isinstance(text, str):
        raise CalendarError("text must be text")
    if len(text) > MAX_TEXT_LENGTH:
        raise CalendarError(f"the quick add text is too long (at most {MAX_TEXT_LENGTH} characters)")
    tokens = _tokens(storable_text(text, "the quick add text"))
    today = now.date()
    found: dict = {}
    title_words: list[str] = []
    location_words: list[str] = []
    in_location = False
    index = 0
    while index < len(tokens):
        match = _match_in_title(tokens, index, today, found)
        if match is not None:
            kind, result = match
            found[kind] = result.value
            if tokens[index].word == "tonight" or (kind == "date" and _word(tokens, index + result.size - 1) == "tonight"):
                found["tonight"] = True
            in_location = False
            index += result.size
            continue
        if not location_words and _location_start(tokens, index, today, found):
            in_location = True
            index += 1
            continue
        (location_words if in_location else title_words).append(tokens[index].text)
        index += 1
    title = " ".join(title_words).strip()
    if not title:
        raise CalendarError("say what the event is as well as when, for example: dinner with mum friday 7pm")
    if not any(kind in found for kind in ("date", "time", "repeat", "allDay")):
        raise CalendarError("say when as well, for example: dinner with mum friday 7pm")
    return _finish(found, title, " ".join(location_words).strip(), now)
