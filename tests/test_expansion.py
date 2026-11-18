import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import icalendar
import pytest
import recurring_ical_events
from dateutil import rrule as dateutil_rrule

from calendarapp.davstore import CalendarInfo, StoredItem
from calendarapp.events import EventBook, find_master
from calendarapp.expansion import MONTHS_PER_YEAR, series_near
from conftest import LONDON

ZONE = "Europe/London"
WINDOW_SEED = 20261114
RANDOM_WINDOWS = 12
FIRST_RANDOM_DAY = date(2004, 6, 1)
LAST_RANDOM_DAY = date(2029, 12, 31)


def document(*events: str, calendar_lines: str = "") -> str:
    body = "".join(f"BEGIN:VEVENT\r\nDTSTAMP:20261101T000000Z\r\n{event.strip()}\r\nEND:VEVENT\r\n" for event in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//test//EN\r\n{calendar_lines}{body}END:VCALENDAR\r\n"


def lines(*parts: str) -> str:
    return "\r\n".join(parts)


CASES = {
    "daily timed in London": document(lines("UID:a", "SUMMARY:Walk", f"DTSTART;TZID={ZONE}:20050103T090000",
                                            f"DTEND;TZID={ZONE}:20050103T100000", "RRULE:FREQ=DAILY")),
    "every third day": document(lines("UID:b", f"DTSTART;TZID={ZONE}:20050103T183000",
                                      f"DTEND;TZID={ZONE}:20050103T190000", "RRULE:FREQ=DAILY;INTERVAL=3")),
    "fortnightly on three weekdays from Sunday weeks": document(lines(
        "UID:c", f"DTSTART;TZID={ZONE}:20050105T070000", f"DTEND;TZID={ZONE}:20050105T080000",
        "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR;WKST=SU")),
    "weekly from the start weekday": document(lines("UID:d", f"DTSTART;TZID={ZONE}:20050107T120000",
                                                    f"DTEND;TZID={ZONE}:20050107T130000", "RRULE:FREQ=WEEKLY")),
    "monthly on the 31st": document(lines("UID:e", f"DTSTART;TZID={ZONE}:20050131T100000",
                                          f"DTEND;TZID={ZONE}:20050131T110000", "RRULE:FREQ=MONTHLY;BYMONTHDAY=31")),
    "monthly from a 31st start day": document(lines("UID:f", f"DTSTART;TZID={ZONE}:20050131T100000",
                                                    f"DTEND;TZID={ZONE}:20050131T110000", "RRULE:FREQ=MONTHLY")),
    "every other month on the second Tuesday": document(lines(
        "UID:g", f"DTSTART;TZID={ZONE}:20050208T190000", f"DTEND;TZID={ZONE}:20050208T200000",
        "RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=2TU")),
    "monthly last working day": document(lines("UID:h", f"DTSTART;TZID={ZONE}:20050131T170000",
                                               f"DTEND;TZID={ZONE}:20050131T173000",
                                               "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1")),
    "yearly on 29 February": document(lines("UID:i", "DTSTART;VALUE=DATE:20040229", "DTEND;VALUE=DATE:20040301",
                                            "RRULE:FREQ=YEARLY")),
    "yearly last Sunday of March at 01:30": document(lines(
        "UID:j", f"DTSTART;TZID={ZONE}:20050327T013000", f"DTEND;TZID={ZONE}:20050327T023000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU")),
    "daily with a count": document(lines("UID:k", f"DTSTART;TZID={ZONE}:20260101T090000",
                                         f"DTEND;TZID={ZONE}:20260101T091500", "RRULE:FREQ=DAILY;COUNT=400")),
    "daily that ended in 2020": document(lines("UID:l", f"DTSTART;TZID={ZONE}:20050103T090000",
                                               f"DTEND;TZID={ZONE}:20050103T100000",
                                               "RRULE:FREQ=DAILY;UNTIL=20200101T000000Z")),
    "daily ending mid November 2026": document(lines("UID:m", f"DTSTART;TZID={ZONE}:20050103T090000",
                                                     f"DTEND;TZID={ZONE}:20050103T100000",
                                                     "RRULE:FREQ=DAILY;UNTIL=20261118T235959Z")),
    "all-day daily ending on a date": document(lines("UID:n", "DTSTART;VALUE=DATE:20050103", "DTEND;VALUE=DATE:20050104",
                                                     "RRULE:FREQ=DAILY;UNTIL=20261120")),
    "daily with removed dates": document(lines(
        "UID:o", f"DTSTART;TZID={ZONE}:20050103T090000", f"DTEND;TZID={ZONE}:20050103T100000", "RRULE:FREQ=DAILY",
        f"EXDATE;TZID={ZONE}:20261116T090000,20261117T090000", f"EXDATE;TZID={ZONE}:20060101T090000")),
    "all-day weekly with a removed date": document(lines("UID:p", "DTSTART;VALUE=DATE:20050104", "DTEND;VALUE=DATE:20050105",
                                                         "RRULE:FREQ=WEEKLY", "EXDATE;VALUE=DATE:20261117")),
    "daily with changed dates": document(
        lines("UID:q", "SUMMARY:Gym", f"DTSTART;TZID={ZONE}:20050103T070000", f"DTEND;TZID={ZONE}:20050103T080000",
              "RRULE:FREQ=DAILY", "SEQUENCE:1"),
        lines("UID:q", "SUMMARY:Gym (late)", f"RECURRENCE-ID;TZID={ZONE}:20261118T070000",
              f"DTSTART;TZID={ZONE}:20261118T200000", f"DTEND;TZID={ZONE}:20261118T210000", "SEQUENCE:1"),
        lines("UID:q", "SUMMARY:Gym (moved from 2005)", f"RECURRENCE-ID;TZID={ZONE}:20050110T070000",
              f"DTSTART;TZID={ZONE}:20261119T120000", f"DTEND;TZID={ZONE}:20261119T130000", "SEQUENCE:1"),
        lines("UID:q", "SUMMARY:Gym (moved to 2005)", f"RECURRENCE-ID;TZID={ZONE}:20261120T070000",
              f"DTSTART;TZID={ZONE}:20050111T120000", f"DTEND;TZID={ZONE}:20050111T130000", "SEQUENCE:1")),
    "all-day daily": document(lines("UID:r", "DTSTART;VALUE=DATE:20050103", "DTEND;VALUE=DATE:20050104", "RRULE:FREQ=DAILY")),
    "three-day all-day every week": document(lines("UID:s", "DTSTART;VALUE=DATE:20050107", "DTEND;VALUE=DATE:20050110",
                                                   "RRULE:FREQ=WEEKLY")),
    "daily at 01:30 across clock changes": document(lines("UID:t", f"DTSTART;TZID={ZONE}:20050103T013000",
                                                          f"DTEND;TZID={ZONE}:20050103T020000", "RRULE:FREQ=DAILY")),
    "daily in UTC": document(lines("UID:u", "DTSTART:20050103T230000Z", "DTEND:20050104T003000Z", "RRULE:FREQ=DAILY")),
    "daily floating time": document(lines("UID:v", "DTSTART:20050103T090000", "DTEND:20050103T093000", "RRULE:FREQ=DAILY")),
    "daily with a duration": document(lines("UID:w", f"DTSTART;TZID={ZONE}:20050103T220000", "DURATION:PT3H",
                                            "RRULE:FREQ=DAILY")),
    "ten-day event every week": document(lines("UID:x", f"DTSTART;TZID={ZONE}:20050103T090000",
                                               f"DTEND;TZID={ZONE}:20050113T090000", "RRULE:FREQ=WEEKLY")),
    "every five hours": document(lines("UID:y", f"DTSTART;TZID={ZONE}:20050103T000000",
                                       f"DTEND;TZID={ZONE}:20050103T001500", "RRULE:FREQ=HOURLY;INTERVAL=5")),
    "daily with extra dates": document(lines(
        "UID:z", f"DTSTART;TZID={ZONE}:20050103T090000", f"DTEND;TZID={ZONE}:20050103T100000",
        "RRULE:FREQ=WEEKLY;BYDAY=MO", f"RDATE;TZID={ZONE}:20261118T150000,20261126T150000")),
    "start that does not match its rule": document(lines("UID:aa", f"DTSTART;TZID={ZONE}:20050104T090000",
                                                         f"DTEND;TZID={ZONE}:20050104T100000", "RRULE:FREQ=WEEKLY;BYDAY=MO")),
    "daily only in June": document(lines("UID:ab", f"DTSTART;TZID={ZONE}:20050601T090000",
                                         f"DTEND;TZID={ZONE}:20050601T100000", "RRULE:FREQ=DAILY;BYMONTH=6")),
    "calendar with X-WR-TIMEZONE": document(lines("UID:ac", "DTSTART:20050103T090000Z", "DTEND:20050103T100000Z",
                                                  "RRULE:FREQ=DAILY"), calendar_lines=f"X-WR-TIMEZONE:{ZONE}\r\n"),
    "two rules": document(lines("UID:ad", f"DTSTART;TZID={ZONE}:20050103T090000", f"DTEND;TZID={ZONE}:20050103T100000",
                                "RRULE:FREQ=WEEKLY;BYDAY=MO", "RRULE:FREQ=WEEKLY;BYDAY=TH")),
    "this and future change": document(
        lines("UID:ae", f"DTSTART;TZID={ZONE}:20050103T090000", f"DTEND;TZID={ZONE}:20050103T100000", "RRULE:FREQ=DAILY"),
        lines("UID:ae", f"RECURRENCE-ID;RANGE=THISANDFUTURE;TZID={ZONE}:20260601T090000",
              f"DTSTART;TZID={ZONE}:20260601T110000", f"DTEND;TZID={ZONE}:20260601T120000")),
    "end in another zone": document(lines("UID:af", f"DTSTART;TZID={ZONE}:20050103T090000",
                                          "DTEND;TZID=Europe/Paris:20050103T110000", "RRULE:FREQ=DAILY")),
    "weekdays only with a changed date": document(
        lines("UID:ag", f"DTSTART;TZID={ZONE}:20050103T083000", f"DTEND;TZID={ZONE}:20050103T090000",
              "RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"),
        lines("UID:ag", f"RECURRENCE-ID;TZID={ZONE}:20261117T083000", f"DTSTART;TZID={ZONE}:20261121T100000",
              f"DTEND;TZID={ZONE}:20261121T110000")),
    "all-day daily with a timed removed date": document(lines("UID:ah", "DTSTART;VALUE=DATE:20050103",
                                                              "DTEND;VALUE=DATE:20050104", "RRULE:FREQ=DAILY",
                                                              f"EXDATE;TZID={ZONE}:20261117T000000")),
    "weekly on January Saturdays": document(lines("UID:aj", f"DTSTART;TZID={ZONE}:19950107T090000",
                                                  f"DTEND;TZID={ZONE}:19950107T100000", "RRULE:FREQ=WEEKLY;BYMONTH=1;BYDAY=SA")),
    "daily on the second of two hours": document(lines("UID:ak", f"DTSTART;TZID={ZONE}:20050103T090000", "DURATION:PT1H",
                                                       "RRULE:FREQ=DAILY;BYHOUR=9,17;BYSETPOS=2")),
    "hourly on the last of two half hours": document(lines("UID:al", f"DTSTART;TZID={ZONE}:20050103T090000", "DURATION:PT10M",
                                                           "RRULE:FREQ=HOURLY;INTERVAL=7;BYMINUTE=0,30;BYSETPOS=-1")),
    "daily with a count from 2005": document(lines("UID:am", f"DTSTART;TZID={ZONE}:20050103T090000",
                                                   f"DTEND;TZID={ZONE}:20050103T100000", "RRULE:FREQ=DAILY;COUNT=7990")),
}

LEFT_ALONE_ONLY = {
    "event lasting thousands of years": document(lines("UID:ai", "DTSTART:20050103T090000Z", "DTEND:99991231T000000Z",
                                                       "RRULE:FREQ=YEARLY")),
}

RANDOM_RULE_SEED = 1114
RANDOM_RULES = 40
RANDOM_RULE_WINDOWS = 6
RANDOM_PARTS = {
    "DAILY": ["", ";BYDAY=MO,WE,FR", ";BYMONTH=1,7", ";BYMONTHDAY=1,15,31"],
    "WEEKLY": ["", ";BYDAY=TU,SA", ";BYDAY=SU,MO;WKST=SU", ";BYDAY=MO,TH;WKST=TH"],
    "MONTHLY": ["", ";BYMONTHDAY=-1", ";BYDAY=1MO,-1FR", ";BYDAY=SA,SU;BYSETPOS=1", ";BYMONTHDAY=29,30,31"],
    "YEARLY": ["", ";BYMONTH=2;BYMONTHDAY=29", ";BYYEARDAY=1,100,-1", ";BYWEEKNO=10,52;BYDAY=MO", ";BYMONTH=10;BYDAY=-1SU"],
}


def random_rule_cases() -> list[tuple[str, str]]:
    picker = random.Random(RANDOM_RULE_SEED)
    cases = []
    for number in range(RANDOM_RULES):
        frequency = picker.choice(sorted(RANDOM_PARTS))
        rule = f"RRULE:FREQ={frequency};INTERVAL={picker.choice((1, 1, 2, 3, 5))}{picker.choice(RANDOM_PARTS[frequency])}"
        first = date(2008, 1, 1) + timedelta(days=picker.randrange(5000))
        if picker.random() < 0.3:
            times = (f"DTSTART;VALUE=DATE:{first:%Y%m%d}", f"DTEND;VALUE=DATE:{first + timedelta(days=picker.choice((1, 2))):%Y%m%d}")
        else:
            clock = picker.choice(("000000", "013000", "093000", "235900"))
            times = (f"DTSTART;TZID={ZONE}:{first:%Y%m%d}T{clock}", f"DURATION:PT{picker.choice((1, 5, 30))}H")
        if picker.random() < 0.3:
            rule += f";UNTIL={first.year + picker.randrange(1, 25)}0601T000000Z" if "TZID" in times[0] else f";UNTIL={first.year + 10}0601"
        cases.append((f"{number} {rule} {times[0]}", document(lines(f"UID:r{number}", *times, rule))))
    return cases

FIXED_WINDOWS = [
    (date(2026, 10, 26), date(2026, 12, 7)),
    (date(2026, 11, 16), date(2026, 11, 23)),
    (date(2026, 11, 18), date(2026, 11, 19)),
    (date(2026, 3, 23), date(2026, 4, 6)),
    (date(2026, 3, 29), date(2026, 3, 30)),
    (date(2026, 10, 25), date(2026, 10, 26)),
    (date(2028, 2, 21), date(2028, 3, 6)),
    (date(2019, 12, 25), date(2020, 1, 8)),
    (date(2005, 1, 1), date(2005, 2, 14)),
    (date(2004, 12, 20), date(2005, 1, 5)),
    (date(1999, 12, 13), date(1999, 12, 20)),
]


def random_windows() -> list[tuple[date, date]]:
    picker = random.Random(WINDOW_SEED)
    span = (LAST_RANDOM_DAY - FIRST_RANDOM_DAY).days
    windows = []
    for _ in range(RANDOM_WINDOWS):
        first = FIRST_RANDOM_DAY + timedelta(days=picker.randrange(span))
        windows.append((first, first + timedelta(days=picker.choice((1, 7, 42)))))
    return windows


VIEW_ZONES = [LONDON, timezone.utc, ZoneInfo("America/New_York"), ZoneInfo("Asia/Tokyo"), ZoneInfo("Asia/Kolkata")]
VIEW_ZONE_IDS = [str(zone) for zone in VIEW_ZONES]
MORE_VIEW_ZONES = VIEW_ZONES + [ZoneInfo("Australia/Lord_Howe"), ZoneInfo("Pacific/Kiritimati"),
                                ZoneInfo("America/Los_Angeles")]


def midnight(day: date, zone=LONDON) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def expanded(calendar, start: datetime, end: datetime) -> list[bytes]:
    return sorted(instance.to_ical() for instance in recurring_ical_events.of(calendar).between(start, end))


@pytest.mark.parametrize("zone", VIEW_ZONES, ids=VIEW_ZONE_IDS)
@pytest.mark.parametrize("name", sorted(CASES))
def test_moving_the_series_start_gives_the_same_instances(name, zone):
    calendar = icalendar.Calendar.from_ical(CASES[name])
    master = find_master(calendar)
    before = calendar.to_ical()
    for first, last in FIXED_WINDOWS + random_windows():
        start, end = midnight(first, zone), midnight(last, zone)
        old = expanded(calendar, start, end)
        new = expanded(series_near(calendar, master, start, end), start, end)
        assert new == old, f"{name}: {first} to {last}"
    assert calendar.to_ical() == before


@pytest.mark.parametrize("zone", VIEW_ZONES, ids=VIEW_ZONE_IDS)
@pytest.mark.parametrize("text", [pytest.param(text, id=name) for name, text in random_rule_cases()])
def test_generated_rules_give_the_same_instances(text, zone):
    calendar = icalendar.Calendar.from_ical(text)
    master = find_master(calendar)
    picker = random.Random(text)
    for _ in range(RANDOM_RULE_WINDOWS):
        first = date(2010, 1, 1) + timedelta(days=picker.randrange(7300))
        start, end = midnight(first, zone), midnight(first + timedelta(days=picker.choice((1, 7, 42))), zone)
        assert expanded(series_near(calendar, master, start, end), start, end) == expanded(calendar, start, end), f"{first}"


ZONED_RULE_SEED = 1115
ZONED_RULES = 30
ZONED_RULE_WINDOWS = 5
SERIES_ZONES = ["Europe/London", "America/New_York", "Australia/Lord_Howe", "Asia/Kolkata", "America/Sao_Paulo",
                "Asia/Tokyo", "Pacific/Apia"]
ZONED_RULE_PARTS = {
    "HOURLY": ["", ";BYHOUR=0,6,12,18", ";BYDAY=MO,TH;BYHOUR=1,2,3"],
    "DAILY": ["", ";BYHOUR=1,13", ";BYDAY=SA,SU;BYHOUR=0,23;BYMINUTE=30", ";BYMONTH=3,10"],
    "WEEKLY": ["", ";BYDAY=SU,WE;BYHOUR=2", ";BYDAY=MO,FR"],
    "MONTHLY": ["", ";BYDAY=-1SU", ";BYMONTHDAY=1,-1;BYHOUR=0"],
    "YEARLY": ["", ";BYMONTH=3,11;BYDAY=1SU", ";BYYEARDAY=60"],
}
CLOCK_TIMES = ("000000", "003000", "013000", "020000", "023000", "120000", "234500")


def zoned_start(picker: random.Random, first: date) -> tuple[str, str, str]:
    kind = picker.choice(("zone", "zone", "zone", "utc", "floating", "all-day"))
    clock = picker.choice(CLOCK_TIMES)
    if kind == "all-day":
        return f";VALUE=DATE:{first:%Y%m%d}", f"DTEND;VALUE=DATE:{first + timedelta(days=picker.choice((1, 3))):%Y%m%d}", ";VALUE=DATE"
    if kind == "utc":
        return f":{first:%Y%m%d}T{clock}Z", f"DURATION:PT{picker.choice((1, 5, 30))}H", ""
    if kind == "floating":
        return f":{first:%Y%m%d}T{clock}", f"DURATION:PT{picker.choice((1, 5, 30))}H", ""
    zone = picker.choice(SERIES_ZONES)
    return f";TZID={zone}:{first:%Y%m%d}T{clock}", f"DURATION:PT{picker.choice((1, 5, 30, 50))}H", f";TZID={zone}"


def override_of(uid: str, start: str, picker: random.Random, first: date) -> str:
    moved = first + timedelta(days=picker.randrange(-30, 3000))
    if start.startswith(";VALUE=DATE"):
        return lines(f"UID:{uid}", "SUMMARY:moved", f"RECURRENCE-ID{start}", f"DTSTART;VALUE=DATE:{moved:%Y%m%d}",
                     f"DTEND;VALUE=DATE:{moved + timedelta(days=1):%Y%m%d}")
    params, value = start.split(":", 1)
    return lines(f"UID:{uid}", "SUMMARY:moved", f"RECURRENCE-ID{start}", f"DTSTART{params}:{moved:%Y%m%d}{value[8:]}",
                 "DURATION:PT2H")


def zoned_rule_cases() -> list[tuple[str, str]]:
    picker = random.Random(ZONED_RULE_SEED)
    cases = []
    for number in range(ZONED_RULES):
        first = date(2008, 1, 1) + timedelta(days=picker.randrange(5000))
        start, end, _ = zoned_start(picker, first)
        frequencies = sorted(ZONED_RULE_PARTS) if "VALUE=DATE" not in start else ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
        frequency = picker.choice(frequencies)
        parts = picker.choice(ZONED_RULE_PARTS[frequency])
        if "VALUE=DATE" in start:
            parts = parts.split(";BYHOUR")[0]
        rule = f"RRULE:FREQ={frequency};INTERVAL={picker.choice((1, 1, 2, 3, 7))}{parts}"
        uid = f"z{number}"
        events = [lines(f"UID:{uid}", "SUMMARY:series", f"DTSTART{start}", end, rule)]
        if picker.random() < 0.5:
            events.append(override_of(uid, start, picker, first))
        cases.append((f"{number} {rule} DTSTART{start}", document(*events)))
    return cases


@pytest.mark.parametrize("text", [pytest.param(text, id=name) for name, text in zoned_rule_cases()])
def test_generated_rules_in_other_zones_and_with_changed_dates_give_the_same_instances(text):
    calendar = icalendar.Calendar.from_ical(text)
    master = find_master(calendar)
    picker = random.Random(text)
    for _ in range(ZONED_RULE_WINDOWS):
        zone = picker.choice(MORE_VIEW_ZONES)
        first = date(2010, 1, 1) + timedelta(days=picker.randrange(7300))
        start, end = midnight(first, zone), midnight(first + timedelta(days=picker.choice((1, 7, 42))), zone)
        assert expanded(series_near(calendar, master, start, end), start, end) == expanded(calendar, start, end), \
            f"{first} in {zone}"


def test_a_long_running_series_is_moved_near_the_window():
    calendar = icalendar.Calendar.from_ical(CASES["daily timed in London"])
    master = find_master(calendar)
    moved = find_master(series_near(calendar, master, midnight(date(2026, 11, 16)), midnight(date(2026, 11, 23))))
    assert date(2026, 11, 1) <= moved["DTSTART"].dt.date() <= date(2026, 11, 16)
    assert moved["DTEND"].dt - moved["DTSTART"].dt == timedelta(hours=1)
    assert master["DTSTART"].dt == datetime(2005, 1, 3, 9, 0, tzinfo=LONDON)


@pytest.mark.parametrize("name", ["calendar with X-WR-TIMEZONE", "two rules", "this and future change",
                                  "end in another zone", "daily with a count", "event lasting thousands of years"])
def test_series_it_cannot_prove_equal_are_left_alone(name):
    calendar = icalendar.Calendar.from_ical({**CASES, **LEFT_ALONE_ONLY}[name])
    start, end = midnight(date(2026, 11, 16)), midnight(date(2026, 11, 23))
    assert series_near(calendar, find_master(calendar), start, end) is calendar


def daily_since(year: int, uid: str) -> str:
    return document(lines(f"UID:{uid}", "SUMMARY:Water the plants", f"DTSTART:{year}0101T090000Z", f"DTEND:{year}0101T091500Z",
                          "RRULE:FREQ=DAILY"))


@pytest.fixture
def counted_rule_steps(monkeypatch):
    steps = []
    original = dateutil_rrule.rrule._iter

    def counting(self):
        for value in original(self):
            steps.append(value)
            yield value

    monkeypatch.setattr(dateutil_rrule.rrule, "_iter", counting)
    return steps


def test_view_work_does_not_grow_with_how_long_ago_a_series_started(book, home, dav, counted_rule_steps):
    dav.put(home, "recent.ics", daily_since(2025, "recent"), None)
    book.occurrences(date(2026, 10, 26), date(2026, 12, 7))
    recent_steps = len(counted_rule_steps)
    counted_rule_steps.clear()
    dav.delete(home, "recent.ics", None)
    dav.put(home, "old.ics", daily_since(1990, "old"), None)
    found, _ = book.occurrences(date(2026, 10, 26), date(2026, 12, 7))
    assert len([occurrence for occurrence in found if occurrence.uid == "old"]) == 42
    assert len(counted_rule_steps) <= recent_steps + 60


def test_opening_one_date_of_an_old_series_does_not_walk_from_its_start(book, home, dav, counted_rule_steps):
    dav.put(home, "old.ics", daily_since(1990, "old"), None)
    shown = book.get(home, "old.ics", "2026-11-18T09:00:00Z")
    assert shown["start"] == "2026-11-18T09:00"
    assert len(counted_rule_steps) < 100


NEVER_MATCHING_RULES = [
    "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
    "FREQ=DAILY;BYDAY=FR,SA,TU;BYSETPOS=3",
    "FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30",
    "FREQ=MONTHLY;BYMONTH=4,6,9,11;BYMONTHDAY=31",
    "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30;UNTIL=99990101T000000Z",
    "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30;COUNT=5",
    "FREQ=MONTHLY;BYMONTH=4,6,9,11;BYMONTHDAY=31;COUNT=1",
]
SLOWEST_NEVER_MATCHING_RULE = "FREQ=HOURLY;INTERVAL=5;BYDAY=SU,TH;BYSETPOS=-2"
MONTHS_IN_A_CALENDAR_CYCLE = 400 * 12
LATE_START = 9500
LATE_WINDOWS = [(date(9550, 3, 1), date(9550, 4, 12)), (date(9550, 3, 9), date(9550, 3, 10)),
                (date(9499, 12, 1), date(9500, 1, 12)), (date(9499, 6, 1), date(9499, 7, 13))]


def never_matching(rule: str, year: int, extra_year: int, start: str = ";TZID=Europe/London:{year}0105T090000",
                   end: str = ";TZID=Europe/London:{year}0105T100000") -> str:
    return document(
        lines("UID:never", "SUMMARY:Never", f"DTSTART{start.format(year=year)}", f"DTEND{end.format(year=year)}",
              f"RRULE:{rule}", f"RDATE;TZID={ZONE}:{extra_year}0305T150000,{extra_year}0310T150000",
              f"EXDATE;TZID={ZONE}:{extra_year}0310T150000"),
        lines("UID:never", "SUMMARY:Never (moved)", f"RECURRENCE-ID{start.format(year=year)}",
              f"DTSTART;TZID={ZONE}:{extra_year}0307T120000", f"DTEND;TZID={ZONE}:{extra_year}0307T130000"))


class OneItemStore:
    def __init__(self, text: str):
        self.info = CalendarInfo("home", "Home", "#3366cc", "1", 0)
        self.item = StoredItem("never.ics", "1", text)

    def calendars(self):
        return [self.info]

    def items(self, info):
        return [self.item]


@pytest.fixture
def counted_month_rebuilds(monkeypatch):
    rebuilds = []
    original = dateutil_rrule._iterinfo.rebuild

    def counting(self, year, month):
        rebuilds.append((year, month))
        return original(self, year, month)

    monkeypatch.setattr(dateutil_rrule._iterinfo, "rebuild", counting)
    return rebuilds


@pytest.mark.parametrize("rule", NEVER_MATCHING_RULES + [SLOWEST_NEVER_MATCHING_RULE])
def test_a_view_of_a_rule_that_never_gives_a_date_does_not_walk_to_the_year_9999(rule, counted_month_rebuilds):
    book = EventBook(OneItemStore(never_matching(rule, 1990, 2026)), LONDON)
    found, _ = book.occurrences(date(2026, 3, 1), date(2026, 4, 12))
    assert sorted(occurrence.instance["SUMMARY"] for occurrence in found) == ["Never", "Never (moved)"]
    assert len(counted_month_rebuilds) <= MONTHS_IN_A_CALENDAR_CYCLE + 24


NEVER_MATCHING_TIMES = [
    (";TZID=Europe/London:{year}0105T090000", ";TZID=Europe/London:{year}0105T100000"),
    (";VALUE=DATE:{year}0105", ";VALUE=DATE:{year}0106"),
    (":{year}0105T090000", ":{year}0105T100000"),
    (":{year}0105T090000Z", ":{year}0105T100000Z"),
]


def is_timed_or_not_hourly(rule: str, times: tuple[str, str]) -> bool:
    return not (rule.startswith("FREQ=HOURLY") and times[0].startswith(";VALUE=DATE"))


@pytest.mark.parametrize("rule, times", [(rule, times) for rule in NEVER_MATCHING_RULES for times in NEVER_MATCHING_TIMES
                                         if is_timed_or_not_hourly(rule, times)])
def test_a_rule_that_never_gives_a_date_keeps_its_extra_and_changed_dates(rule, times):
    calendar = icalendar.Calendar.from_ical(never_matching(rule, LATE_START, LATE_WINDOWS[0][0].year, *times))
    master = find_master(calendar)
    for first, last in LATE_WINDOWS:
        start, end = midnight(first), midnight(last)
        old = expanded(calendar, start, end)
        assert expanded(series_near(calendar, master, start, end), start, end) == old, f"{first} to {last}"
    assert len(expanded(calendar, midnight(LATE_WINDOWS[0][0]), midnight(LATE_WINDOWS[0][1]))) == 2


class WalkedTooFar(BaseException):
    pass


@pytest.fixture
def capped_month_rebuilds(monkeypatch):
    rebuilds = []
    original = dateutil_rrule._iterinfo.rebuild

    def counting(self, year, month):
        rebuilds.append((year, month))
        if len(rebuilds) > MONTHS_IN_A_CALENDAR_CYCLE + 24:
            raise WalkedTooFar(f"the rule was walked through more than {len(rebuilds) - 1} months, last {year}-{month}")
        return original(self, year, month)

    monkeypatch.setattr(dateutil_rrule._iterinfo, "rebuild", counting)
    return rebuilds


START_YEAR_IN_VIEW = 2026
VIEWS_NEAR_THE_START = [((date(2025, 12, 29), date(2026, 3, 9)), ["Never", "Never (moved)"]),
                        ((date(2025, 10, 1), date(2025, 11, 12)), [])]


@pytest.mark.parametrize("window, summaries", VIEWS_NEAR_THE_START, ids=["holds the start", "before the start"])
@pytest.mark.parametrize("rule", NEVER_MATCHING_RULES + [SLOWEST_NEVER_MATCHING_RULE])
def test_a_view_near_or_before_the_start_of_a_rule_that_never_gives_a_date_does_not_walk_to_the_year_9999(
        rule, window, summaries, capped_month_rebuilds):
    book = EventBook(OneItemStore(never_matching(rule, START_YEAR_IN_VIEW, START_YEAR_IN_VIEW)), LONDON)
    found, _ = book.occurrences(*window)
    assert sorted(occurrence.instance["SUMMARY"] for occurrence in found) == summaries
    assert len(capped_month_rebuilds) <= MONTHS_IN_A_CALENDAR_CYCLE + 24


SET_POSITIONS_NO_PERIOD_HOLDS = [
    "FREQ=HOURLY;BYSETPOS=2",
    SLOWEST_NEVER_MATCHING_RULE,
    "FREQ=HOURLY;BYMINUTE=0,30;BYSETPOS=3,-3",
    "FREQ=DAILY;BYHOUR=9,17;BYSETPOS=-3",
    "FREQ=DAILY;BYDAY=FR,SA,TU;BYSETPOS=3",
]
MONTHS_A_VIEW_MAY_REBUILD = 24


@pytest.mark.parametrize("rule", SET_POSITIONS_NO_PERIOD_HOLDS)
def test_a_set_position_no_hour_or_day_holds_is_not_walked_at_all(rule, capped_month_rebuilds):
    calendar = icalendar.Calendar.from_ical(never_matching(rule, 1990, 2026))
    start, end = midnight(date(2026, 3, 1)), midnight(date(2026, 4, 12))
    near = series_near(calendar, find_master(calendar), start, end)
    assert not capped_month_rebuilds
    found = recurring_ical_events.of(near).between(start, end)
    assert sorted(instance["SUMMARY"] for instance in found) == ["Never", "Never (moved)"]
    assert len(capped_month_rebuilds) <= MONTHS_A_VIEW_MAY_REBUILD


def test_a_counted_rule_with_no_date_loses_its_count_and_a_counted_rule_with_dates_is_left_alone():
    never = icalendar.Calendar.from_ical(never_matching("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30;COUNT=5;UNTIL=20300101T000000Z",
                                                        START_YEAR_IN_VIEW, START_YEAR_IN_VIEW))
    start, end = midnight(date(2026, 3, 1)), midnight(date(2026, 4, 12))
    rule = find_master(series_near(never, find_master(never), start, end))["RRULE"]
    assert "COUNT" not in rule and "BYMONTHDAY" not in rule
    assert rule["UNTIL"] == find_master(never)["RRULE"]["UNTIL"]
    counted = icalendar.Calendar.from_ical(CASES["daily with a count from 2005"])
    assert series_near(counted, find_master(counted), start, end) is counted


SEED_1091_SERIES = document(
    lines("UID:seed", "SUMMARY:series", "DTSTART;TZID=America/Sao_Paulo:19711026T183000", "DURATION:PT30M",
          "RRULE:FREQ=WEEKLY;INTERVAL=10;BYMONTHDAY=1,13;BYDAY=SA",
          "RDATE;TZID=America/Sao_Paulo:19990820T233000,19990818T001500"),
    lines("UID:seed", "SUMMARY:moved", "RECURRENCE-ID;TZID=America/Sao_Paulo:19990820T183000",
          "DTSTART;TZID=America/Sao_Paulo:19750127T013000", "DURATION:PT2H"))
SEED_1091_VIEW_ZONE = ZoneInfo("Australia/Lord_Howe")


@pytest.mark.parametrize("first, last", [(date(1999, 8, 3), date(1999, 8, 10)), (date(1999, 8, 14), date(1999, 8, 22)),
                                         (date(1999, 12, 20), date(2000, 1, 3))])
def test_a_view_in_1999_whose_next_date_falls_in_2000_gives_the_same_instances(first, last):
    calendar = icalendar.Calendar.from_ical(SEED_1091_SERIES)
    start, end = midnight(first, SEED_1091_VIEW_ZONE), midnight(last, SEED_1091_VIEW_ZONE)
    assert expanded(series_near(calendar, find_master(calendar), start, end), start, end) == expanded(calendar, start, end)


NEVER = "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30"
TIMED_IN_VIEW = (f"DTSTART;TZID={ZONE}:99901110T090000", f"DTEND;TZID={ZONE}:99901110T100000")
ALL_DAY_IN_VIEW = ("DTSTART;VALUE=DATE:99901110", "DTEND;VALUE=DATE:99901111")
FLOATING_IN_VIEW = ("DTSTART:99901110T090000", "DTEND:99901110T100000")
VIEW_HOLDING_THE_START = (date(9990, 11, 1), date(9990, 12, 1))
NEVER_SERIES_HOLDING_THE_VIEW = {
    "timed": (*TIMED_IN_VIEW, NEVER),
    "with an extra date": (*TIMED_IN_VIEW, NEVER, f"RDATE;TZID={ZONE}:99901115T090000"),
    "start removed": (*TIMED_IN_VIEW, NEVER, f"EXDATE;TZID={ZONE}:99901110T090000"),
    "until before the start": (*TIMED_IN_VIEW, f"{NEVER};UNTIL=99901101T000000Z"),
    "until after the start": (*TIMED_IN_VIEW, f"{NEVER};UNTIL=99911101T000000Z"),
    "until at the start": ("DTSTART:99901110T090000Z", "DTEND:99901110T100000Z", f"{NEVER};UNTIL=99901110T090000Z"),
    "counted": (*TIMED_IN_VIEW, f"{NEVER};COUNT=5"),
    "all-day until a date before the start": (*ALL_DAY_IN_VIEW, f"{NEVER};UNTIL=99901101"),
    "all-day": (*ALL_DAY_IN_VIEW, "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30"),
    "floating": (*FLOATING_IN_VIEW, NEVER),
    "floating until before the start": (*FLOATING_IN_VIEW, f"{NEVER};UNTIL=99901101T000000"),
    "hourly set position no hour holds": (*TIMED_IN_VIEW, "RRULE:FREQ=HOURLY;BYSETPOS=2"),
}


@pytest.mark.parametrize("zone", VIEW_ZONES, ids=VIEW_ZONE_IDS)
@pytest.mark.parametrize("name", sorted(NEVER_SERIES_HOLDING_THE_VIEW))
def test_a_rule_that_never_gives_a_date_shows_its_start_as_the_library_does(name, zone):
    calendar = icalendar.Calendar.from_ical(document(lines("UID:start", "SUMMARY:start", *NEVER_SERIES_HOLDING_THE_VIEW[name])))
    start, end = midnight(VIEW_HOLDING_THE_START[0], zone), midnight(VIEW_HOLDING_THE_START[1], zone)
    near = series_near(calendar, find_master(calendar), start, end)
    assert near is not calendar
    assert expanded(near, start, end) == expanded(calendar, start, end)


X_WR_NEW_YORK = "X-WR-TIMEZONE:America/New_York\r\n"
UTC_IN_VIEW = ("DTSTART:99901110T230000Z", "DTEND:99901111T000000Z")
MOVED_INTO_THE_VIEW = lines("UID:start", "SUMMARY:moved", "RECURRENCE-ID:99901110T230000Z", "DTSTART:99901115T120000Z",
                            "DTEND:99901115T130000Z")
NEVER_SERIES_THE_OLD_GUARDS_REFUSED = {
    "X-WR-TIMEZONE, start in UTC": ((*UTC_IN_VIEW, NEVER), (), X_WR_NEW_YORK),
    "X-WR-TIMEZONE, floating start": ((*FLOATING_IN_VIEW, NEVER), (), X_WR_NEW_YORK),
    "X-WR-TIMEZONE, floating until before the start": ((*FLOATING_IN_VIEW, f"{NEVER};UNTIL=99901101T000000"), (),
                                                       X_WR_NEW_YORK),
    "X-WR-TIMEZONE, extra date in UTC": ((*UTC_IN_VIEW, NEVER, "RDATE:99901120T120000Z"), (), X_WR_NEW_YORK),
    "X-WR-TIMEZONE, start moved": ((*UTC_IN_VIEW, NEVER), (MOVED_INTO_THE_VIEW,), X_WR_NEW_YORK),
    "X-WR-TIMEZONE, counted": ((*UTC_IN_VIEW, f"{NEVER};COUNT=3"), (), X_WR_NEW_YORK),
    "DTEND as a date on a timed start": ((f"DTSTART;TZID={ZONE}:99901110T090000", "DTEND;VALUE=DATE:99901111", NEVER), (), ""),
    "DTEND in another zone": ((f"DTSTART;TZID={ZONE}:99901110T090000", "DTEND;TZID=Europe/Paris:99901110T110000", NEVER),
                              (), ""),
    "floating start, DTEND in UTC": (("DTSTART:99901110T090000", "DTEND:99901110T100000Z", NEVER), (), ""),
}
VIEWS_ROUND_THE_START = [VIEW_HOLDING_THE_START, (date(9990, 6, 1), date(9990, 7, 13)), (date(9992, 3, 1), date(9992, 4, 12))]


@pytest.mark.parametrize("zone", VIEW_ZONES, ids=VIEW_ZONE_IDS)
@pytest.mark.parametrize("name", sorted(NEVER_SERIES_THE_OLD_GUARDS_REFUSED))
def test_a_rule_that_never_gives_a_date_in_a_series_that_cannot_be_moved_shows_what_the_library_shows(name, zone):
    master_lines, changes, calendar_lines = NEVER_SERIES_THE_OLD_GUARDS_REFUSED[name]
    calendar = icalendar.Calendar.from_ical(document(lines("UID:start", "SUMMARY:start", *master_lines), *changes,
                                                     calendar_lines=calendar_lines))
    before = calendar.to_ical()
    for first, last in VIEWS_ROUND_THE_START:
        start, end = midnight(first, zone), midnight(last, zone)
        near = series_near(calendar, find_master(calendar), start, end)
        assert near is not calendar, f"{first} to {last}"
        assert expanded(near, start, end) == expanded(calendar, start, end), f"{first} to {last}"
    assert calendar.to_ical() == before


@pytest.mark.parametrize("master_lines, calendar_lines", [
    (("DTSTART:19900105T090000Z", "DTEND:19900105T100000Z", NEVER, "RDATE:20260305T150000Z"), X_WR_NEW_YORK),
    (("DTSTART:19900105T090000", "DTEND:19900105T100000", NEVER, "RDATE:20260305T150000"), X_WR_NEW_YORK),
    ((f"DTSTART;TZID={ZONE}:19900105T090000", "DTEND;TZID=Europe/Paris:19900105T110000", NEVER,
      f"RDATE;TZID={ZONE}:20260305T150000"), ""),
], ids=["X-WR-TIMEZONE in UTC", "X-WR-TIMEZONE floating", "DTEND in another zone"])
def test_a_view_of_such_a_series_does_not_walk_to_the_year_9999(master_lines, calendar_lines, capped_month_rebuilds):
    calendar = icalendar.Calendar.from_ical(document(lines("UID:never", "SUMMARY:Never", *master_lines),
                                                     calendar_lines=calendar_lines))
    start, end = midnight(date(2026, 3, 1)), midnight(date(2026, 4, 12))
    found = recurring_ical_events.of(series_near(calendar, find_master(calendar), start, end)).between(start, end)
    assert [instance["SUMMARY"] for instance in found] == ["Never"]
    assert len(capped_month_rebuilds) <= MONTHS_IN_A_CALENDAR_CYCLE + 24


@pytest.mark.parametrize("master_lines", [
    (f"DTSTART;TZID={ZONE}:19900105T090000", "DTEND;VALUE=DATE:19900106", NEVER, f"RDATE;TZID={ZONE}:20260305T150000"),
    ("DTSTART:19900105T090000", "DTEND:19900105T100000Z", NEVER, "RDATE:20260305T150000"),
], ids=["DTEND as a date", "floating start, DTEND in UTC"])
def test_a_series_whose_length_is_unknown_is_searched_from_its_start_and_no_further_than_a_cycle_past_the_view(
        master_lines, counted_month_rebuilds):
    calendar = icalendar.Calendar.from_ical(document(lines("UID:never", "SUMMARY:Never", *master_lines)))
    start, end = midnight(date(2026, 3, 1)), midnight(date(2026, 4, 12))
    found = recurring_ical_events.of(series_near(calendar, find_master(calendar), start, end)).between(start, end)
    assert [instance["SUMMARY"] for instance in found] == ["Never"]
    months_from_the_start_to_the_view = (2026 - 1990 + 1) * MONTHS_PER_YEAR
    assert len(counted_month_rebuilds) <= MONTHS_IN_A_CALENDAR_CYCLE + months_from_the_start_to_the_view + 24


def test_a_series_with_a_change_for_this_and_future_dates_is_still_left_to_the_library():
    calendar = icalendar.Calendar.from_ical(document(
        lines("UID:f", "SUMMARY:f", *UTC_IN_VIEW, NEVER),
        lines("UID:f", "SUMMARY:later", "RECURRENCE-ID;RANGE=THISANDFUTURE:99901110T230000Z", "DTSTART:99901112T230000Z",
              "DTEND:99901113T000000Z")))
    start, end = midnight(VIEW_HOLDING_THE_START[0]), midnight(VIEW_HOLDING_THE_START[1])
    assert series_near(calendar, find_master(calendar), start, end) is calendar
