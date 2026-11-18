from datetime import date

import pytest

from calendarapp.errors import CalendarError
from calendarapp.events import occurrence_json
from calendarapp.transfer import MAX_IMPORT_EVENTS, export_calendars, import_calendar
from conftest import LONDON

GOOGLE_EXPORT = (
    "BEGIN:VCALENDAR\r\nPRODID:-//Google Inc//Google Calendar 70.9054//EN\r\nVERSION:2.0\r\nX-WR-CALNAME:Home\r\n"
    "X-WR-TIMEZONE:Europe/London\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20261120T190000Z\r\nDTEND:20261120T200000Z\r\nUID:g1@example.com\r\nSUMMARY:Dinner\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261224\r\nDTEND;VALUE=DATE:20261227\r\nUID:g2@example.com\r\nSUMMARY:Holiday\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=Europe/London:20261116T070000\r\nDTEND;TZID=Europe/London:20261116T080000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=2\r\nUID:g3@example.com\r\nSUMMARY:Gym\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=Europe/London:20261123T180000\r\nDTEND;TZID=Europe/London:20261123T190000\r\n"
    "RECURRENCE-ID;TZID=Europe/London:20261123T070000\r\nUID:g3@example.com\r\nSUMMARY:Gym (late)\r\nEND:VEVENT\r\n"
    "BEGIN:VTODO\r\nUID:t1\r\nSUMMARY:Not an event\r\nEND:VTODO\r\n"
    "END:VCALENDAR\r\n")


def shown(book):
    found, _ = book.occurrences(date(2026, 11, 1), date(2027, 1, 1))
    return [(item["title"], item["start"]) for item in (occurrence_json(occurrence, LONDON) for occurrence in found)]


def test_import_a_google_export(book, home):
    result = import_calendar(book, home, GOOGLE_EXPORT.encode())
    assert result == {"imported": 3, "alreadyThere": 0, "skipped": 1, "refused": []}
    assert shown(book) == [("Gym", "2026-11-16T07:00"), ("Dinner", "2026-11-20T19:00"), ("Gym (late)", "2026-11-23T18:00"),
                           ("Holiday", "2026-12-24")]


def test_importing_twice_adds_nothing(book, home):
    import_calendar(book, home, GOOGLE_EXPORT.encode())
    assert import_calendar(book, home, GOOGLE_EXPORT.encode())["alreadyThere"] == 3
    assert len(shown(book)) == 4


def test_export_then_import_round_trips(book, home):
    book.create({"calendarId": home, "title": "Round trip", "start": "2026-11-20T19:00", "end": "2026-11-20T20:00",
                 "repeat": {"freq": "daily", "count": 2}, "reminders": [10]})
    text, file_name = export_calendars(book, home)
    assert file_name == "calendar-personal.ics"
    other = book.create_calendar("Copy", "#123456")["id"]
    assert import_calendar(book, other, text.encode())["imported"] == 1
    found, _ = book.occurrences(date(2026, 11, 20), date(2026, 11, 23), [other])
    assert [occurrence_json(item, LONDON)["reminders"] for item in found] == [[10], [10]]


