from datetime import datetime, timezone

import pytest

from calendarapp.errors import CalendarError
from calendarapp.reminders import ARTY_KEY_STATE, ArtyNotifier
from conftest import FakeArty


def at_utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def add(book, home, **fields):
    body = {"calendarId": home, "title": "Dentist", "start": "2026-11-13T10:30", "end": "2026-11-13T11:00", "reminders": [15]}
    body.update(fields)
    return book.create(body)


def test_a_reminder_is_sent_once_when_it_falls_due(book, home, desk, clock):
    add(book, home, location="High St")
    assert desk.run_once() == 0
    clock.set(at_utc(2026, 11, 13, 10, 15, 5))
    assert desk.run_once() == 1
    assert desk.run_once() == 0
    reminders, latest = desk.feed(0)
    assert [(item["title"], item["message"], item["artyResult"]) for item in reminders] == [
        ("Dentist", "Friday 13 November, 10:30 to 11:00 at High St", "not linked")]
    assert desk.feed(latest)[0] == []


def test_a_reminder_missed_by_more_than_the_grace_is_skipped(book, home, desk, clock):
    add(book, home)
    clock.set(at_utc(2026, 11, 13, 10, 40))
    assert desk.run_once() == 0


def test_reminders_follow_the_spring_clock_change(book, home, desk, clock):
    add(book, home, start="2027-03-28T09:00", end="2027-03-28T10:00", reminders=[60])
    clock.set(at_utc(2027, 3, 28, 6, 59))
    assert desk.run_once() == 0
    clock.set(at_utc(2027, 3, 28, 7, 0, 30))
    assert desk.run_once() == 1


def test_each_date_of_a_repeating_event_gets_its_reminder(book, home, desk, clock):
    add(book, home, start="2026-11-13T10:30", end="2026-11-13T11:00", repeat={"freq": "daily", "count": 3})
    for day in (13, 14, 15):
        clock.set(at_utc(2026, 11, day, 10, 16))
        assert desk.run_once() == 1
    assert len(desk.feed(0)[0]) <= 3


def test_all_day_reminders_count_from_midnight(book, home, desk, clock):
    add(book, home, title="Bins", allDay=True, start="2026-11-14", end="2026-11-14", reminders=[900])
    clock.set(at_utc(2026, 11, 13, 9, 0, 10))
    assert desk.run_once() == 1
    assert desk.feed(0)[0][0]["message"] == "Saturday 14 November (all day)"


def test_an_absolute_alarm_from_a_phone_is_honoured(book, home, desk, clock, dav):
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\nUID:abs\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20261120T120000Z\r\nDTEND:20261120T130000Z\r\nSUMMARY:Phone event\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n"
            "DESCRIPTION:x\r\nTRIGGER;VALUE=DATE-TIME:20261113T110000Z\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    dav.put(home, "abs.ics", text, None)
    clock.set(at_utc(2026, 11, 13, 11, 0, 30))
    assert desk.run_once() == 1


def test_linked_reminders_go_to_arty_with_the_calendar_category(book, home, desk, clock, notifier, database, monkeypatch):
    fake = FakeArty()
    monkeypatch.setattr(notifier, "_post", fake)
    notifier.base_url = "http://arty.test:8081"
    database.set_state(ARTY_KEY_STATE, "arty_dev_secret")
    add(book, home)
    clock.set(at_utc(2026, 11, 13, 10, 15, 5))
    desk.run_once()
    assert fake.sent == [("/v1/notify", {"title": "Starfire Calendar: Dentist", "message": "Friday 13 November, 10:30 to 11:00",
                                         "category": "calendar"}, "arty_dev_secret")]
    assert desk.feed(0)[0][0]["artyResult"] == "sent to Updates"


def test_a_refused_key_is_reported_not_raised(notifier, database, monkeypatch):
    fake = FakeArty()
    fake.answers["/v1/notify"] = (401, {})
    monkeypatch.setattr(notifier, "_post", fake)
    notifier.base_url = "http://arty.test:8081"
    database.set_state(ARTY_KEY_STATE, "old")
    assert notifier.send("t", "m") == "key refused"


def test_an_unreachable_arty_is_reported_not_raised(database):
    notifier = ArtyNotifier(database, "http://127.0.0.1:9", timeout=1)
    database.set_state(ARTY_KEY_STATE, "k")
    assert notifier.send("t", "m") == "Arty unreachable"


