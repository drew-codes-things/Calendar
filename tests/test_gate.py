import base64

import pytest
from werkzeug.test import Client

from calendarapp.gate import FAILURES_BEFORE_STRICT, FAILURES_PER_ADDRESS, FailureLimiter
from conftest import CALDAV_USER, FakeClock

HTTPS = {"Cf-Visitor": '{"scheme":"https"}'}
EVENT = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//phone//EN\r\nBEGIN:VEVENT\r\nUID:from-phone\r\nDTSTAMP:20261113T000000Z\r\n"
         "DTSTART;TZID=Europe/London:20261120T190000\r\nDTEND;TZID=Europe/London:20261120T200000\r\nSUMMARY:Made on the phone\r\n"
         "END:VEVENT\r\nEND:VCALENDAR\r\n")
PROPFIND_PRINCIPAL = ('<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                      "<D:prop><D:current-user-principal/><C:calendar-home-set/></D:prop></D:propfind>")


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def call(gate, method, path, password=None, user=CALDAV_USER, address="127.0.0.1", headers=None, **kwargs):
    sent = {**HTTPS, **(headers or {})}
    if password is not None:
        sent["Authorization"] = basic(user, password)
    return Client(gate).open(path, method=method, headers=sent, environ_base={"REMOTE_ADDR": address}, **kwargs)


@pytest.fixture
def secret(passwords):
    return passwords.create("Test phone")["password"]


def test_switched_off_answers_nothing_even_with_a_good_password(gate, secret):
    gate.enabled = False
    for path in ("/dav/", "/.well-known/caldav", "/dav/owner/"):
        answer = call(gate, "PROPFIND", path, secret)
        assert answer.status_code == 404
        assert b"switched off" in answer.data


@pytest.mark.parametrize("visitor", [None, '{"scheme":"http"}', "not json", '["https"]'])
def test_only_https_through_cloudflare(gate, secret, visitor):
    headers = {"Cf-Visitor": visitor} if visitor is not None else {}
    answer = Client(gate).open("/dav/", method="PROPFIND", headers={**headers, "Authorization": basic(CALDAV_USER, secret)})
    assert answer.status_code == 403
    assert "WWW-Authenticate" not in answer.headers


def test_well_known_points_at_the_dav_root(gate):
    answer = call(gate, "PROPFIND", "/.well-known/caldav")
    assert answer.status_code == 301 and answer.headers["Location"] == "/dav/"


def test_no_password_gets_a_basic_challenge(gate):
    answer = call(gate, "PROPFIND", "/dav/")
    assert answer.status_code == 401
    assert answer.headers["WWW-Authenticate"].startswith('Basic realm="Calendar"')


@pytest.mark.parametrize("user, password", [
    (CALDAV_USER, "wrong"), ("someone", None), (CALDAV_USER, ""), (CALDAV_USER, "x" * 5000), ("", "")])
def test_wrong_credentials_are_refused(gate, secret, user, password):
    answer = call(gate, "PROPFIND", "/dav/", secret if password is None else password, user=user)
    assert answer.status_code == 401


def test_a_revoked_password_stops_working(gate, passwords, secret):
    assert call(gate, "PROPFIND", "/dav/owner/", secret, headers={"Depth": "0"}).status_code == 207
    passwords.revoke(passwords.active()[0]["id"])
    assert call(gate, "PROPFIND", "/dav/owner/", secret, headers={"Depth": "0"}).status_code == 401


def test_starfire_cookies_and_identity_headers_do_not_sign_in(gate):
    headers = {"Cookie": "starfire_session=anything", "X-Forwarded-User": "admin", "X-Remote-User": CALDAV_USER}
    assert call(gate, "PROPFIND", "/dav/owner/", headers=headers).status_code == 401


def test_repeated_failures_from_one_address_are_blocked(gate, secret):
    for _ in range(FAILURES_PER_ADDRESS):
        assert call(gate, "PROPFIND", "/dav/", "wrong", address="203.0.113.9").status_code == 401
    blocked = call(gate, "PROPFIND", "/dav/", secret, address="203.0.113.9")
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert call(gate, "PROPFIND", "/dav/owner/", secret, address="198.51.100.4", headers={"Depth": "0"}).status_code == 207


def test_the_cloudflare_address_is_used_only_from_the_proxy(gate, secret):
    for _ in range(FAILURES_PER_ADDRESS):
        call(gate, "PROPFIND", "/dav/", "wrong", headers={"Cf-Connecting-Ip": "203.0.113.7"})
    assert call(gate, "PROPFIND", "/dav/", secret, headers={"Cf-Connecting-Ip": "203.0.113.7"}).status_code == 429
    assert call(gate, "PROPFIND", "/dav/owner/", secret, headers={"Cf-Connecting-Ip": "203.0.113.8", "Depth": "0"}).status_code == 207
    for _ in range(FAILURES_PER_ADDRESS):
        call(gate, "PROPFIND", "/dav/", "wrong", address="10.0.0.77", headers={"Cf-Connecting-Ip": "203.0.113.20"})
    assert call(gate, "PROPFIND", "/dav/", secret, address="10.0.0.77", headers={"Cf-Connecting-Ip": "203.0.113.21"}).status_code == 429


def test_many_failures_overall_make_every_failing_address_wait():
    clock = FakeClock(0)
    limiter = FailureLimiter(clock=clock)
    for number in range(FAILURES_BEFORE_STRICT):
        limiter.failed(f"10.0.{number // 250}.{number % 250}")
    limiter.failed("203.0.113.50")
    assert limiter.seconds_blocked("203.0.113.50") > 0
    assert limiter.seconds_blocked("203.0.113.51") == 0
    clock.advance(16 * 60)
    assert limiter.seconds_blocked("203.0.113.50") == 0


def test_blocks_end_after_the_window():
    clock = FakeClock(0)
    limiter = FailureLimiter(clock=clock)
    for _ in range(FAILURES_PER_ADDRESS):
        limiter.failed("203.0.113.9")
    assert limiter.seconds_blocked("203.0.113.9") > 0
    clock.advance(15 * 60 + 1)
    assert limiter.seconds_blocked("203.0.113.9") == 0


def test_discovery_and_sync_work_with_a_good_password(gate, secret, book):
    principal = call(gate, "PROPFIND", "/dav/", secret, headers={"Depth": "0"}, data=PROPFIND_PRINCIPAL)
    assert principal.status_code == 207
    assert b"<href>/dav/owner/</href>" in principal.data
    home = book.calendar_list()[0]["id"]
    put = call(gate, "PUT", f"/dav/owner/{home}/from-phone.ics", secret, headers={"Content-Type": "text/calendar", "If-None-Match": "*"},
               data=EVENT)
    assert put.status_code == 201
    from datetime import date
    found, _ = book.occurrences(date(2026, 11, 20), date(2026, 11, 21))
    assert [str(occurrence.instance.get("SUMMARY")) for occurrence in found] == ["Made on the phone"]
    fetched = call(gate, "GET", f"/dav/owner/{home}/from-phone.ics", secret)
    assert fetched.status_code == 200 and b"Made on the phone" in fetched.data
    assert fetched.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", [
    "/dav/other/", "/dav/ownerx/", "/dav/Owner/"])
