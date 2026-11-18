from datetime import date

import pytest

from calendarapp.errors import CalendarError
from conftest import LONDON


def event(home, **overrides):
    fields = {"calendarId": home, "title": "Dinner with mum", "start": "2026-11-20T19:00", "end": "2026-11-20T21:00"}
    fields.update(overrides)
    return fields


def listed(book, first, last):
    found, _ = book.occurrences(date.fromisoformat(first), date.fromisoformat(last))
    from calendarapp.events import occurrence_json
    return [occurrence_json(occurrence, LONDON) for occurrence in found]


def test_a_new_store_gets_one_calendar(book):
    calendars = book.calendar_list()
    assert [calendar["name"] for calendar in calendars] == ["Personal"]


def test_a_timed_event_is_listed_in_local_time(book, home):
    book.create(event(home, location="Nando's", notes="book a table", reminders=[30]))
    [shown] = listed(book, "2026-11-20", "2026-11-21")
    assert shown["title"] == "Dinner with mum"
    assert (shown["start"], shown["end"]) == ("2026-11-20T19:00", "2026-11-20T21:00")
    assert shown["startsAt"] == "2026-11-20T19:00:00Z"
    assert shown["location"] == "Nando's" and shown["notes"] == "book a table"
    assert shown["reminders"] == [30]
    assert shown["recurring"] is False and shown["occurrence"] is None


def test_summer_times_are_an_hour_ahead_of_utc(book, home):
    book.create(event(home, start="2027-06-01T09:00", end="2027-06-01T10:00"))
    [shown] = listed(book, "2027-06-01", "2027-06-02")
    assert shown["start"] == "2027-06-01T09:00"
    assert shown["startsAt"] == "2027-06-01T08:00:00Z"


def test_a_weekly_event_keeps_its_clock_time_across_the_spring_change(book, home):
    book.create(event(home, title="Standup", start="2027-03-22T09:00", end="2027-03-22T09:15", repeat={"freq": "weekly", "count": 3}))
    shown = listed(book, "2027-03-01", "2027-04-30")
    assert [item["start"] for item in shown] == ["2027-03-22T09:00", "2027-03-29T09:00", "2027-04-05T09:00"]
    assert [item["startsAt"] for item in shown] == ["2027-03-22T09:00:00Z", "2027-03-29T08:00:00Z", "2027-04-05T08:00:00Z"]
    assert shown[0]["repeatText"] == "Every week on Mon, 3 times"


def test_a_daily_event_keeps_its_clock_time_across_the_autumn_change(book, home):
    book.create(event(home, title="Walk", start="2027-10-30T08:00", end="2027-10-30T09:00", repeat={"freq": "daily", "count": 2}))
    shown = listed(book, "2027-10-29", "2027-11-02")
    assert [item["start"] for item in shown] == ["2027-10-30T08:00", "2027-10-31T08:00"]
    assert [item["startsAt"] for item in shown] == ["2027-10-30T07:00:00Z", "2027-10-31T08:00:00Z"]


def test_a_time_lost_when_the_clocks_go_forward_is_refused(book, home):
    with pytest.raises(CalendarError, match="clocks go forward"):
        book.create(event(home, start="2027-03-28T01:30", end="2027-03-28T02:30"))


def test_a_time_that_happens_twice_when_the_clocks_go_back_is_the_first_one(book, home):
    book.create(event(home, start="2027-10-31T01:30", end="2027-10-31T03:00"))
    [shown] = listed(book, "2027-10-31", "2027-11-01")
    assert shown["startsAt"] == "2027-10-31T00:30:00Z"
    assert shown["endsAt"] == "2027-10-31T03:00:00Z"


def test_all_day_and_multi_day_events(book, home):
    book.create(event(home, title="Holiday", allDay=True, start="2026-12-24", end="2026-12-27"))
    shown = listed(book, "2026-12-20", "2026-12-31")
    assert [(item["allDay"], item["start"], item["end"]) for item in shown] == [(True, "2026-12-24", "2026-12-27")]
    assert listed(book, "2026-12-26", "2026-12-27")[0]["title"] == "Holiday"
    assert listed(book, "2026-12-28", "2026-12-29") == []


def test_a_timed_event_over_several_days_shows_on_each(book, home):
    book.create(event(home, title="Festival", start="2026-11-20T18:00", end="2026-11-22T12:00"))
    assert len(listed(book, "2026-11-21", "2026-11-22")) == 1