def test_linking_to_arty(notifier, database, monkeypatch):
    fake = FakeArty()
    fake.answers["/v1/device/start"] = (200, {"device_code": "dev", "user_code": "ABCD-EFGH", "expires_in": 600})
    fake.answers["/v1/device/token"] = (202, {"status": "pending"})
    monkeypatch.setattr(notifier, "_post", fake)
    notifier.base_url = "http://arty.test:8081"
    assert notifier.start_link() == {"userCode": "ABCD-EFGH", "expiresIn": 600}
    assert notifier.finish_link() == "waiting"
    fake.answers["/v1/device/token"] = (200, {"api_key": "arty_dev_new"})
    assert notifier.finish_link() == "linked"
    assert database.state(ARTY_KEY_STATE) == "arty_dev_new"
    assert notifier.is_linked
    notifier.unlink()
    assert not notifier.is_linked


def test_a_denied_link_saves_nothing(notifier, database, monkeypatch):
    fake = FakeArty()
    fake.answers["/v1/device/start"] = (200, {"device_code": "dev", "user_code": "ABCD-EFGH"})
    fake.answers["/v1/device/token"] = (403, {"error": "denied"})
    monkeypatch.setattr(notifier, "_post", fake)
    notifier.base_url = "http://arty.test:8081"
    notifier.start_link()
    assert notifier.finish_link() == "denied"
    assert database.state(ARTY_KEY_STATE) is None


def test_linking_needs_an_arty_address(notifier):
    with pytest.raises(CalendarError, match="ARTY_URL"):
        notifier.start_link()


def test_an_absolute_alarm_on_a_repeating_event_fires_once(book, home, desk, clock, dav):
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\nUID:water-plants\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART;TZID=Europe/London:20261101T120000\r\nDTEND;TZID=Europe/London:20261101T130000\r\nRRULE:FREQ=DAILY\r\n"
            "SUMMARY:Water the plants\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:x\r\nTRIGGER;VALUE=DATE-TIME:20261113T110000Z\r\n"
            "END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    dav.put(home, "water-plants.ics", text, None)
    clock.set(at_utc(2026, 11, 13, 11, 0, 30))
    assert desk.run_once() == 1
    assert len(desk.feed(0)[0]) == 1


def test_the_page_feed_keeps_working_after_a_quiet_month(desk, clock):
    from calendarapp.reminders import DueReminder
    moment = datetime.fromtimestamp(clock(), timezone.utc)
    for name in ("a", "b", "c"):
        desk._claim(DueReminder(name, name, "m", moment))
    _, latest = desk.feed(0)
    clock.advance(31 * 86400)
    desk._forget_old()
    desk._claim(DueReminder("later", "later", "m", datetime.fromtimestamp(clock(), timezone.utc)))
    reminders, newest = desk.feed(latest)
    assert [item["title"] for item in reminders] == ["later"] and newest > latest


def test_a_failed_delivery_to_arty_is_tried_again(book, home, desk, clock, notifier, database, monkeypatch):
    from calendarapp.reminders import MAX_ARTY_ATTEMPTS
    fake = FakeArty()
    fake.answers["/v1/notify"] = (503, {})
    monkeypatch.setattr(notifier, "_post", fake)
    notifier.base_url = "https://arty.test"
    database.set_state(ARTY_KEY_STATE, "k")
    add(book, home)
    clock.set(at_utc(2026, 11, 13, 10, 15, 5))
    desk.run_once()
    fake.answers["/v1/notify"] = (200, {"result": "sent to Updates"})
    clock.advance(30)
    desk.run_once()
    clock.advance(30)
    desk.run_once()
    assert len(fake.sent) == 2
    assert desk.feed(0)[0][0]["artyResult"] == "sent to Updates"
    add(book, home, title="Physio", start="2026-11-13T11:00", end="2026-11-13T12:00", reminders=[15])
    fake.answers["/v1/notify"] = (503, {})
    clock.set(at_utc(2026, 11, 13, 10, 45, 5))
    for _ in range(MAX_ARTY_ATTEMPTS + 3):
        desk.run_once()
        clock.advance(30)
    assert len(fake.sent) == 2 + MAX_ARTY_ATTEMPTS


def test_arty_redirects_are_not_followed(database):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    reached = []

    class Redirecting(BaseHTTPRequestHandler):
        def do_POST(self):
            reached.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        notifier = ArtyNotifier(database, f"http://127.0.0.1:{server.server_port}", timeout=5)
        database.set_state(ARTY_KEY_STATE, "k")
        assert notifier.send("t", "m") == "Arty answered 302"
        assert reached == ["/v1/notify"]
    finally:
        server.shutdown()
