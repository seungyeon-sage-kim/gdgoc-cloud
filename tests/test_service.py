"""Tests that run in CI before every deploy (pytest)."""

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shared import config

PUBLISHED: list = []  # (topic, data) for every message published during a test


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WORKER_DELAY_MS", "0")
    monkeypatch.setenv("TICKET_SECRET", "test-secret")
    monkeypatch.setenv("SIGNUPS_PER_MINUTE_PER_IP", "1000")  # tests all come from one address
    monkeypatch.setenv("ADMIN_EMAILS", "organizer@example.com")
    monkeypatch.setenv("STAFF_EMAILS", "door@example.com")
    monkeypatch.setenv("MAIL_BACKEND", "outbox")
    config.get_settings.cache_clear()
    from shared import guard

    guard._hits.clear()
    from shared import bus, store

    # No background HTTP delivery in tests; we call the worker and the promoter
    # directly. Published messages are kept so tests can read them (sign-in codes).
    PUBLISHED.clear()
    monkeypatch.setattr(bus, "publish", lambda topic, data: PUBLISHED.append((topic, data)))
    monkeypatch.setattr(store, "_document_updated", lambda *args: None)
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def web():
    from shared import demo, store
    from web.main import app

    with TestClient(app) as client:
        store.create_event(demo.make_event("test"))  # 30 seats, open since yesterday, starts in a week
        yield client


def set_event(**changes):
    """Change the test event, e.g. set_event(seat_limit=2)."""
    from shared import store

    store.update_event(replace(store.get_event("test"), **changes))


def status(web, event_id="test"):
    return web.get(f"/api/events/{event_id}/status").json()


def sign_in(web, email):
    """The real flow: ask for a code, read it from the email message, type it in."""
    assert web.post("/api/auth/code", json={"email": email}).status_code == 200
    code = [data["code"] for topic, data in PUBLISHED if data.get("kind") == "login_code" and data["to"] == email][-1]
    assert web.post("/api/auth/verify", json={"email": email, "code": code}).status_code == 200


@pytest.fixture
def worker():
    from ticket_worker.main import app

    with TestClient(app) as client:
        yield client


def signup(web, n, event_id="test"):
    return web.post(f"/api/events/{event_id}/registrations", json={"name": f"학생{n}", "email": f"s{n}@yonsei.ac.kr"})


def push(worker, registration_id):
    envelope = {
        "message": {
            "data": base64.b64encode(json.dumps({"registration_id": registration_id}).encode()).decode(),
            "messageId": "1",
        }
    }
    return worker.post("/", json=envelope)


def test_never_oversells_under_concurrency(web, monkeypatch):
    # 60 people press the button at the same moment for 30 seats and 10 waitlist places
    set_event(waitlist_limit=10)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda n: signup(web, n), range(60)))
    statuses = [r.json().get("status") if r.status_code == 201 else r.status_code for r in results]
    assert statuses.count("pending") == 30
    assert statuses.count("waitlisted") == 10
    assert statuses.count(409) == 20
    now = status(web)
    assert (now["seats_left"], now["waitlisted"], now["state"]) == (0, 10, "full")


def test_one_seat_per_email(web):
    assert signup(web, 1).status_code == 201
    second = signup(web, 1)
    assert second.status_code == 409 and second.json()["error"] == "duplicate"


def test_not_open_yet(web):
    set_event(opens_at=(config.now() + timedelta(hours=1)).isoformat())
    response = signup(web, 1)
    assert response.status_code == 403 and response.json()["error"] == "not_open"


def test_worker_is_idempotent(web, worker):
    reg_id = signup(web, 1).json()["id"]
    assert push(worker, reg_id).status_code == 204
    assert web.get(f"/api/registrations/{reg_id}").json()["status"] == "issued"
    # Pub/Sub may deliver the same message again: still fine, nothing changes
    assert push(worker, reg_id).status_code == 204
    assert web.get(f"/t/{reg_id}/qr.svg").status_code == 200


def test_cancel_frees_the_seat(web):
    reg_id = signup(web, 1).json()["id"]
    assert status(web)["seats_left"] == 29
    assert web.post(f"/api/registrations/{reg_id}/cancel").status_code == 200
    assert status(web)["seats_left"] == 30


def test_checkin_once_and_rejects_forged_tokens(web, worker):
    from shared import tokens

    reg_id = signup(web, 1).json()["id"]
    push(worker, reg_id)
    sign_in(web, "door@example.com")
    token = tokens.ticket_token("test", reg_id)

    def scan(value):
        return web.post("/api/checkin", json={"event_id": "test", "value": value}).json()["result"]

    assert scan(token) == "ok"
    assert scan(token) == "already"

    forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert scan(forged) == "invalid"


def test_failed_delivery_retries_then_dead_letters(monkeypatch):
    import httpx

    from shared import bus, store

    store.init()
    calls = []
    monkeypatch.setattr(bus.time, "sleep", lambda seconds: calls.append(seconds))
    monkeypatch.setattr(bus.httpx, "post", lambda *a, **k: httpx.Response(500))
    bus.deliver_with_retries(
        "http://worker",
        {},
        {},
        "test",
        lambda attempts, error: store.add_dead_letter("registrations", "{}", attempts, error),
    )
    assert calls == [1.0, 2.0, 4.0, 8.0]  # waits double between attempts
    assert store.list_dead_letters()[0]["error"] == "HTTP 500"


def test_checkin_needs_a_staff_account(web):
    assert web.post("/api/checkin", json={"event_id": "test", "value": "ABC-DEF"}).status_code == 401
    sign_in(web, "someone@example.com")  # signed in, but not on the staff list
    assert web.post("/api/checkin", json={"event_id": "test", "value": "ABC-DEF"}).status_code == 401


def test_signup_survives_a_failed_publish(web, monkeypatch):
    from shared import bus

    def broken(topic, data):
        raise RuntimeError("Pub/Sub unavailable")

    monkeypatch.setattr(bus, "publish", broken)
    response = signup(web, 1)
    # The seat is saved, so the person still gets their ticket page
    assert response.status_code == 201
    assert web.get(f"/api/registrations/{response.json()['id']}").json()["status"] == "pending"


def test_the_default_limit_lets_a_campus_on_one_address_sign_up(monkeypatch):
    """Everyone on campus Wi-Fi can arrive as one address when signups open."""
    from shared import guard

    monkeypatch.delenv("SIGNUPS_PER_MINUTE_PER_IP")
    config.get_settings.cache_clear()
    assert all(guard.allow("203.0.113.9") for _ in range(100))
    assert not guard.allow("203.0.113.9")


def test_rate_limit_per_address(web, monkeypatch):
    monkeypatch.setenv("SIGNUPS_PER_MINUTE_PER_IP", "3")
    config.get_settings.cache_clear()
    codes = [signup(web, n).status_code for n in range(5)]
    assert codes == [201, 201, 201, 429, 429]


def test_bot_check_requires_a_token_when_configured(web, monkeypatch):
    monkeypatch.setenv("RECAPTCHA_SITE_KEY", "test-site-key")
    config.get_settings.cache_clear()
    response = signup(web, 1)  # no recaptcha_token in the body
    assert response.status_code == 403 and response.json()["error"] == "bot_suspected"


# ---------- waitlist and attendance confirmation ----------


@pytest.fixture
def promoter():
    from waitlist_promoter.main import app

    with TestClient(app) as client:
        yield client


def firestore_update(promoter, registration_id, before, after):
    """The protobuf event Eventarc delivers when Firestore updates a registration."""
    from google.events.cloud.firestore_v1 import Document, DocumentEventData, Value

    def document(status):
        return Document(name=f"registrations/{registration_id}", fields={"status": Value(string_value=status)})

    body = DocumentEventData.serialize(DocumentEventData(old_value=document(before), value=document(after)))
    return promoter.post("/", content=body, headers={"content-type": "application/protobuf"})


def test_full_event_takes_a_waitlist(web):
    set_event(seat_limit=2)
    signup(web, 1), signup(web, 2)
    third = signup(web, 3).json()
    assert third["status"] == "waitlisted"
    assert status(web)["state"] == "waitlist"
    assert web.get(f"/api/registrations/{third['id']}").json()["waitlist_position"] == 1
    assert web.get(f"/api/registrations/{signup(web, 4).json()['id']}").json()["waitlist_position"] == 2


