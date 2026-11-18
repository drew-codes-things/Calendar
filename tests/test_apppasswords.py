import pytest

from calendarapp.apppasswords import MAX_ACTIVE_PASSWORDS, password_hash
from calendarapp.errors import CalendarError


def test_only_a_hash_is_stored(passwords, database):
    made = passwords.create("Phone")
    with database.connect() as connection:
        stored = connection.execute("SELECT password_hash FROM app_passwords").fetchone()[0]
    assert stored == password_hash(made["password"]) and made["password"] not in stored
    assert len(made["password"]) == 29


def test_passwords_are_different_each_time(passwords):
    assert passwords.create("A")["password"] != passwords.create("B")["password"]


def test_last_use_is_recorded(passwords, clock):
    made = passwords.create("Phone")
    assert passwords.check(made["password"], "203.0.113.5")
    assert passwords.active()[0]["lastUsedFrom"] == "203.0.113.5"


@pytest.mark.parametrize("name", [None, "", "   ", "x" * 81, "bad\x07name", 5])
def test_bad_names_are_refused(passwords, name):
    with pytest.raises(CalendarError):
        passwords.create(name)


def test_there_is_a_cap(passwords):
    for number in range(MAX_ACTIVE_PASSWORDS):
        passwords.create(f"Device {number}")
    with pytest.raises(CalendarError, match="revoke one"):
        passwords.create("One more")


@pytest.mark.parametrize("attempt", [None, "", 5, "x" * 300])
def test_odd_attempts_fail_quietly(passwords, attempt):
    passwords.create("Phone")
    assert passwords.check(attempt, "1.2.3.4") is False


def test_the_database_and_its_folder_are_private_whatever_the_umask(tmp_path):
    import os
    import stat
    from calendarapp.db import Database
    previous = os.umask(0o002)
    try:
        Database(tmp_path / "fresh" / "calendar.db")
        loose = tmp_path / "loose"
        loose.mkdir(mode=0o775)
        (loose / "calendar.db").touch(mode=0o644)
        Database(loose / "calendar.db")
    finally:
        os.umask(previous)
    for folder in ("fresh", "loose"):
        assert stat.S_IMODE((tmp_path / folder).stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / folder / "calendar.db").stat().st_mode) == 0o600


def test_reminder_ids_are_never_reused_after_an_upgrade(tmp_path):
    import sqlite3
    from calendarapp.db import Database
    path = tmp_path / "calendar.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE reminders_sent (id INTEGER PRIMARY KEY, reminder_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
                      "message TEXT NOT NULL, alarm_at REAL NOT NULL, sent_at REAL NOT NULL, arty_result TEXT NOT NULL DEFAULT '');"
                      "INSERT INTO reminders_sent VALUES (7, 'k7', 'old', 'm', 1, 1, 'not linked');")
    old.close()
    database = Database(path)
    with database.connect(immediate=True) as connection:
        assert connection.execute("SELECT title FROM reminders_sent WHERE id = 7").fetchone()[0] == "old"
        connection.execute("DELETE FROM reminders_sent")
        connection.execute("INSERT INTO reminders_sent (reminder_key, title, message, alarm_at, sent_at) VALUES ('n', 'new', 'm', 2, 2)")
        assert connection.execute("SELECT id FROM reminders_sent").fetchone()[0] == 8
