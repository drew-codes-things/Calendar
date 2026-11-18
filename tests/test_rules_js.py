import json
import shutil
import subprocess
from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parent.parent / "calendarapp" / "static" / "rules.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_rules(expression: str):
    script = f"const R = require({json.dumps(str(RULES))}); process.stdout.write(JSON.stringify({expression}));"
    finished = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(finished.stdout)


@pytest.mark.parametrize(("server", "editor"), [
    ({"freq": "weekly", "interval": 1, "until": "2027-06-30", "weekdays": ["MO"]},
     {"freq": "weekly", "interval": 1, "weekdays": ["MO"], "until": "2027-06-30"}),
    ({"count": 10, "freq": "daily", "interval": 1}, {"freq": "daily", "interval": 1, "count": 10}),
    ({"freq": "weekly", "weekdays": ["WE", "MO"]}, {"freq": "weekly", "interval": 1, "weekdays": ["MO", "WE"]}),
    ({"freq": "monthly", "interval": 1}, {"freq": "monthly", "interval": 1, "monthlyBy": "day"}),
])
def test_an_untouched_repeat_in_another_key_order_is_not_a_change(server, editor):
    assert run_rules(f"R.repeatChanged({json.dumps(server)}, {json.dumps(editor)})") is False


@pytest.mark.parametrize(("server", "editor"), [
    ({"freq": "weekly", "interval": 1, "weekdays": ["MO"]}, {"freq": "weekly", "interval": 1, "weekdays": ["TU"]}),
    ({"freq": "daily", "interval": 1, "count": 10}, {"freq": "daily", "interval": 1, "count": 11}),
    ({"freq": "daily", "interval": 1}, {"freq": "daily", "interval": 2}),
    ({"freq": "daily", "interval": 1}, None),
    (None, {"freq": "daily", "interval": 1}),
    ({"freq": "monthly", "interval": 1, "monthlyBy": "day"}, {"freq": "monthly", "interval": 1, "monthlyBy": "weekday"}),
    ({"freq": "weekly", "interval": 1, "until": "2027-06-30", "weekdays": ["MO"]}, {"freq": "weekly", "interval": 1, "weekdays": ["MO"]}),
])
def test_a_real_repeat_change_is_seen(server, editor):
    assert run_rules(f"R.repeatChanged({json.dumps(server)}, {json.dumps(editor)})") is True


def test_the_reminder_cursor_follows_the_server_when_its_numbers_start_again():
    assert run_rules("[R.nextReminderCursor(3, 1), R.nextReminderCursor(3, 7), R.nextReminderCursor(0, 0)]") == [1, 7, 0]


def test_ticking_all_day_keeps_reminders_the_person_chose():
    chosen = run_rules("[R.remindersAfterAllDayToggle([15, 60], true), R.remindersAfterAllDayToggle([-540], false)]")
    assert chosen == [[15, 60], [-540]]


def test_ticking_all_day_swaps_only_the_untouched_default():
    swapped = run_rules("[R.remindersAfterAllDayToggle([30], true), R.remindersAfterAllDayToggle([], true), "
                        "R.remindersAfterAllDayToggle([], false)]")
    assert swapped == [[], [], [30]]


@pytest.mark.parametrize(("answer", "signed_out"), [
    ({"redirected": True, "contentType": "text/html; charset=utf-8", "status": 200}, True),
    ({"redirected": False, "contentType": "text/html", "status": 200}, True),
    ({"redirected": False, "contentType": "application/json", "status": 200}, False),
    ({"redirected": False, "contentType": "application/json", "status": 409}, False),
    ({"redirected": False, "contentType": "", "status": 204}, False),
    ({"redirected": False, "contentType": "text/html", "status": 502}, False),
])
def test_a_sign_in_page_in_place_of_data_means_signed_out(answer, signed_out):
    assert run_rules(f"R.isSignedOutAnswer({json.dumps(answer)})") is signed_out