def test_cancellation_promotes_the_first_in_line_once(web, promoter, monkeypatch):
    from shared import bus

    set_event(seat_limit=1)
    sent = []
    monkeypatch.setattr(bus, "publish", lambda topic, data: topic == "registrations" and sent.append(data))
    holder, first, second = (signup(web, n).json()["id"] for n in range(3))
    sent.clear()

    web.post(f"/api/registrations/{holder}/cancel")
    # Eventarc delivers at least once: the same event twice promotes only one person
    for _ in range(2):
        assert firestore_update(promoter, holder, "issued", "cancelled").status_code == 204
    assert web.get(f"/api/registrations/{first}").json()["status"] == "pending"
    assert web.get(f"/api/registrations/{second}").json()["status"] == "waitlisted"
    assert sent == [{"registration_id": first}]  # ticket-worker issues their QR


def test_promoter_ignores_changes_that_free_no_seat(web, promoter, monkeypatch):
    set_event(seat_limit=1)
    holder, waiting = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    assert firestore_update(promoter, holder, "pending", "issued").status_code == 204
    assert firestore_update(promoter, waiting, "waitlisted", "cancelled").status_code == 204
    assert web.get(f"/api/registrations/{waiting}").json()["status"] == "waitlisted"


def registrations_published():
    return [data["registration_id"] for topic, data in PUBLISHED if topic == "registrations"]


def pub_sub_down(topic, data):
    raise RuntimeError("Pub/Sub is down")


def test_a_new_signup_goes_behind_people_already_waiting(web):
    set_event(seat_limit=1)
    holder, waiting = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    web.post(f"/api/registrations/{holder}/cancel")  # the seat is free; waitlist-promoter has not run yet

    assert (status(web)["state"], status(web)["seats_left"]) == ("waitlist", 0)
    late = signup(web, 3).json()
    assert late["status"] == "waitlisted"
    assert web.get(f"/api/registrations/{waiting}").json()["waitlist_position"] == 1
    assert web.get(f"/api/registrations/{late['id']}").json()["waitlist_position"] == 2


def test_a_full_waitlist_refuses_a_signup_even_with_a_seat_free(web):
    set_event(seat_limit=1, waitlist_limit=1)
    holder = signup(web, 1).json()["id"]
    signup(web, 2)
    web.post(f"/api/registrations/{holder}/cancel")
    assert status(web)["state"] == "full"
    assert signup(web, 3).status_code == 409


def event_form(**changes):
    return {
        "title": "테스트 행사",
        "summary": "요약",
        "description": "소개",
        "venue": "공학원",
        "starts_at": "2030-03-10T19:00",
        "ends_at": "2030-03-10T21:00",
        "opens_at": "2030-03-01T12:00",
        "closes_at": "2030-03-10T19:00",
        "confirm_deadline": "2030-03-09T18:00",
        "seat_limit": "1",
        "waitlist_limit": "10",
        "published": "on",
    } | changes


def test_raising_the_seat_limit_gives_the_new_seats_to_people_waiting(web):
    set_event(seat_limit=1)
    signup(web, 0)
    first, second, third = (signup(web, n).json()["id"] for n in (1, 2, 3))
    PUBLISHED.clear()
    sign_in(web, "organizer@example.com")

    saved = web.post("/admin/events/test/edit", data=event_form(seat_limit="3"), follow_redirects=False)
    assert saved.status_code == 303
    assert [web.get(f"/api/registrations/{r}").json()["status"] for r in (first, second, third)] == [
        "pending",
        "pending",
        "waitlisted",
    ]
    assert registrations_published() == [first, second]


def test_saving_the_seat_limit_survives_a_failed_publish(web, monkeypatch):
    from shared import bus

    set_event(seat_limit=1)
    signup(web, 0)
    waiting = signup(web, 1).json()["id"]
    sign_in(web, "organizer@example.com")
    monkeypatch.setattr(bus, "publish", pub_sub_down)

    saved = web.post("/admin/events/test/edit", data=event_form(seat_limit="2"), follow_redirects=False)
    assert saved.status_code == 303  # the housekeeping job publishes it later
    assert web.get(f"/api/registrations/{waiting}").json()["status"] == "pending"


def test_a_promoted_seat_survives_a_failed_publish(web, worker, promoter, monkeypatch):
    """The seat is saved, then Pub/Sub fails. Eventarc's retry finds no free seat, so only the job can still send it."""
    from jobs import housekeeping
    from shared import bus, store

    set_event(seat_limit=1)
    holder, waiting = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    web.post(f"/api/registrations/{holder}/cancel")
    with monkeypatch.context() as outage:
        outage.setattr(bus, "publish", pub_sub_down)
        with pytest.raises(RuntimeError):  # a 5xx on Cloud Run: Eventarc retries
            firestore_update(promoter, holder, "issued", "cancelled")
    assert store.get(waiting).status == "pending"

    PUBLISHED.clear()
    assert firestore_update(promoter, holder, "issued", "cancelled").status_code == 204
    housekeeping.main()  # just promoted: the message may still be on its way
    assert registrations_published() == []

    later = config.now() + housekeeping.STUCK_AFTER + timedelta(seconds=1)
    monkeypatch.setattr(housekeeping, "now", lambda: later)
    housekeeping.main()
    assert registrations_published() == [waiting]

    assert push(worker, waiting).status_code == 204
    assert store.get(waiting).status == "issued"
    assert ("promoted", "s2@yonsei.ac.kr") in emails_requested()  # 자리 배정, not 신청 완료
    housekeeping.main()  # issued: nothing left to publish
    assert registrations_published() == [waiting]


def test_a_signup_that_was_never_announced_gets_its_ticket(web, monkeypatch):
    from jobs import housekeeping
    from shared import bus

    with monkeypatch.context() as outage:
        outage.setattr(bus, "publish", pub_sub_down)
        registration_id = signup(web, 1).json()["id"]  # the person closes the page
    later = config.now() + housekeeping.STUCK_AFTER + timedelta(seconds=1)
    monkeypatch.setattr(housekeeping, "now", lambda: later)
    housekeeping.main()
    assert registrations_published() == [registration_id]


def test_a_failed_email_request_brings_the_ticket_message_back(web, worker, monkeypatch):
    """Marked issued first, the retry would skip the ticket and nobody would be told."""
    from shared import bus, store

    registration_id = signup(web, 1).json()["id"]
    with monkeypatch.context() as outage:
        outage.setattr(bus, "publish", pub_sub_down)
        assert push(worker, registration_id).status_code == 500
    assert store.get(registration_id).status == "pending"

    assert push(worker, registration_id).status_code == 204
    assert store.get(registration_id).status == "issued"
    assert emails_requested().count(("ticket_issued", "s1@yonsei.ac.kr")) == 1


def test_the_job_gives_a_free_seat_to_the_next_in_line(web, capsys):
    """Eventarc delivers almost always. If a delivery never arrives, the seat must not stay empty."""
    from jobs import housekeeping

    set_event(seat_limit=1)
    holder, first, second = (signup(web, n).json()["id"] for n in range(3))
    web.post(f"/api/registrations/{holder}/cancel")  # no Eventarc event in this test
    PUBLISHED.clear()

    housekeeping.main()
    assert web.get(f"/api/registrations/{first}").json()["status"] == "pending"
    assert web.get(f"/api/registrations/{second}").json()["status"] == "waitlisted"
    assert registrations_published() == [first]
    assert "gave 1 free seats" in capsys.readouterr().out
    housekeeping.main()  # every 5 minutes: nothing more to do
    assert registrations_published() == [first]


def test_the_job_leaves_the_waitlist_alone_once_the_event_started(web):
    from jobs import housekeeping

    set_event(seat_limit=1)
    holder, waiting = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    web.post(f"/api/registrations/{holder}/cancel")
    set_event(starts_at=(config.now() - timedelta(minutes=1)).isoformat())
    housekeeping.main()
    assert web.get(f"/api/registrations/{waiting}").json()["status"] == "waitlisted"