@pytest.mark.parametrize("data, message", [
    (b"", "choose an .ics file"),
    (b"\xff\xfe not text", "not UTF-8"),
    (b"hello", "could not be read"),
    (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", "no events"),
    (b"BEGIN:VCARD\r\nVERSION:3.0\r\nEND:VCARD\r\n", "VCALENDAR"),
])
def test_bad_imports_are_refused(book, home, data, message):
    with pytest.raises(CalendarError, match=message):
        import_calendar(book, home, data)


def test_too_many_events_are_refused(book, home):
    events = "".join(f"BEGIN:VEVENT\r\nUID:e{number}\r\nDTSTART;VALUE=DATE:20261201\r\nSUMMARY:x\r\nEND:VEVENT\r\n"
                     for number in range(MAX_IMPORT_EVENTS + 1))
    with pytest.raises(CalendarError, match="at most"):
        import_calendar(book, home, f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{events}END:VCALENDAR\r\n".encode())


def test_import_into_an_unknown_calendar(book, home):
    with pytest.raises(CalendarError):
        import_calendar(book, "nope", GOOGLE_EXPORT.encode())


def test_export_of_all_calendars(book, home):
    work = book.create_calendar("Work", "#5b8def")["id"]
    book.create({"calendarId": home, "title": "A", "start": "2026-11-20T19:00", "end": "2026-11-20T20:00"})
    book.create({"calendarId": work, "title": "B", "allDay": True, "start": "2026-11-21", "end": "2026-11-21"})
    text, file_name = export_calendars(book, None)
    assert file_name == "calendar.ics"
    assert "SUMMARY:A" in text and "SUMMARY:B" in text
    assert text.count("BEGIN:VTIMEZONE") == 1


def one_event(uid, rule=""):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261113T100000Z\r\nDTSTART:20261120T120000Z\r\nDTEND:20261120T130000Z\r\n"
            f"{rule}SUMMARY:{uid}\r\nEND:VEVENT\r\n")


def wrapped(*events, header=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//EN\r\n" + header + "".join(events) + "END:VCALENDAR\r\n").encode()


@pytest.mark.parametrize("rule", ["RRULE:FREQ=SECONDLY\r\n", "RRULE:FREQ=MINUTELY\r\n", "RRULE:FREQ=HOURLY;INTERVAL=2\r\n",
                                  "RRULE:FREQ=DAILY;BYHOUR=9,10\r\n"])
def test_rules_that_repeat_more_than_daily_are_refused_at_import(book, home, rule):
    result = import_calendar(book, home, wrapped(one_event("fine"), one_event("tick", rule)))
    assert result["imported"] == 1
    assert [entry["title"] for entry in result["refused"]] == ["tick"]
    assert "more than once a day" in result["refused"][0]["reason"]


def test_the_size_and_event_count_are_checked_before_the_file_is_read(book, home, monkeypatch):
    from calendarapp import transfer
    assert transfer.MAX_IMPORT_BYTES <= 2 * 1024 * 1024

    def must_not_parse(*args, **kwargs):
        raise AssertionError("the file was parsed before its limits were checked")

    monkeypatch.setattr(transfer.icalendar.Calendar, "from_ical", must_not_parse)
    too_many = wrapped(*(one_event(f"e{number}") for number in range(transfer.MAX_IMPORT_EVENTS + 1)))
    with pytest.raises(CalendarError, match="at most") as refused:
        import_calendar(book, home, too_many)
    assert refused.value.status == 413
    with pytest.raises(CalendarError) as too_big:
        import_calendar(book, home, b"X" * (transfer.MAX_IMPORT_BYTES + 1))
    assert too_big.value.status == 413


def test_an_unknown_x_wr_timezone_is_a_clear_refusal(book, home):
    with pytest.raises(CalendarError) as refused:
        import_calendar(book, home, wrapped(one_event("a").replace("DTSTART:20261120T120000Z", "DTSTART:20261120T120000"),
                                            header="X-WR-TIMEZONE:Not/AZone\r\n"))
    assert refused.value.status == 400 and "time zone" in str(refused.value)


def test_an_event_whose_dates_cannot_be_worked_out_is_refused_at_import(book, home):
    forever = one_event("forever").replace("DTEND:20261120T130000Z", "DTEND:99991231T235959Z")
    result = import_calendar(book, home, wrapped(one_event("fine"), forever))
    assert result["imported"] == 1 and [entry["title"] for entry in result["refused"]] == ["forever"]


def test_a_repeat_too_slow_to_work_out_is_refused_at_import(book, home):
    ancient = one_event("ancient", "RRULE:FREQ=DAILY\r\n").replace("20261120T", "00010101T")
    result = import_calendar(book, home, wrapped(one_event("fine"), ancient))
    assert result["imported"] == 1 and [entry["title"] for entry in result["refused"]] == ["ancient"]
    assert "too long" in result["refused"][0]["reason"]


def test_an_import_stops_checking_when_its_time_is_used_up(book, home, monkeypatch):
    from calendarapp import transfer
    monkeypatch.setattr(transfer, "IMPORT_CHECK_SECONDS", -1)
    result = import_calendar(book, home, wrapped(one_event("first"), one_event("second")))
    assert result["imported"] == 0 and [entry["title"] for entry in result["refused"]] == ["first", "second"]
    assert "smaller file" in result["refused"][0]["reason"]
