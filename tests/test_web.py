import pytest

from conftest import OWNER


def new_event(owner, **overrides):
    calendar = owner.get("/api/calendars").get_json()["calendars"][0]["id"]
    body = {"calendarId": calendar, "title": "Dinner", "start": "2026-11-20T19:00", "end": "2026-11-20T21:00"}
    body.update(overrides)
    answer = owner.post("/api/events", json=body)
    assert answer.status_code == 201, answer.get_json()
    return answer.get_json()


def events(owner, first="2026-11-01", last="2026-12-01"):
    answer = owner.get(f"/api/events?from={first}&to={last}")
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()["events"]


def test_health_is_public_and_checks_the_store(client):
    answer = client.get("/api/health")
    assert answer.status_code == 200 and answer.get_json() == {"ok": True}


@pytest.mark.parametrize("path", ["/", "/api/session", "/api/events?from=2026-11-01&to=2026-11-02", "/api/app-passwords",
                                  "/api/export.ics", "/api/reminders"])
def test_everything_else_needs_the_proxy_user(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Forwarded-User": "someone-else"}).status_code == 401
    assert client.get(path, headers={"X-Forwarded-User": OWNER}, environ_base={"REMOTE_ADDR": "10.0.0.50"}).status_code == 401


def test_writes_need_the_csrf_token_and_same_origin(client, owner):
    calendar = owner.get("/api/calendars").get_json()["calendars"][0]["id"]
    body = {"calendarId": calendar, "title": "x", "start": "2026-11-20T19:00", "end": "2026-11-20T20:00"}
    assert client.post("/api/events", json=body, headers={"X-Forwarded-User": OWNER}).status_code == 403
    assert owner.post("/api/events", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert owner.post("/api/app-passwords", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_security_headers(owner):
    answer = owner.get("/")
    assert answer.status_code == 200
    assert "default-src 'self'" in answer.headers["Content-Security-Policy"]
    assert answer.headers["X-Frame-Options"] == "DENY"
    assert owner.get("/api/session").headers["Cache-Control"] == "no-store"


def test_session_describes_caldav_and_arty(owner):
    session = owner.get("/api/session").get_json()
    assert session["timeZone"] == "Europe/London"
    assert session["caldav"] == {"enabled": True, "url": "http://localhost:8110/dav/", "user": "owner"}
    assert session["arty"]["configured"] is False


def test_create_edit_and_delete_through_the_api(owner):
    created = new_event(owner, repeat={"freq": "weekly", "count": 2}, reminders=[15])
    path = f"/api/calendars/{created['calendarId']}/events/{created['itemId']}"
    second = events(owner)[1]
    assert owner.patch(f"{path}?scope=one&occurrence={second['occurrence']}", json={"title": "Late dinner"}).status_code == 200
    assert [event["title"] for event in events(owner)] == ["Dinner", "Late dinner"]
    one = owner.get(f"{path}?occurrence={second['occurrence']}").get_json()
    assert one["title"] == "Late dinner" and one["changedInstance"] is True
    assert owner.delete(f"{path}?scope=one&occurrence={second['occurrence']}").status_code == 204
    assert [event["title"] for event in events(owner)] == ["Dinner"]
    assert owner.delete(path).status_code == 204
    assert events(owner) == []


@pytest.mark.parametrize("path", ["/api/calendars/..%2Fother/events/x.ics", "/api/calendars/nope/events/x.ics",
                                  "/api/calendars/%2E%2E/events/x.ics"])
def test_odd_paths_are_not_found(owner, path):
    assert owner.get(path).status_code == 404


def test_bad_event_input_is_a_400_with_a_reason(owner):
    calendar = owner.get("/api/calendars").get_json()["calendars"][0]["id"]
    answer = owner.post("/api/events", json={"calendarId": calendar, "title": "x", "start": "2027-03-28T01:30", "end": "2027-03-28T02:00"})
    assert answer.status_code == 400 and "clocks go forward" in answer.get_json()["error"]
    assert owner.post("/api/events", json=["not", "an", "object"]).status_code == 400
    assert owner.get("/api/events?from=2026-11-01&to=2028-11-01").status_code == 400
    assert owner.get("/api/events?from=yesterday&to=2026-11-02").status_code == 400


def test_quick_add_preview_and_create(owner):
    preview = owner.post("/api/parse", json={"text": "dinner with mum friday 7pm"}).get_json()
    assert (preview["title"], preview["start"], preview["end"]) == ("dinner with mum", "2026-11-13T19:00", "2026-11-13T20:00")
    assert owner.post("/api/quick", json={"text": "dinner with mum friday 7pm"}).status_code == 201
    assert [event["title"] for event in events(owner)] == ["dinner with mum"]
    work = owner.post("/api/calendars", json={"name": "Work", "color": "#5b8def"}).get_json()
    assert owner.post("/api/quick", json={"text": "standup tomorrow 9am #work"}).get_json()["calendarId"] == work["id"]
    assert owner.post("/api/quick", json={"text": "x tomorrow #nope"}).status_code == 400


def test_calendars_api(owner):
    assert [calendar["name"] for calendar in owner.get("/api/calendars").get_json()["calendars"]] == ["Personal"]
    made = owner.post("/api/calendars", json={"name": "Work", "color": "#5b8def"})
    assert made.status_code == 201
    work = made.get_json()["id"]
    assert owner.patch(f"/api/calendars/{work}", json={"name": "Office"}).get_json()["name"] == "Office"
    assert owner.post("/api/calendars", json={"name": "<b>", "color": "javascript:alert(1)"}).status_code == 400
    assert owner.delete(f"/api/calendars/{work}").status_code == 204
    [only] = [calendar["id"] for calendar in owner.get("/api/calendars").get_json()["calendars"]]
    assert owner.delete(f"/api/calendars/{only}").status_code == 409


def test_app_passwords_are_shown_once_and_revocable(owner, passwords):
    made = owner.post("/api/app-passwords", json={"name": "Test phone"})
    assert made.status_code == 201
    secret = made.get_json()["password"]
    listed = owner.get("/api/app-passwords").get_json()["passwords"]
    assert listed == [{"id": made.get_json()["id"], "name": "Test phone", "createdAt": listed[0]["createdAt"],
                       "lastUsedAt": None, "lastUsedFrom": None}]
    assert secret not in owner.get("/api/app-passwords").get_data(as_text=True)
    assert passwords.check(secret, "1.2.3.4")
    assert owner.delete(f"/api/app-passwords/{made.get_json()['id']}").status_code == 204
    assert not passwords.check(secret, "1.2.3.4")
    assert owner.delete(f"/api/app-passwords/{made.get_json()['id']}").status_code == 404
    assert owner.delete("/api/app-passwords/abc").status_code == 404


def test_export_is_an_attachment(owner):
    new_event(owner, title="Exported")
    answer = owner.get("/api/export.ics")
    assert answer.status_code == 200
    assert answer.headers["Content-Disposition"].startswith("attachment;")
    assert answer.mimetype == "text/calendar"
    assert "SUMMARY:Exported" in answer.get_data(as_text=True)


def test_arty_link_without_an_address_says_so(owner):
    assert owner.post("/api/arty/link").status_code == 409
    assert owner.post("/api/arty/test").get_json() == {"result": "not linked"}


def test_a_save_or_delete_from_a_stale_page_is_refused(owner):
    made = new_event(owner)
    [seen] = events(owner)
    path = f"/api/calendars/{made['calendarId']}/events/{made['itemId']}"
    assert owner.patch(path, json={"location": "from the phone"}, headers={"If-Match": seen["etag"]}).status_code == 200
    stale = owner.patch(path, json={"title": "stale"}, headers={"If-Match": seen["etag"]})
    assert stale.status_code == 409 and "changed somewhere else" in stale.get_json()["error"]
    assert owner.delete(path, headers={"If-Match": seen["etag"]}).status_code == 409
    [now] = events(owner)
    assert (now["title"], now["location"]) == ("Dinner", "from the phone")
    assert owner.delete(path, headers={"If-Match": now["etag"]}).status_code == 204


def test_quick_add_preview_is_posted_so_the_text_stays_out_of_urls(owner):
    assert owner.get("/api/parse?text=dinner friday 7pm").status_code == 405
    preview = owner.post("/api/parse", json={"text": "dinner friday 7pm"})
    assert preview.status_code == 200 and preview.get_json()["title"] == "dinner"