@pytest.mark.parametrize("change, message", [
    ({"title": "  "}, "give the event a title"),
    ({"end": "2026-11-20T18:00"}, "ends before it starts"),
    ({"start": "2026-11-31T19:00"}, "a day that exists"),
    ({"start": "tomorrow"}, "2027-01-31T19:30"),
    ({"reminders": [1, 2, 3, 4, 5, 6]}, "at most 5"),
    ({"reminders": ["15"]}, "whole number of minutes"),
    ({"repeat": {"freq": "hourly"}}, "repeat.freq"),
    ({"repeat": {"freq": "daily", "count": 3, "until": "2027-01-01"}}, "not both"),
    ({"repeat": {"freq": "daily", "until": "2026-01-01"}}, "before the event starts"),
    ({"repeat": {"freq": "weekly", "weekdays": ["XX"]}}, "repeat.weekdays"),
    ({"allDay": "yes"}, "allDay must be true or false"),
    ({"end": "2028-11-20T19:00"}, "at most 366 days"),
    ({"title": "x" * 501}, "too long"),
])
def test_bad_fields_are_refused(book, home, change, message):
    with pytest.raises(CalendarError, match=message):
        book.create(event(home, **change))


def test_unknown_calendar_is_refused(book, home):
    with pytest.raises(CalendarError) as error:
        book.create(event("nope"))
    assert error.value.status == 404
    with pytest.raises(CalendarError):
        book.create(event("../other"))


def test_edit_one_date_of_a_repeating_event(book, home):
    created = book.create(event(home, title="Gym", start="2026-11-16T07:00", end="2026-11-16T08:00", repeat={"freq": "weekly"}))
    second = listed(book, "2026-11-23", "2026-11-24")[0]
    book.update(home, created["itemId"], {"start": "2026-11-23T18:00", "end": "2026-11-23T19:00", "title": "Gym (evening)"},
                scope="one", occurrence=second["occurrence"])
    shown = listed(book, "2026-11-16", "2026-12-01")
    assert [(item["start"], item["title"], item["changedInstance"]) for item in shown] == [
        ("2026-11-16T07:00", "Gym", False), ("2026-11-23T18:00", "Gym (evening)", True), ("2026-11-30T07:00", "Gym", False)]


def test_edit_all_dates_moves_the_series_and_keeps_changed_dates(book, home):
    created = book.create(event(home, title="Gym", start="2026-11-16T07:00", end="2026-11-16T08:00", repeat={"freq": "weekly"}))
    second = listed(book, "2026-11-23", "2026-11-24")[0]
    book.update(home, created["itemId"], {"title": "Gym (late)"}, scope="one", occurrence=second["occurrence"])
    third = listed(book, "2026-11-30", "2026-12-01")[0]
    book.update(home, created["itemId"], {"start": "2026-11-30T07:30", "end": "2026-11-30T08:30", "title": "Gym!"},
                scope="all", occurrence=third["occurrence"])
    shown = listed(book, "2026-11-16", "2026-12-08")
    assert [(item["start"], item["title"]) for item in shown] == [
        ("2026-11-16T07:30", "Gym!"), ("2026-11-23T07:30", "Gym (late)"), ("2026-11-30T07:30", "Gym!"), ("2026-12-07T07:30", "Gym!")]


def test_moving_a_weekly_series_to_another_day_moves_its_weekday(book, home):
    created = book.create(event(home, title="Swim", start="2026-11-16T07:00", end="2026-11-16T08:00", repeat={"freq": "weekly"}))
    first = listed(book, "2026-11-16", "2026-11-17")[0]
    book.update(home, created["itemId"], {"start": "2026-11-17T07:00", "end": "2026-11-17T08:00"}, scope="all", occurrence=first["occurrence"])
    shown = listed(book, "2026-11-16", "2026-12-01")
    assert [item["start"] for item in shown] == ["2026-11-17T07:00", "2026-11-24T07:00"]


def test_changing_how_it_repeats_drops_changed_dates(book, home):
    created = book.create(event(home, title="Gym", start="2026-11-16T07:00", end="2026-11-16T08:00", repeat={"freq": "weekly"}))
    second = listed(book, "2026-11-23", "2026-11-24")[0]
    book.update(home, created["itemId"], {"title": "Moved"}, scope="one", occurrence=second["occurrence"])
    book.update(home, created["itemId"], {"repeat": {"freq": "daily", "count": 3}}, scope="all")
    shown = listed(book, "2026-11-16", "2026-12-01")
    assert [(item["start"], item["title"]) for item in shown] == [
        ("2026-11-16T07:00", "Gym"), ("2026-11-17T07:00", "Gym"), ("2026-11-18T07:00", "Gym")]