def test_a_failed_publish_in_the_job_leaves_the_other_events_alone(web, monkeypatch):
    from jobs import housekeeping
    from shared import bus, demo, store

    store.create_event(demo.make_event("other"))
    set_event(seat_limit=1)
    store.update_event(replace(store.get_event("other"), seat_limit=1))
    waiting = []
    for event_id in ("test", "other"):
        holder = signup(web, 1, event_id).json()["id"]
        waiting.append(signup(web, 2, event_id).json()["id"])
        web.post(f"/api/registrations/{holder}/cancel")
    PUBLISHED.clear()

    failures = [RuntimeError("Pub/Sub is down")]

    def fails_once(topic, data):
        if failures:
            raise failures.pop()
        PUBLISHED.append((topic, data))

    monkeypatch.setattr(bus, "publish", fails_once)
    assert housekeeping.main() == 1  # the run reports the trouble, and still gets to the other event
    assert [store.get(r).status for r in waiting] == ["pending", "pending"]  # both seats saved
    assert len(registrations_published()) == 1  # one message went out; the next run sends the other


def test_unconfirmed_seats_are_released_at_the_deadline(web, monkeypatch, capsys):
    from jobs import housekeeping

    confirmed, unconfirmed = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    assert web.post(f"/api/registrations/{confirmed}/confirm").json()["confirmed"] is True

    set_event(confirm_deadline=(config.now() + timedelta(hours=5)).isoformat())
    housekeeping.main()  # before the deadline: only a reminder
    assert web.get(f"/api/registrations/{unconfirmed}").json()["status"] == "pending"

    set_event(confirm_deadline=(config.now() - timedelta(minutes=1)).isoformat())
    assert web.post(f"/api/registrations/{unconfirmed}/confirm").status_code == 409  # too late
    housekeeping.main()
    assert web.get(f"/api/registrations/{unconfirmed}").json()["status"] == "cancelled"
    assert web.get(f"/api/registrations/{confirmed}").json()["status"] == "pending"
    assert "released 1" in capsys.readouterr().out


def test_the_deploy_check_changes_nothing(web, monkeypatch, capsys):
    """A deploy tries the job's new image with --check: a seat a real run would release stays."""
    from jobs import housekeeping
    from shared import store

    registration_id = signup(web, 1).json()["id"]
    set_event(confirm_deadline=(config.now() - timedelta(minutes=1)).isoformat())
    store.set_meta("reminded:test", config.now().isoformat())  # asked, never confirmed: due for release

    monkeypatch.setattr("sys.argv", ["housekeeping", "--check"])
    assert housekeeping.main() == 0
    assert web.get(f"/api/registrations/{registration_id}").json()["status"] == "pending"
    assert "changed nothing" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["housekeeping"])
    housekeeping.main()  # the Scheduler's run does the work
    assert web.get(f"/api/registrations/{registration_id}").json()["status"] == "cancelled"


def test_nobody_loses_a_seat_without_having_been_asked(web, capsys):
    """The job was down for the whole reminder window: a cancelled ticket cannot be restored."""
    from jobs import housekeeping

    registration_id = signup(web, 1).json()["id"]
    set_event(confirm_deadline=(config.now() - timedelta(minutes=1)).isoformat())
    housekeeping.main()
    assert web.get(f"/api/registrations/{registration_id}").json()["status"] == "pending"
    assert "released" not in capsys.readouterr().out


def test_with_email_off_nobody_is_asked_and_nobody_loses_a_seat(web, monkeypatch):
    from jobs import housekeeping
    from shared import store

    registration_id = signup(web, 1).json()["id"]
    monkeypatch.setenv("MAIL_BACKEND", "log")
    config.get_settings.cache_clear()
    set_event(confirm_deadline=(config.now() + timedelta(hours=5)).isoformat())
    assert housekeeping.main() == 0
    assert store.get_meta("reminded:test") is None
    set_event(confirm_deadline=(config.now() - timedelta(minutes=1)).isoformat())
    housekeeping.main()
    assert store.get(registration_id).status == "pending"


def test_one_event_failing_leaves_the_later_events_alone(web, monkeypatch):
    from jobs import housekeeping
    from shared import demo, store

    store.create_event(demo.make_event("other"))
    seen = []
    real_release = housekeeping.release

    def fails_for_the_first_event(ev, force=False):
        seen.append(ev.id)
        if len(seen) == 1:
            raise RuntimeError("a database hiccup")
        return real_release(ev, force)

    monkeypatch.setattr(housekeeping, "release", fails_for_the_first_event)
    assert housekeeping.main() == 1
    assert len(seen) >= 2


def test_a_refused_read_of_the_minimum_fails_the_run(web, cloud_run, monkeypatch):
    from jobs import housekeeping

    def refused(url, timeout):
        def boom():
            raise RuntimeError("403")

        return SimpleNamespace(raise_for_status=boom, json=lambda: {})

    monkeypatch.setattr(cloud_run, "get", lambda self, url, timeout: refused(url, timeout))
    assert housekeeping.main() == 1


def test_a_failed_send_is_not_recorded_so_the_retry_sends_it(web, monkeypatch):
    from shared import mail, store

    registration_id = signup(web, 1).json()["id"]
    message = {"kind": "ticket_issued", "to": "s1@yonsei.ac.kr", "registration_id": registration_id}
    with monkeypatch.context() as outage:
        outage.setattr(mail, "_deliver", lambda message: pub_sub_down("", {}))
        with pytest.raises(RuntimeError):
            mail.send(message)
    assert not store.email_was_sent(f"ticket_issued:{registration_id}")
    assert mail.send(message) is True
    assert mail.send(message) is False  # the repeat Pub/Sub may deliver


def test_reserved_test_domains_are_recognised():
    from shared import mail

    for address in ("load-1@example.com", "a@EXAMPLE.org", "a@example.net.", "a@mail.example.com", "a@x.test",
                    "a@site.example", "a@nowhere.invalid", "a@localhost", "a@app.localhost"):  # fmt: skip
        assert mail.reserved_address(address), address
    for address in ("s1@yonsei.ac.kr", "a@example.co.kr", "a@notexample.com", "a@testing.com", "a@example.com.kr"):
        assert not mail.reserved_address(address), address


class FakeSMTP:
    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        pass

    def login(self, *args):
        pass

    def send_message(self, email):
        FakeSMTP.sent.append(email["To"])


def test_real_mail_never_goes_to_a_test_address(web, monkeypatch, caplog):
    """The load test signs up @example.com addresses; with mail on, none of them is sent to."""
    from shared import mail, store

    monkeypatch.setenv("MAIL_BACKEND", "smtp")
    monkeypatch.setenv("MAIL_FROM", "tickets@example.org")
    config.get_settings.cache_clear()
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    registration_id = web.post(
        "/api/events/test/registrations", json={"name": "부하 테스트 1", "email": "load-1@example.com"}
    ).json()["id"]
    message = {"kind": "ticket_issued", "to": "load-1@example.com", "registration_id": registration_id}
    with caplog.at_level("INFO", logger="mail"):
        assert mail.send(message) is True  # handled, so Pub/Sub does not retry it
        assert mail.send({"kind": "login_code", "to": "a@x.test", "code": "123456"}) is True
    assert FakeSMTP.sent == []
    assert "test address, not sent: ticket_issued to load-1@example.com" in caplog.text
    assert store.email_was_sent(f"ticket_issued:{registration_id}")
    mail.send({"kind": "login_code", "to": "s1@yonsei.ac.kr", "code": "123456"})
    assert FakeSMTP.sent == ["s1@yonsei.ac.kr"]


def test_a_reminder_that_failed_to_queue_is_tried_again(web, monkeypatch):
    from jobs import housekeeping
    from shared import bus, store

    signup(web, 1)
    set_event(confirm_deadline=(config.now() + timedelta(hours=5)).isoformat())
    with monkeypatch.context() as outage:
        outage.setattr(bus, "publish", pub_sub_down)
        assert housekeeping.main() == 1  # shows as a failed run, which is what the alert looks for
    assert store.get_meta("reminded:test") is None
    assert ("confirm_reminder", "s1@yonsei.ac.kr") not in emails_requested()

    housekeeping.remind(store.get_event("test"))
    assert store.get_meta("reminded:test")
    assert ("confirm_reminder", "s1@yonsei.ac.kr") in emails_requested()


