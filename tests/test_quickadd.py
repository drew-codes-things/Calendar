from datetime import datetime

import pytest

from calendarapp.errors import CalendarError
from calendarapp.quickadd import parse_quick_add
from conftest import LONDON

FRIDAY_MORNING = datetime(2026, 11, 13, 10, 0, tzinfo=LONDON)


def parsed(text):
    result = parse_quick_add(text, FRIDAY_MORNING)
    return {**result.fields(), "calendar": result.calendar}


def timed(title, start, end, **extra):
    return {"title": title, "allDay": False, "start": start, "end": end, "location": extra.get("location", ""),
            "repeat": extra.get("repeat"), "calendar": extra.get("calendar")}


def all_day(title, start, end, **extra):
    return {"title": title, "allDay": True, "start": start, "end": end, "location": extra.get("location", ""),
            "repeat": extra.get("repeat"), "calendar": extra.get("calendar")}


@pytest.mark.parametrize("text, expected", [
    ("dinner with mum friday 7pm", timed("dinner with mum", "2026-11-13T19:00", "2026-11-13T20:00")),
    ("dinner with mum next friday 7pm", timed("dinner with mum", "2026-11-20T19:00", "2026-11-20T20:00")),
    ("dentist tomorrow 9:30am", timed("dentist", "2026-11-14T09:30", "2026-11-14T10:30")),
    ("lunch with sam at the pub tomorrow 1pm for 2 hours",
     timed("lunch with sam", "2026-11-14T13:00", "2026-11-14T15:00", location="the pub")),
    ("meeting 3-4pm monday", timed("meeting", "2026-11-16T15:00", "2026-11-16T16:00")),
    ("meeting monday from 10am to 11:30am", timed("meeting", "2026-11-16T10:00", "2026-11-16T11:30")),
    ("call 19:00", timed("call", "2026-11-13T19:00", "2026-11-13T20:00")),
    ("breakfast 8am", timed("breakfast", "2026-11-14T08:00", "2026-11-14T09:00")),
    ("party sat 8pm", timed("party", "2026-11-14T20:00", "2026-11-14T21:00")),
    ("gig on 4 dec at 11pm until 1am", timed("gig", "2026-12-04T23:00", "2026-12-05T01:00")),
    ("drinks tonight 7-11", timed("drinks", "2026-11-13T19:00", "2026-11-13T23:00")),
    ("dinner tonight", timed("dinner", "2026-11-13T19:00", "2026-11-13T20:00")),
    ("dinner at nando's tonight at 8", timed("dinner", "2026-11-13T20:00", "2026-11-13T21:00", location="nando's")),
    ("holiday 24 dec to 27 dec", all_day("holiday", "2026-12-24", "2026-12-27")),
    ("mum's birthday 2 dec every year", all_day("mum's birthday", "2026-12-02", "2026-12-02", repeat={"freq": "yearly"})),
    ("gym every monday 7am", timed("gym", "2026-11-16T07:00", "2026-11-16T08:00", repeat={"freq": "weekly", "weekdays": ["MO"]})),
    ("standup weekdays 9:15", timed("standup", "2026-11-13T09:15", "2026-11-13T10:15",
                                    repeat={"freq": "weekly", "weekdays": ["MO", "TU", "WE", "TH", "FR"]})),
    ("bins every 2 weeks", all_day("bins", "2026-11-13", "2026-11-13", repeat={"freq": "weekly", "interval": 2})),
    ("conference 2027-03-01 all day #work", all_day("conference", "2027-03-01", "2027-03-01", calendar="work")),
    ("payday 28/11", all_day("payday", "2026-11-28", "2026-11-28")),
    ('"Friday 7pm drinks" next friday 6pm', timed("Friday 7pm drinks", "2026-11-20T18:00", "2026-11-20T19:00")),
    ("sun cream shopping tomorrow", all_day("sun cream shopping", "2026-11-14", "2026-11-14")),
    ("party in 3 days", all_day("party", "2026-11-16", "2026-11-16")),
    ("trip in 2 weeks 9am", timed("trip", "2026-11-27T09:00", "2026-11-27T10:00")),
    ("weekly review friday 3pm", timed("weekly review", "2026-11-13T15:00", "2026-11-13T16:00")),
    ("gym on sun", all_day("gym", "2026-11-15", "2026-11-15")),
])
def test_quick_add_understands(text, expected):
    assert parsed(text) == expected


@pytest.mark.parametrize("text, message", [
    ("buy milk", "say when"),
    ("friday 7pm", "say what the event is"),
    ("", "say what the event is"),
    ("x" * 501, "too long"),
    (None, "text must be text"),
    ("trip 27 dec to 24 dec 2026", "before the start"),
])
def test_quick_add_refuses(text, message):
    with pytest.raises(CalendarError, match=message):
        parse_quick_add(text, FRIDAY_MORNING)