def test_delete_one_date_and_then_all(book, home):
    created = book.create(event(home, title="Class", allDay=True, start="2026-11-16", end="2026-11-16", repeat={"freq": "daily", "count": 3}))
    middle = listed(book, "2026-11-17", "2026-11-18")[0]
    book.delete(home, created["itemId"], scope="one", occurrence=middle["occurrence"])
    assert [item["start"] for item in listed(book, "2026-11-16", "2026-11-20")] == ["2026-11-16", "2026-11-18"]
    book.delete(home, created["itemId"], scope="all")
    assert listed(book, "2026-11-16", "2026-11-20") == []


def test_a_date_that_is_not_part_of_the_series_is_refused(book, home):
    created = book.create(event(home, start="2026-11-16T07:00", end="2026-11-16T08:00", repeat={"freq": "weekly"}))
    with pytest.raises(CalendarError) as error:
        book.delete(home, created["itemId"], scope="one", occurrence="2026-11-17T07:00:00Z")
    assert error.value.status == 404


def test_repeat_until_a_date_includes_that_day(book, home):
    book.create(event(home, start="2026-11-16T19:00", end="2026-11-16T20:00", repeat={"freq": "daily", "until": "2026-11-18"}))
    assert [item["start"][:10] for item in listed(book, "2026-11-15", "2026-11-25")] == ["2026-11-16", "2026-11-17", "2026-11-18"]


def test_monthly_on_the_nth_weekday_and_yearly(book, home):
    book.create(event(home, title="Book club", start="2026-11-10T19:00", end="2026-11-10T20:00",
                      repeat={"freq": "monthly", "monthlyBy": "weekday", "count": 3}))
    book.create(event(home, title="Birthday", allDay=True, start="2026-12-02", end="2026-12-02", repeat={"freq": "yearly"}))
    shown = listed(book, "2026-11-01", "2027-12-03")
    assert [item["start"][:10] for item in shown if item["title"] == "Book club"] == ["2026-11-10", "2026-12-08", "2027-01-12"]
    assert [item["start"] for item in shown if item["title"] == "Birthday"] == ["2026-12-02", "2027-12-02"]
    assert shown[0]["repeatText"] == "Every month on the second Tuesday, 3 times"


def test_moving_an_event_to_another_calendar(book, home):
    work = book.create_calendar("Work", "#5b8def")
    created = book.create(event(home))
    book.update(home, created["itemId"], {"calendarId": work["id"]})
    [shown] = listed(book, "2026-11-20", "2026-11-21")
    assert shown["calendarId"] == work["id"] and shown["color"] == "#5b8def"


def test_a_phone_event_in_utc_is_shown_in_london_time(book, home, dav):
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\nUID:phone-1\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20270701T120000Z\r\nDTEND:20270701T130000Z\r\nSUMMARY:From the phone\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    dav.put(home, "phone-1.ics", text, None)
    [shown] = listed(book, "2027-07-01", "2027-07-02")
    assert (shown["start"], shown["end"]) == ("2027-07-01T13:00", "2027-07-01T14:00")


def test_a_rule_this_app_cannot_edit_is_kept(book, home, dav):
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\nUID:phone-2\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART;TZID=Europe/London:20261102T090000\r\nDTEND;TZID=Europe/London:20261102T100000\r\n"
            "RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1\r\nSUMMARY:First Monday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    dav.put(home, "phone-2.ics", text, None)
    first = listed(book, "2026-11-02", "2026-11-03")[0]
    assert first["repeat"]["custom"] is True
    book.update(home, "phone-2.ics", {"title": "First Monday!"}, scope="all", occurrence=first["occurrence"])
    shown = listed(book, "2026-11-01", "2027-01-31")
    assert [(item["start"], item["title"]) for item in shown] == [
        ("2026-11-02T09:00", "First Monday!"), ("2026-12-07T09:00", "First Monday!"), ("2027-01-04T09:00", "First Monday!")]