def test_other_users_are_off_limits(gate, secret, path):
    assert call(gate, "PROPFIND", path, secret, headers={"Depth": "0"}).status_code == 403


@pytest.mark.parametrize("path", [
    "/dav/../api/session", "/dav/%2e%2e/api/session", "/dav/owner/%2e%2e/other/", "/dav/owner/..%2f..%2fapi/session",
    "/dav//owner/", "/dav/owner/%5c..%5cother", "/api/session", "/", "/davx/", "/dav%2fowner/", "/static/app.js",
    "/dav/owner/%00/"])
def test_path_tricks_reach_nothing(gate, secret, path):
    answer = call(gate, "GET", path, secret)
    assert answer.status_code == 404


def test_move_cannot_leave_your_calendars(gate, secret, book):
    home = book.calendar_list()[0]["id"]
    call(gate, "PUT", f"/dav/owner/{home}/a.ics", secret, headers={"Content-Type": "text/calendar"}, data=EVENT)
    for destination in ("/dav/other/x/a.ics", "https://evil.example/dav/owner/x.ics", "/dav/owner/../other/a.ics", "/api/x"):
        answer = call(gate, "MOVE", f"/dav/owner/{home}/a.ics", secret, headers={"Destination": destination})
        assert answer.status_code == 403, destination


def test_unknown_methods_and_huge_bodies_are_refused(gate, secret):
    assert call(gate, "TRACE", "/dav/owner/", secret).status_code == 405
    assert call(gate, "PUT", "/dav/owner/x/big.ics", secret, data=b"x" * (11 * 1024 * 1024)).status_code == 413


def test_failures_from_one_ipv6_network_count_together(gate, secret):
    for number in range(1, FAILURES_PER_ADDRESS + 1):
        call(gate, "PROPFIND", "/dav/", "wrong", headers={"Cf-Connecting-Ip": f"2001:db8:1:2::{number}"})
    assert call(gate, "PROPFIND", "/dav/", secret, headers={"Cf-Connecting-Ip": "2001:db8:1:2:ffff::9"}).status_code == 429
    assert call(gate, "PROPFIND", "/dav/owner/", secret, headers={"Cf-Connecting-Ip": "2001:db8:1:3::1", "Depth": "0"}).status_code == 207


def test_a_deeply_nested_cf_visitor_is_simply_not_https(gate, secret):
    deep = "[" * 100000 + "]" * 100000
    assert call(gate, "PROPFIND", "/dav/owner/", secret, headers={"Cf-Visitor": deep, "Depth": "0"}).status_code == 403