def test_a_seat_given_just_before_the_deadline_counts_as_confirmed(web):
    from shared import store, waitlist

    set_event(seat_limit=1)
    holder, waiting = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    set_event(confirm_deadline=(config.now() + timedelta(hours=3)).isoformat())
    web.post(f"/api/registrations/{holder}/cancel")
    assert waitlist.fill_free_seats(store.get_event("test")) == [waiting]
    assert store.get(waiting).confirmed_at
    assert store.unconfirmed("test") == []  # the job leaves it alone

    set_event(seat_limit=2)
    assert signup(web, 3).json()["confirmed"] is True


def test_trace_travels_with_the_message():
    from shared import bus, runtime

    token = runtime.use_trace("projects/p/traces/abc")
    try:
        envelope = bus.push_envelope({"registration_id": "x"}, attributes={"trace": runtime.current_trace()})
    finally:
        runtime.reset_trace(token)
    # ticket-worker reads it back and logs under the same trace as the signup request
    assert bus.trace_of(envelope) == "projects/p/traces/abc"


def test_deadline_job_keeps_a_seat_confirmed_after_listing(web, monkeypatch):
    from shared import store

    reg_id = signup(web, 1).json()["id"]
    listed = store.unconfirmed("test")  # the job lists unconfirmed seats...
    web.post(f"/api/registrations/{reg_id}/confirm")  # ...and the person confirms just then
    assert [store.cancel(i, only_unconfirmed=True) for i in listed] == [False]
    assert web.get(f"/api/registrations/{reg_id}").json()["status"] == "pending"


def test_a_huge_email_is_refused_without_freezing_the_instance(web):
    huge = "a@" + "." * 500_000 + "@"
    started = time.monotonic()
    assert web.post("/api/auth/code", json={"email": huge}).status_code == 422
    assert web.post("/api/events/test/registrations", json={"name": "a", "email": huge}).status_code == 422
    assert time.monotonic() - started < 2


def test_sign_in_never_redirects_to_another_site():
    from web.auth import safe_next

    assert safe_next("/events/test?x=1") == "/events/test?x=1"
    for outside in (
        "https://evil.example",
        "//evil.example",
        "/\\evil.example",
        "/\t/evil.example",
        "/\n/evil.example",
    ):
        assert safe_next(outside) == "/me"


def test_a_non_ascii_token_is_refused_not_a_crash(web):
    from shared import tokens

    assert tokens.verify("페이로드.서명", "ticket") is None
    sign_in(web, "door@example.com")
    answer = web.post("/api/checkin/lookup", json={"event_id": "test", "value": "티켓.서명"})
    assert answer.status_code == 200 and answer.json()["result"] == "invalid"


def test_api_description_is_not_public(web):
    assert web.get("/openapi.json").status_code == 404


def test_rate_limit_uses_the_address_google_saw(web, monkeypatch):
    monkeypatch.setenv("SIGNUPS_PER_MINUTE_PER_IP", "1")
    config.get_settings.cache_clear()
    spoof = lambda fake: {"X-Forwarded-For": f"{fake}, 203.0.113.7"}  # noqa: E731
    url = "/api/events/test/registrations"
    first = web.post(url, json={"name": "a", "email": "a@x.dev"}, headers=spoof("1.1.1.1"))
    second = web.post(url, json={"name": "b", "email": "b@x.dev"}, headers=spoof("2.2.2.2"))
    assert (first.status_code, second.status_code) == (201, 429)


def test_csv_export_neutralises_formulas_and_pages_send_safety_headers(web):
    web.post("/api/events/test/registrations", json={"name": "=HYPERLINK(1)", "email": "f@x.dev"})
    sign_in(web, "organizer@example.com")
    csv_text = web.get("/admin/events/test/registrations.csv").text
    assert "'=HYPERLINK(1)" in csv_text
    headers = web.get("/").headers
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"


def test_promoter_reads_the_protobuf_event_eventarc_sends(web, promoter):
    set_event(seat_limit=1)
    holder, waiting = signup(web, 1).json()["id"], signup(web, 2).json()["id"]
    web.post(f"/api/registrations/{holder}/cancel")
    assert firestore_update(promoter, holder, "issued", "cancelled").status_code == 204
    assert web.get(f"/api/registrations/{waiting}").json()["status"] == "pending"


def test_calendar_file(web):
    # '캘린더에 추가' downloads one event that calendar apps import
    response = web.get("/events/test/calendar.ics")
    assert response.headers["content-type"].startswith("text/calendar")
    body = response.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.count("BEGIN:VEVENT") == 1
    assert "DTSTART:" in body and "DTEND:" in body and "SUMMARY:" in body


# ---------- many events ----------


def test_home_lists_published_events_and_hides_drafts(web):
    from shared import demo, store

    store.create_event(demo.make_event("draft-night", title="준비 중인 행사", published=False))
    store.create_event(demo.make_event("old-night", title="지난 행사 하나", starts_in=timedelta(days=-3)))
    upcoming, past = web.get("/").text, web.get("/?view=past").text
    assert "/events/test" in upcoming and "지난 행사 하나" in past and "지난 행사 하나" not in upcoming
    assert "준비 중인 행사" not in upcoming + past
    # A draft is not there for the public, nor can anyone sign up for it...
    assert web.get("/events/draft-night").status_code == 404
    assert signup(web, 1, "draft-night").status_code == 404
    # ...but an organizer can preview it
    sign_in(web, "organizer@example.com")
    assert "공개 전 미리보기" in web.get("/events/draft-night").text
    # A finished event says so and takes no signups
    assert status(web, "old-night")["state"] == "ended"
    assert signup(web, 1, "old-night").json()["error"] == "closed"


def test_organizer_creates_an_event_from_the_form(web):
    from shared import store

    sign_in(web, "organizer@example.com")
    form = {
        "id": "gemini-day",
        "title": "Gemini 데이",
        "summary": "",
        "description": "소개",
        "venue": "공학원",
        "starts_at": "2030-03-10T19:00",
        "ends_at": "2030-03-10T21:00",
        "opens_at": "2030-03-01T12:00",
        "closes_at": "2030-03-10T19:00",
        "confirm_deadline": "2030-03-09T18:00",
        "seat_limit": "40",
        "waitlist_limit": "10",
    }
    created = web.post("/admin/events/new", data=form, follow_redirects=False)
    assert created.status_code == 303 and created.headers["location"].startswith("/admin/events/gemini-day")
    ev = store.get_event("gemini-day")
    assert (ev.seat_limit, ev.published, ev.starts.hour) == (40, False, 19)  # Korea time, hidden until published

    again = web.post("/admin/events/new", data=form)  # the address is taken
    assert again.status_code == 422 and "이미 사용 중인 주소" in again.text
    backwards = web.post("/admin/events/new", data=form | {"id": "late", "ends_at": "2030-03-10T18:00"})
    assert backwards.status_code == 422 and "종료 시간은 시작 시간보다" in backwards.text

    web.post("/admin/events/gemini-day/publish", data={"published": "1"})
    assert store.get_event("gemini-day").published


def test_a_ticket_only_opens_the_door_of_its_own_event(web, worker):
    from shared import demo, store, tokens

    store.create_event(demo.make_event("other"))
    reg_id = signup(web, 1, "other").json()["id"]
    push(worker, reg_id)
    sign_in(web, "door@example.com")
    result = web.post("/api/checkin", json={"event_id": "test", "value": tokens.ticket_token("other", reg_id)}).json()
    assert result["result"] == "wrong_event"
    assert web.get(f"/api/registrations/{reg_id}").json()["checked_in"] is False


# ---------- accounts ----------


def test_sign_in_with_an_emailed_code(web):
    from shared import store

    assert web.get("/me", follow_redirects=False).headers["location"] == "/login?next=/me"
    web.post("/api/auth/code", json={"email": "Me@Example.com"})
    topic, message = PUBLISHED[-1]
    assert topic == "emails" and message["kind"] == "login_code" and message["to"] == "me@example.com"
    assert "code" not in str(store.get_user("me@example.com"))  # nothing stored before sign-in

    wrong = "000000" if message["code"] != "000000" else "111111"
    assert web.post("/api/auth/verify", json={"email": "me@example.com", "code": wrong}).status_code == 401
    ok = web.post("/api/auth/verify", json={"email": "me@example.com", "code": message["code"], "next": "//evil"})
    assert ok.json()["next"] == "/me"  # only paths on this site
    assert "me@example.com" in web.get("/me").text
    # A code works once
    again = web.post("/api/auth/verify", json={"email": "me@example.com", "code": message["code"]})
    assert again.status_code == 410