def test_a_broken_item_does_not_hide_the_others(book, home, dav):
    book.create(event(home))
    status, _, _ = dav._send("PUT", f"/owner/{home}/broken.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                             {"Content-Type": "text/calendar"})
    assert len(listed(book, "2026-11-20", "2026-11-21")) == 1


def test_calendars_can_be_made_renamed_and_deleted(book, home):
    work = book.create_calendar("Work", "#5b8def")
    book.change_calendar(work["id"], "Office", "#123456")
    assert {(item["name"], item["color"]) for item in book.calendar_list()} == {("Personal", "#e5383b"), ("Office", "#123456")}
    book.delete_calendar(work["id"])
    with pytest.raises(CalendarError, match="only calendar"):
        book.delete_calendar(home)
    with pytest.raises(CalendarError, match="color"):
        book.create_calendar("Bad", "red")


def test_a_change_made_elsewhere_in_between_is_not_overwritten(book, home, dav):
    created = book.create(event(home))
    stale = dav.item(home, created["itemId"])
    book.update(home, created["itemId"], {"title": "First change"})
    from calendarapp.davstore import ChangedElsewhere
    with pytest.raises(ChangedElsewhere):
        dav.put(home, created["itemId"], stale.text, stale.etag)


def test_calendars_keep_the_order_they_were_made_in(book, home):
    book.create_calendar("Work", "#5b8def")
    book.create_calendar("Family", "#5fd990")
    assert [calendar["name"] for calendar in book.calendar_list()] == ["Personal", "Work", "Family"]


def phone_item(uid, start, end, rule, extra=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:{start}\r\nDTEND:{end}\r\nRRULE:{rule}\r\n{extra}"
            "SUMMARY:Tick\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def test_a_rule_finer_than_hourly_is_shown_once_as_a_custom_rule(book, home, dav):
    dav.put(home, "tick.ics", phone_item("tick", "20261112T230000Z", "20261112T230001Z", "FREQ=MINUTELY"), None)
    assert listed(book, "2026-11-13", "2026-11-14") == []
    [shown] = listed(book, "2026-11-12", "2026-11-13")
    assert shown["start"] == "2026-11-12T23:00" and shown["repeat"]["custom"] is True
    with pytest.raises(CalendarError, match="more often than once an hour"):
        book.update(home, "tick.ics", {"title": "x"}, scope="one", occurrence="2026-11-12T23:00:00Z")


def test_each_item_stops_at_its_own_cap_so_other_events_still_show(book, home, dav):
    from calendarapp.events import MAX_OCCURRENCES_PER_ITEM
    every_hour = "FREQ=DAILY;BYHOUR=" + ",".join(str(hour) for hour in range(24))
    dav.put(home, "hourly.ics", phone_item("hourly", "20261113T000000Z", "20261113T000100Z", every_hour), None)
    book.create(event(home, title="Late in the window", start="2027-12-01T09:00", end="2027-12-01T10:00"))
    found, truncated = book.occurrences(date(2026, 11, 13), date(2027, 12, 2))
    titles = [str(occurrence.instance.get("SUMMARY")) for occurrence in found]
    assert titles.count("Tick") == MAX_OCCURRENCES_PER_ITEM
    assert "Late in the window" in titles
    assert truncated is True


def test_saving_with_a_stale_etag_is_refused(book, home):
    from calendarapp.davstore import ChangedElsewhere
    created = book.create(event(home))
    [seen] = listed(book, "2026-11-20", "2026-11-21")
    book.update(home, created["itemId"], {"location": "changed on the phone"})
    with pytest.raises(ChangedElsewhere):
        book.update(home, created["itemId"], {"title": "stale page"}, expected_etag=seen["etag"])
    with pytest.raises(ChangedElsewhere):
        book.delete(home, created["itemId"], expected_etag=seen["etag"])
    [now] = listed(book, "2026-11-20", "2026-11-21")
    assert (now["title"], now["location"]) == ("Dinner with mum", "changed on the phone")
    book.update(home, created["itemId"], {"title": "fresh page"}, expected_etag=now["etag"])
    assert listed(book, "2026-11-20", "2026-11-21")[0]["title"] == "fresh page"


def test_moving_a_weekly_series_to_another_day_keeps_changed_and_skipped_dates(book, home):
    created = book.create(event(home, title="Club", start="2027-01-04T09:00", end="2027-01-04T10:00", repeat={"freq": "weekly"}))
    second = listed(book, "2027-01-11", "2027-01-12")[0]
    book.update(home, created["itemId"], {"title": "Club (hall)"}, scope="one", occurrence=second["occurrence"])
    third = listed(book, "2027-01-18", "2027-01-19")[0]
    book.delete(home, created["itemId"], scope="one", occurrence=third["occurrence"])
    first = listed(book, "2027-01-04", "2027-01-05")[0]
    book.update(home, created["itemId"], {"start": "2027-01-05T09:00", "end": "2027-01-05T10:00", "repeat": first["repeat"]},
                scope="all", occurrence=first["occurrence"])
    shown = listed(book, "2027-01-04", "2027-01-31")
    assert [(item["start"], item["title"], item["changedInstance"]) for item in shown] == [
        ("2027-01-05T09:00", "Club", False), ("2027-01-12T09:00", "Club (hall)", True), ("2027-01-26T09:00", "Club", False)]


def test_turning_off_the_repeat_from_a_later_date_keeps_that_date(book, home):
    created = book.create(event(home, title="Club", start="2027-04-05T18:00", end="2027-04-05T19:00", repeat={"freq": "weekly"}))
    later = listed(book, "2027-05-03", "2027-05-04")[0]
    book.update(home, created["itemId"], {"start": later["start"], "end": later["end"], "repeat": None},
                scope="all", occurrence=later["occurrence"])
    assert [(item["start"], item["recurring"]) for item in listed(book, "2027-04-01", "2027-06-30")] == [("2027-05-03T18:00", False)]


def test_a_repeating_time_lost_to_the_spring_change_keeps_its_length(book, home):
    book.create(event(home, title="Night shift", start="2026-03-20T01:30", end="2026-03-20T02:15", repeat={"freq": "daily"}))
    [shown] = listed(book, "2026-03-29", "2026-03-30")
    assert (shown["start"], shown["end"]) == ("2026-03-29T02:30", "2026-03-29T03:15")
    assert shown["startsAt"] < shown["endsAt"]


def test_first_requests_at_once_make_only_one_default_calendar(dav, clock):
    import threading
    from calendarapp.events import EventBook
    book = EventBook(dav, LONDON, clock=clock)
    starters = 8
    barrier = threading.Barrier(starters)

    def first_request():
        barrier.wait()
        book.calendars()

    threads = [threading.Thread(target=first_request) for _ in range(starters)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [info.display_name for info in dav.calendars()] == ["Personal"]


def test_an_event_whose_dates_cannot_be_worked_out_is_logged_once(book, home, dav, caplog):
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\nUID:forever\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20261120T120000Z\r\nDTEND:99991231T235959Z\r\nSUMMARY:Forever\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    dav.put(home, "forever.ics", text, None)
    book.create(event(home))
    with caplog.at_level("WARNING", logger="calendar.events"):
        for _ in range(3):
            assert [item["title"] for item in listed(book, "2026-11-20", "2026-11-21")] == ["Dinner with mum"]
    assert len([record for record in caplog.records if "forever.ics" in record.getMessage()]) == 1


def test_rebuilding_the_parsed_cache_is_safe_while_another_request_adds_to_it(book, home):
    import threading
    from calendarapp.when import start_of_day
    work = book.create_calendar("Work", "#5b8def")["id"]
    book.create(event(home))
    book.create(event(work, title="Work dinner"))
    work_info = book.store.calendar(work)
    iterating, inserted = threading.Event(), threading.Event()

    class SlowCache(dict):
        def items(self):
            for pair in super().items():
                iterating.set()
                inserted.wait(0.5)
                yield pair

    def other_request():
        iterating.wait(5)
        book._parsed_items(work_info)
        inserted.set()

    listed(book, "2026-11-20", "2026-11-21")
    book.parsed = SlowCache({key: value for key, value in book.parsed.items() if key[0] == home})
    helper = threading.Thread(target=other_request)
    helper.start()
    book.occurrences_between(start_of_day(date(2026, 11, 20), LONDON), start_of_day(date(2026, 11, 21), LONDON), [home])
    helper.join(5)
    assert inserted.is_set()
    assert any(key[0] == work for key in book.parsed)


def test_a_calendar_made_elsewhere_with_a_name_this_app_cannot_use_is_skipped(book, home, dav, caplog):
    status, _, _ = dav._send("MKCALENDAR", "/owner/nb%C2%A0sp/")
    assert status == 201
    try:
        book.create(event(home))
        with caplog.at_level("WARNING", logger="calendar.davstore"):
            assert [calendar["id"] for calendar in book.calendar_list()] == [home]
            assert [item["title"] for item in listed(book, "2026-11-20", "2026-11-21")] == ["Dinner with mum"]
        assert any("nb" in record.getMessage() for record in caplog.records)
    finally:
        dav._send("DELETE", "/owner/nb%C2%A0sp/")