def test_guessing_codes_locks_the_code(web):
    web.post("/api/auth/code", json={"email": "me@example.com"})
    code = PUBLISHED[-1][1]["code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        web.post("/api/auth/verify", json={"email": "me@example.com", "code": wrong})
    # Five wrong tries: even the right code no longer works
    assert web.post("/api/auth/verify", json={"email": "me@example.com", "code": code}).json()["error"] == "locked"


def test_a_new_code_waits_a_minute(web):
    assert web.post("/api/auth/code", json={"email": "me@example.com"}).status_code == 200
    assert web.post("/api/auth/code", json={"email": "me@example.com"}).json()["error"] == "too_soon"


def test_roles_come_from_the_email_lists(web):
    sign_in(web, "door@example.com")
    assert web.get("/checkin").status_code == 200
    assert web.get("/admin").status_code == 403  # staff, not admin
    page = web.get("/").text
    assert 'href="/checkin"' in page and 'href="/admin"' not in page

    web.post("/logout")
    sign_in(web, "organizer@example.com")
    assert web.get("/admin").status_code == 200 and web.get("/checkin").status_code == 200


def test_my_page_lists_my_signups_and_prefills_the_form(web):
    web.post("/api/events/test/registrations", json={"name": "나", "email": "me@example.com"})
    sign_in(web, "me@example.com")
    web.post("/me/profile", data={"name": "김연세", "affiliation": "컴퓨터과학과"})
    page = web.get("/me").text
    assert "Cloud Run 실전 나이트" in page and "김연세" in page
    event_page = web.get("/events/test").text
    assert 'value="me@example.com" readonly' in event_page and 'value="컴퓨터과학과"' in event_page


def test_event_card_is_right_before_any_script_runs(web):
    page = web.get("/events/test").text
    assert (
        '<div class="reg-state" data-when="open">' in page
        and '<div class="reg-state" data-when="ended" hidden>' in page
    )
    assert "data-buybar-note>" in page and "석 남음</span></div>" in page
    web.post("/api/events/test/registrations", json={"name": "나", "email": "me@example.com"})
    sign_in(web, "me@example.com")
    mine = web.get("/events/test").text
    assert (
        '<div class="reg-state" data-when="mine">' in mine and '<div class="reg-state" data-when="open" hidden>' in mine
    )
    assert "참석 확인을 눌러 주세요" in mine and "참석 확인 필요" in mine and ">티켓 보기</button>" in mine


def past_ticket(email="me@example.com", event_id="old", title="지난 행사 하나", days=-3, check_in=False):
    from shared import demo, store

    ev = demo.make_event(event_id, title=title, starts_in=timedelta(days=days))
    store.create_event(ev)
    reg = store.register(ev, "나", email, "")
    store.mark_issued(reg.id)
    if check_in:
        store.check_in(reg.id)
    return reg


def test_past_tickets_have_their_own_tab_and_say_what_happened(web):
    came = past_ticket(event_id="came", title="참석한 행사", days=-3, check_in=True)
    past_ticket(event_id="missed", title="불참한 행사", days=-40)
    cancelled = web.post("/api/events/test/registrations", json={"name": "나", "email": "me@example.com"}).json()["id"]
    web.post(f"/api/registrations/{cancelled}/cancel")
    sign_in(web, "me@example.com")
    assert "참석한 행사" not in web.get("/me").text
    page = web.get("/me?view=past").text
    assert page.index("Cloud Run 실전 나이트") < page.index("참석한 행사") < page.index("불참한 행사")  # latest first
    assert "입장 완료" in page and "미입장" in page and "취소됨" in page and "참가 확정" not in page
    assert came.code not in page


def test_my_page_past_tab_is_empty_with_a_way_forward(web):
    sign_in(web, "me@example.com")
    page = web.get("/me?view=past").text
    assert "지난 티켓이 없습니다" in page and "신청할 수 있는 행사" not in page


def test_a_ticket_for_an_ended_event_has_nothing_left_to_do(web):
    reg = past_ticket()
    page = web.get(f"/t/{reg.id}").text
    assert "지난 티켓" in page and "입장 기록이 없습니다." in page
    for gone in ("qr.svg", "data-cancel", "data-confirm>", "calendar.ics", "참석 확인"):
        assert gone not in page
    answer = web.post(f"/api/registrations/{reg.id}/cancel")
    assert answer.status_code == 409 and answer.json()["error"] == "event_started"


def test_the_event_page_of_an_ended_event_links_to_my_ticket(web):
    reg = past_ticket()
    sign_in(web, "me@example.com")
    page = web.get("/events/old").text
    assert '<div class="reg-state" data-when="ended">' in page and f'href="/t/{reg.id}">내 티켓 보기' in page
    assert '<div class="reg-state" data-when="mine" hidden>' in page


def test_my_page_has_one_logout_and_separate_tabs(web):
    assert web.get("/me/profile", follow_redirects=False).headers["location"] == "/login?next=/me/profile"
    sign_in(web, "me@example.com")
    tickets, profile = web.get("/me").text, web.get("/me/profile").text
    for page in (tickets, profile):
        assert page.count('action="/logout"') == 1
    assert 'action="/me/profile"' not in tickets and 'action="/me/profile"' in profile


def test_saving_the_profile_returns_to_the_profile(web):
    sign_in(web, "me@example.com")
    saved = web.post("/me/profile", data={"name": "김연세", "affiliation": ""}, follow_redirects=False)
    assert saved.headers["location"] == "/me/profile?notice=saved"
    assert "저장되었습니다" in web.get(saved.headers["location"]).text


def test_empty_my_page_suggests_open_events_and_a_ticket_replaces_them(web):
    sign_in(web, "me@example.com")
    assert "신청할 수 있는 행사" in web.get("/me").text
    web.post("/api/events/test/registrations", json={"name": "나", "email": "me@example.com"})
    page = web.get("/me").text
    assert "신청할 수 있는 행사" not in page and "예정된 티켓" in page


def test_deleting_the_account_cancels_upcoming_seats(web):
    reg_id = web.post("/api/events/test/registrations", json={"name": "나", "email": "me@example.com"}).json()["id"]
    sign_in(web, "me@example.com")
    page = web.post("/me/delete").text
    assert "회원 탈퇴가 완료되었습니다" in page and "data-account-menu" not in page
    assert web.get(f"/api/registrations/{reg_id}").json()["status"] == "cancelled"
    assert web.get("/me", follow_redirects=False).status_code == 303  # signed out


def test_without_email_only_organizers_get_a_code(web, monkeypatch):
    monkeypatch.setenv("MAIL_BACKEND", "log")
    config.get_settings.cache_clear()
    assert web.post("/api/auth/code", json={"email": "me@example.com"}).json()["error"] == "mail_off"
    assert web.post("/api/auth/code", json={"email": "organizer@example.com"}).status_code == 200


def test_worker_sends_mail_to_the_outbox(worker):
    from shared import mail

    envelope = {
        "message": {
            "data": base64.b64encode(
                json.dumps({"kind": "login_code", "to": "a@b.c", "code": "123456"}).encode()
            ).decode(),
            "messageId": "2",
        }
    }
    assert worker.post("/mail", json=envelope).status_code == 204
    assert mail.outbox()[0]["subject"].endswith("인증 코드 123456")


# ---------- emails ----------


def emails_requested():
    return [(data["kind"], data["to"]) for topic, data in PUBLISHED if topic == "emails"]


def test_each_step_asks_for_its_email(web, worker):
    set_event(seat_limit=1)
    holder = signup(web, 1).json()["id"]
    signup(web, 2)  # the event is full: waitlisted
    push(worker, holder)
    web.post(f"/api/registrations/{holder}/cancel")
    assert emails_requested() == [
        ("waitlisted", "s2@yonsei.ac.kr"),
        ("ticket_issued", "s1@yonsei.ac.kr"),
        ("cancelled", "s1@yonsei.ac.kr"),
    ]


def test_a_repeated_delivery_sends_the_email_once(web):
    from shared import mail

    reg_id = signup(web, 1).json()["id"]
    message = {"kind": "ticket_issued", "to": "s1@yonsei.ac.kr", "registration_id": reg_id}
    assert mail.send(message) is True
    assert mail.send(message) is False  # Pub/Sub delivered it again
    assert len(mail.outbox()) == 1


def test_every_email_has_a_subject_and_the_ticket_or_event_link(web):
    from shared import mail

    reg_id = signup(web, 1).json()["id"]
    for kind in mail.REGISTRATION_KINDS:
        subject, body = mail.render({"kind": kind, "to": "s1@yonsei.ac.kr", "registration_id": reg_id})
        assert subject.startswith("[Cloud Ticketing]") and "Cloud Run 실전 나이트" in subject + body
        assert f"/t/{reg_id}" in body or "/events/test" in body


def test_housekeeping_reminds_once_a_day_before_the_deadline(web, capsys):
    from jobs import housekeeping

    confirmed = signup(web, 1).json()["id"]
    signup(web, 2)  # holds a seat, never confirms
    web.post(f"/api/registrations/{confirmed}/confirm")
    housekeeping.main()  # the deadline is days away: no reminder yet
    assert ("confirm_reminder", "s2@yonsei.ac.kr") not in emails_requested()

    set_event(confirm_deadline=(config.now() + timedelta(hours=5)).isoformat())
    housekeeping.main()
    housekeeping.main()  # every 5 minutes: still one reminder each
    assert [e for e in emails_requested() if e[0] == "confirm_reminder"] == [("confirm_reminder", "s2@yonsei.ac.kr")]


class FakeCloudRun:
    """Stands in for Cloud Run's Admin API: keeps the service-level minimum, records each change."""

    minimum = 0
    patches: list = []

    def __init__(self, credentials):
        pass

    def get(self, url, timeout):
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"scaling": {"minInstanceCount": FakeCloudRun.minimum}}
        )

    def patch(self, url, params, json):
        FakeCloudRun.patches.append((url, params, json))
        FakeCloudRun.minimum = json["scaling"]["minInstanceCount"]
        return SimpleNamespace(raise_for_status=lambda: None)


@pytest.fixture
def cloud_run(monkeypatch):
    import google.auth
    import google.auth.transport.requests

    FakeCloudRun.minimum, FakeCloudRun.patches = 0, []
    monkeypatch.setenv("WEB_SERVICE", "projects/p/locations/r/services/web")
    monkeypatch.setattr(google.auth, "default", lambda scopes: (None, "p"))
    monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", FakeCloudRun)
    return FakeCloudRun


def test_prewarm_changes_the_service_level_minimum_only_around_an_opening(web, cloud_run):
    from jobs import housekeeping

    set_event(opens_at=(config.now() - timedelta(minutes=61)).isoformat())
    housekeeping.main()
    assert cloud_run.patches == []  # already at 0, the tail has passed: no call at all

    set_event(opens_at=(config.now() + timedelta(minutes=10)).isoformat(), published=False)
    housekeeping.main()
    assert cloud_run.patches == []  # a draft event brings nobody

    set_event(published=True)
    housekeeping.main()
    housekeeping.main()  # already at 3: no second call
    url = "https://run.googleapis.com/v2/projects/p/locations/r/services/web"
    assert cloud_run.patches == [
        (url, {"updateMask": "scaling.minInstanceCount"}, {"scaling": {"minInstanceCount": 3}})
    ]

    set_event(opens_at=(config.now() - timedelta(minutes=59)).isoformat())
    housekeeping.main()  # still within the hour after opening
    assert cloud_run.minimum == 3
    set_event(opens_at=(config.now() - timedelta(minutes=61)).isoformat())
    housekeeping.main()
    assert cloud_run.minimum == 0


def test_prewarm_goes_back_to_the_usual_minimum_not_to_zero(web, cloud_run, monkeypatch):
    from jobs import housekeeping

    monkeypatch.setenv("WEB_MIN_INSTANCES", "1")
    housekeeping.main()  # the test event opened yesterday
    assert cloud_run.minimum == 1
    set_event(opens_at=(config.now() + timedelta(minutes=10)).isoformat())
    housekeeping.main()
    assert cloud_run.minimum == 3


def test_a_run_that_hit_trouble_exits_with_failure_after_doing_the_rest(web, cloud_run, monkeypatch):
    from jobs import housekeeping

    assert housekeeping.main() == 0
    monkeypatch.setattr(housekeeping, "prewarm", lambda events: pub_sub_down("", {}))
    signup(web, 1)
    set_event(confirm_deadline=(config.now() + timedelta(hours=5)).isoformat())
    assert housekeeping.main() == 1
    assert ("confirm_reminder", "s1@yonsei.ac.kr") in emails_requested()  # the rest still ran


def test_housekeeping_prewarms_around_the_opening(web, capsys):
    from jobs import housekeeping

    housekeeping.main()  # the test event opened yesterday: nothing to warm
    assert "keep 0 instances" in capsys.readouterr().out
    set_event(opens_at=(config.now() + timedelta(minutes=10)).isoformat())
    housekeeping.main()
    assert "keep 3 instances" in capsys.readouterr().out


def test_signing_up_after_the_deadline_keeps_the_seat(web, worker):
    """No time left to confirm, so the job that releases unconfirmed seats must leave this one."""
    from jobs import housekeeping
    from shared import store
    from shared.config import now

    set_event(confirm_deadline=(now() - timedelta(hours=1)).isoformat())
    assert "지금 신청하면 참석 확인 없이" in web.get("/events/test").text
    answer = signup(web, 1).json()
    assert answer["confirmed"] is True
    push(worker, answer["id"])
    housekeeping.release(store.get_event("test"))
    assert store.get(answer["id"]).status == "issued"


def door(web, path, value):
    return web.post(f"/api/checkin{path}", json={"event_id": "test", "value": value}).json()


def test_the_signed_event_decides_even_if_the_registration_is_here(web):
    """A real signature, but for another event: the token itself says it is not for this door."""
    from shared import tokens

    reg_id = signup(web, 1).json()["id"]
    sign_in(web, "door@example.com")
    assert door(web, "/lookup", tokens.ticket_token("other-event", reg_id))["result"] == "wrong_event"
    assert door(web, "/lookup", tokens.ticket_token("test", reg_id))["result"] != "wrong_event"


def test_the_door_looks_first_then_staff_let_in_and_can_undo(web, worker):
    from shared import store

    reg_id = signup(web, 1).json()["id"]
    push(worker, reg_id)
    sign_in(web, "door@example.com")
    code = store.get(reg_id).code

    looked = door(web, "/lookup", code)
    assert looked["result"] == "ready" and looked["attendee"]["name"] == "학생1"
    assert store.get(reg_id).checked_in_at is None  # looking changes nothing

    done = door(web, "", code)
    assert done["result"] == "ok" and done["counts"]["checked_in"] == 1
    assert door(web, "/lookup", code)["result"] == "already"

    undone = door(web, "/undo", code)
    assert undone["result"] == "undone" and undone["counts"]["checked_in"] == 0
    assert door(web, "/undo", code)["result"] == "not_checked_in"
    assert door(web, "/lookup", code)["result"] == "ready"


def test_the_door_says_when_it_is_not_the_event_day(web, worker):
    from shared import store
    from shared.config import now

    reg_id = signup(web, 1).json()["id"]
    push(worker, reg_id)
    sign_in(web, "door@example.com")
    code = store.get(reg_id).code
    assert door(web, "/lookup", code)["off_day"] is True  # the test event is a week away
    set_event(starts_at=now().isoformat())
    assert door(web, "/lookup", code)["off_day"] is False


def test_a_waitlisted_person_has_no_seat_at_the_door(web, worker):
    from shared import store

    set_event(seat_limit=1)
    push(worker, signup(web, 1).json()["id"])
    waiting = signup(web, 2).json()
    assert waiting["status"] == "waitlisted"
    sign_in(web, "door@example.com")
    assert door(web, "/lookup", waiting["code"])["result"] == "waitlisted"
    assert door(web, "", waiting["code"])["result"] == "waitlisted"
    assert store.get(waiting["id"]).checked_in_at is None


def test_only_staff_can_look_up_or_undo(web):
    for path in ("/lookup", "/undo"):
        assert web.post(f"/api/checkin{path}", json={"event_id": "test", "value": "ABC-DEF"}).status_code == 401


def test_each_stage_has_one_name_on_every_screen(web, worker):
    from shared import store

    reg_id = web.post("/api/events/test/registrations", json={"name": "나", "email": "me@example.com"}).json()["id"]
    push(worker, reg_id)
    sign_in(web, "me@example.com")

    for page in (web.get("/me").text, web.get("/events/test").text, web.get(f"/t/{reg_id}").text):
        assert "참석 확인 필요" in page and "참가 확정" not in page and "발급 완료" not in page
    assert "참석 확인 후 입구에서" in web.get(f"/t/{reg_id}").text

    web.post(f"/api/registrations/{reg_id}/confirm")
    for page in (web.get("/me").text, web.get("/events/test").text, web.get(f"/t/{reg_id}").text):
        assert "참석 확인 완료" in page and "참석 확인 필요" not in page
    assert web.get(f"/api/registrations/{reg_id}").json()["card"]["title"] == "참석 확인 완료"

    store.check_in(reg_id)
    assert "입장 완료" in web.get("/me").text
    event_page = web.get("/events/test").text
    assert '<p class="reg-title" data-mine-title>입장 완료</p>' in event_page
    assert '<p class="reg-foot" data-when="mine" data-before-entry hidden>' in event_page
    ticket = web.get(f"/t/{reg_id}").text
    assert "입장 완료" in ticket
    for gone in ("qr.svg", "보여 주세요", "data-cancel", "calendar.ics"):
        assert gone not in ticket


def test_a_ticket_cannot_be_cancelled_once_the_event_started(web, worker):
    from shared.config import now

    reg_id = signup(web, 1).json()["id"]
    push(worker, reg_id)
    set_event(starts_at=(now() - timedelta(minutes=5)).isoformat())
    assert "data-cancel" not in web.get(f"/t/{reg_id}").text
    assert web.post(f"/api/registrations/{reg_id}/cancel").json()["error"] == "event_started"


def test_an_open_ticket_can_tell_that_it_is_out_of_date(web, worker):
    """The page holds what it was drawn with; its script compares that with each answer and reloads on a change."""
    from shared.config import now

    reg_id = signup(web, 1).json()["id"]
    push(worker, reg_id)
    answer = web.get(f"/api/registrations/{reg_id}").json()
    assert f'data-deadline="{answer["deadline"]}"' in web.get(f"/t/{reg_id}").text
    assert 'data-confirmed="false"' in web.get(f"/t/{reg_id}").text

    edited = (now() + timedelta(days=2)).isoformat()
    set_event(confirm_deadline=edited)
    assert web.get(f"/api/registrations/{reg_id}").json()["deadline"] == edited
    assert f'data-deadline="{edited}"' in web.get(f"/t/{reg_id}").text


def test_stylesheets_have_balanced_braces():
    """A stray } makes browsers drop the rule after it, silently: the footer lost all its styles this way."""
    import re
    from pathlib import Path

    sheets = list((Path(__file__).parent.parent / "web" / "static" / "css").glob("*.css"))
    assert sheets
    for sheet in sheets:
        depth = 0
        for char in re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S):
            depth += {"{": 1, "}": -1}.get(char, 0)
            assert depth >= 0, f"stray closing brace in {sheet.name}"
        assert depth == 0, f"unclosed rule in {sheet.name}"


def test_site_pages_and_a_friendly_404(web):
    home = web.get("/").text
    assert 'href="/terms"' not in home and 'href="/admin"' not in home  # the footer only says what the project is
    signup = web.get("/signup").text
    assert 'href="/terms"' in signup and 'href="/privacy"' in signup
    assert "보관 기간" in web.get("/privacy").text and web.get("/terms").status_code == 200
    page = web.get("/no-such-page", headers={"accept": "text/html"})
    assert page.status_code == 404 and "전체 행사 보기" in page.text
    assert web.get("/no-such-page").json() == {"detail": "Not Found"}  # scripts still get JSON


def test_a_job_waits_for_its_local_deliveries(monkeypatch):
    import time

    from shared import bus

    delivered = []
    monkeypatch.setattr(bus, "deliver_with_retries", lambda *args: (time.sleep(0.2), delivered.append(args)))
    bus.deliver_in_background("http://worker", {}, {}, "test")
    bus.wait_for_deliveries()  # the job calls this before exiting
    assert delivered == [("http://worker", {}, {}, "test")]


def test_an_uploaded_poster_is_served_on_the_event_page(web):
    import io

    from PIL import Image

    sign_in(web, "organizer@example.com")
    image = io.BytesIO()
    Image.new("RGB", (800, 800), "#336699").save(image, "PNG")
    web.post("/admin/events/test/poster", files={"file": ("poster.png", image.getvalue(), "image/png")})
    page = web.get("/events/test").text
    src = page.split('class="cover cover--hero" src="')[1].split('"')[0]
    assert src.startswith("/media/posters/test/web.jpg") and web.get(src).status_code == 200


def test_search_filter_and_calendar_feed(web):
    from shared import demo, store

    store.create_event(demo.make_event("k8s", title="쿠버네티스 입문", opened_ago=timedelta(days=-2)))
    assert "쿠버네티스 입문" in web.get("/?q=쿠버").text
    assert "쿠버네티스 입문" not in web.get("/?q=도커").text
    only_open = web.get("/?open=1").text  # k8s opens in two days: not open yet
    assert "/events/test" in only_open and "/events/k8s" not in only_open
    feed = web.get("/calendar.ics").text
    assert feed.count("BEGIN:VEVENT") == 2 and "X-WR-CALNAME:Cloud Ticketing" in feed


def test_the_door_can_find_a_guest_by_name(web, worker):
    reg_id = signup(web, 1).json()["id"]
    push(worker, reg_id)
    assert web.get("/api/events/test/attendees").status_code == 401
    sign_in(web, "door@example.com")
    guests = web.get("/api/events/test/attendees").json()
    assert [(g["name"], g["checked_in"]) for g in guests] == [("학생1", False)]
    web.post("/api/checkin", json={"event_id": "test", "value": guests[0]["code"]})  # the 입장 button
    assert web.get("/api/events/test/attendees").json()[0]["checked_in"] is True


def test_sample_attendees_have_working_tickets(web):
    """An issued sample ticket has its QR image; a waitlisted one has none yet."""
    from shared import demo, store

    set_event(seat_limit=1)
    demo.add_attendees(store.get_event("test"), 2)
    regs = {reg.status: reg for reg in store.list_registrations("test")}
    assert web.get(f"/t/{regs['issued'].id}/qr.svg").status_code == 200
    assert web.get(f"/t/{regs['waitlisted'].id}/qr.svg").status_code == 404


@pytest.mark.parametrize("revision, secure", [(None, False), ("web-00001-abc", True)])
def test_session_cookie_is_secure_only_on_cloud_run(web, monkeypatch, revision, secure):
    """Safari drops a Secure cookie on http://localhost, so sign-in there needs a plain one."""
    if revision:
        monkeypatch.setenv("K_REVISION", revision)
    monkeypatch.setenv("K_SERVICE", "web")  # dev.py and compose set this locally too
    config.get_settings.cache_clear()

    email = "visitor@example.com"
    assert web.post("/api/auth/code", json={"email": email}).status_code == 200
    code = [data["code"] for topic, data in PUBLISHED if data.get("kind") == "login_code"][-1]
    response = web.post("/api/auth/verify", json={"email": email, "code": code})
    assert ("secure" in response.headers["set-cookie"].lower()) is secure


@pytest.fixture
def google(web, monkeypatch):
    """Google sign-in switched on, with Google's token check replaced: a test says what the token says."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    config.get_settings.cache_clear()
    from web import auth

    claims = {"email": "Me@Gmail.com", "email_verified": True, "name": "구글 사용자"}
    monkeypatch.setattr(auth, "google_claims", lambda credential, client_id: claims)
    return claims


def google_sign_in(web, target="/me"):
    return web.post("/api/auth/google", json={"credential": "token", "next": target})


def test_google_sign_in_starts_a_session_and_fills_the_profile(web, google):
    from shared import store

    response = google_sign_in(web, "/events/test")
    assert response.status_code == 200 and response.json()["next"] == "/events/test"
    assert web.get("/me", follow_redirects=False).status_code == 200
    assert store.get_user("me@gmail.com")["name"] == "구글 사용자"


def test_google_sign_in_keeps_an_existing_profile_and_stays_on_this_site(web, google):
    from shared import store

    store.save_user("me@gmail.com", "직접 입력한 이름", "수학과")
    assert google_sign_in(web, "//evil.example").json()["next"] == "/me"
    assert store.get_user("me@gmail.com")["name"] == "직접 입력한 이름"


def test_google_sign_in_gives_the_same_roles_as_the_email_list(web, google, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "me@gmail.com")
    config.get_settings.cache_clear()
    assert google_sign_in(web).status_code == 200
    assert web.get("/admin").status_code == 200


@pytest.mark.parametrize(
    "claims, status",
    [
        ({"email": "me@gmail.com", "email_verified": False}, 403),
        ({"email": "me@outside.example", "email_verified": True}, 403),  # Google does not vouch for it
        ({"email": "me@yonsei.ac.kr", "email_verified": True, "hd": "yonsei.ac.kr"}, 200),  # Workspace
    ],
)
def test_google_sign_in_takes_only_addresses_google_vouches_for(web, google, claims, status):
    google.clear()
    google.update(claims)
    response = google_sign_in(web)
    assert response.status_code == status
    assert ("session" in web.cookies) is (status == 200)


def test_google_sign_in_refuses_a_forged_token_for_real(web, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    config.get_settings.cache_clear()
    assert google_sign_in(web).status_code == 401  # the real check: not even a JWT
    assert "session" not in web.cookies


def test_google_sign_in_is_off_without_a_client_id(web):
    assert google_sign_in(web).status_code == 404
    page = web.get("/login")
    assert "accounts.google.com/gsi/client" not in page.text and "data-email-form" in page.text
    assert page.headers["referrer-policy"] == "no-referrer-when-downgrade"


def test_login_page_shows_google_and_email_or_google_alone(web, google, monkeypatch):
    both = web.get("/login").text
    assert "accounts.google.com/gsi/client" in both and "data-email-form" in both
    assert 'data-client-id="test-client.apps.googleusercontent.com"' in both

    monkeypatch.setenv("MAIL_BACKEND", "log")
    config.get_settings.cache_clear()
    alone = web.get("/login").text
    assert "data-google-button" in alone and "data-email-form" not in alone and "운영진만" not in alone


def test_no_address_is_published_unless_one_is_set(web, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "organizer@example.com")
    config.get_settings.cache_clear()
    for path in ("/", "/privacy", "/terms"):
        page = web.get(path).text
        assert "organizer@example.com" not in page and "mailto:" not in page


def test_services_refuse_to_start_on_google_cloud_without_their_secrets(monkeypatch):
    from fastapi import FastAPI

    from shared.runtime import setup

    monkeypatch.setenv("BACKEND", "gcp")
    monkeypatch.setenv("TICKET_SECRET", "set")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        setup(FastAPI(), "web", secrets=("TICKET_SECRET", "SESSION_SECRET"))


def test_nothing_is_signed_with_a_public_or_empty_key_on_google_cloud(monkeypatch):
    from shared import tokens

    monkeypatch.setenv("BACKEND", "gcp")
    monkeypatch.delenv("TICKET_SECRET", raising=False)
    config.get_settings.cache_clear()
    assert config.get_settings().ticket_secret == ""
    with pytest.raises(RuntimeError):
        tokens.ticket_token("event", "registration")


def test_design_page_is_for_admins_on_cloud_run(web, monkeypatch):
    assert web.get("/design").status_code == 200  # locally, for whoever works on the site
    monkeypatch.setenv("ADMIN_EMAILS", "organizer@example.com")
    monkeypatch.setenv("K_REVISION", "web-00001-abc")
    config.get_settings.cache_clear()
    assert web.get("/design").status_code == 404

    monkeypatch.delenv("K_REVISION")  # sign in over plain http, then come back as Cloud Run
    config.get_settings.cache_clear()
    sign_in(web, "organizer@example.com")
    monkeypatch.setenv("K_REVISION", "web-00001-abc")
    config.get_settings.cache_clear()
    assert web.get("/design").status_code == 200


def test_email_for_a_registration_that_is_gone_is_dropped_not_retried(web, worker):
    """Otherwise it fails every retry and lands in the dead letter queue, which alerts someone."""
    envelope = {
        "message": {
            "data": base64.b64encode(
                json.dumps({"kind": "promoted", "to": "gone@example.com", "registration_id": "no-such-id"}).encode()
            ).decode(),
            "messageId": "1",
        }
    }
    assert worker.post("/mail", json=envelope).status_code == 204


def test_header_and_pages_offer_sign_up(web, google, monkeypatch):
    home = web.get("/").text
    assert 'href="/signup"' in home and 'href="/login"' in home

    signup = web.get("/signup").text
    assert "data-email-form" in signup and 'name="agree"' in signup
    assert "data-google-button" in signup and 'data-text="signup_with"' in signup
    login = web.get("/login").text
    assert "data-email-form" in login and 'name="agree"' not in login and 'href="/signup"' in login
    assert "password" not in login
    assert web.get("/password-reset").status_code == 404

    monkeypatch.setenv("MAIL_BACKEND", "log")
    config.get_settings.cache_clear()
    alone = web.get("/signup").text
    assert "data-google-button" in alone and "data-email-form" not in alone


def test_sign_up_works_only_after_the_emailed_code_and_keeps_the_name(web):
    from shared import store

    assert web.post("/api/auth/code", json={"email": "new@example.com"}).status_code == 200
    assert store.get_user("new@example.com") is None  # not an account until the code proves the address
    code = [data["code"] for topic, data in PUBLISHED if data.get("kind") == "login_code"][-1]
    wrong = str((int(code) + 1) % 1_000_000).zfill(6)
    attempt = {"email": "new@example.com", "name": "새 회원"}
    assert web.post("/api/auth/verify", json=attempt | {"code": wrong}).status_code == 401
    assert store.get_user("new@example.com") is None
    assert (
        web.post("/api/auth/verify", json=attempt | {"code": code, "next": "/events/test"}).json()["next"]
        == "/events/test"
    )
    assert store.get_user("new@example.com")["name"] == "새 회원"


def test_a_past_event_with_no_signups_does_not_claim_zero_attendees(web):
    from shared import demo, store

    store.create_event(demo.make_event("past", title="지난 세션", starts_in=timedelta(days=-3)))
    assert "0명" not in web.get("/events/past").text
    assert "0명" not in web.get("/?view=past").text


def test_smtp_sends_from_the_verified_address_and_logs_in_with_the_provider_login(monkeypatch):
    """With Resend the login is the word resend, so the From address has its own setting."""
    from shared import mail

    monkeypatch.setenv("MAIL_FROM", "tickets@mail.example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "re_test")
    config.get_settings.cache_clear()
    calls = {}

    class Server:
        def __init__(self, host, port, timeout):
            calls["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            calls["tls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, email):
            calls["from"] = email["From"]
            calls["message_id"] = email["Message-ID"]

    monkeypatch.setattr(mail.smtplib, "SMTP", Server)
    mail._send_smtp("a@example.com", "제목", "본문")
    assert calls["server"] == ("smtp.resend.com", 587) and calls["tls"]
    assert calls["login"] == ("resend", "re_test")
    assert "tickets@mail.example.com" in calls["from"] and "mail.example.com" in calls["message_id"]


def test_sending_never_logs_the_sign_in_code_or_the_address(monkeypatch, caplog):
    """Anyone who can read the logs could otherwise sign in as that person."""
    import logging

    from shared import mail

    monkeypatch.setenv("MAIL_FROM", "tickets@mail.example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "re_test")
    config.get_settings.cache_clear()

    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def send_message(self, email):
            pass

    monkeypatch.setattr(mail.smtplib, "SMTP", Server)
    with caplog.at_level(logging.INFO):
        mail._send_smtp("person@example.com", "[Cloud Ticketing] 인증 코드 123456", "본문")
    assert "123456" not in caplog.text and "person@example.com" not in caplog.text
    assert "sent email" in caplog.text
