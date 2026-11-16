"""The cloud code path (shared/gcp.py) against the Firestore and Pub/Sub emulators.

Google's emulators run on the CI machine and behave like the real services, with
no project, no credentials and no bill. CI starts them and runs:

    BACKEND=gcp GCP_PROJECT=demo-tickets \\
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 PUBSUB_EMULATOR_HOST=127.0.0.1:8086 \\
    python -m pytest tests/test_gcp_emulator.py

Without the emulators (a normal `pytest` run) these tests are skipped.
"""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("BACKEND") == "gcp" and os.environ.get("FIRESTORE_EMULATOR_HOST")),
    reason="needs BACKEND=gcp and the Firestore/Pub/Sub emulators",
)

PROJECT = os.environ.get("GCP_PROJECT", "demo-tickets")


EVENT = {}


@pytest.fixture(autouse=True)
def fresh_event():
    from shared import demo, store

    # A new event per test: its seat counter and documents start empty
    ev = demo.make_event(f"test-{uuid.uuid4().hex[:8]}", seat_limit=10, waitlist_limit=10)
    store.create_event(ev)
    EVENT["ev"] = ev
    yield


def event():
    return EVENT["ev"]


def register(n):
    from shared import store

    try:
        return store.register(event(), f"학생{n}", f"s{n}@yonsei.ac.kr", "").id
    except store.Full:
        return "full"
    except store.AlreadyRegistered:
        return "duplicate"


def status(registration_id):
    from shared import store

    return store.get(registration_id).status


def test_transaction_never_oversells():
    from shared import store

    # 5 at once still makes transactions collide. The emulator locks harder than Firestore:
    # at 10, one signup now and then used up its 20 retries and failed CI (2026-11-16)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(register, range(40)))
    ids = [r for r in results if r not in ("full", "duplicate")]
    assert results.count("full") == 20 and len(ids) == 20
    assert sorted(status(i) for i in ids) == ["pending"] * 10 + ["waitlisted"] * 10
    stats = store.stats(event())
    assert (stats["held"], stats["pending"], stats["waitlisted"], stats["seats_free"]) == (10, 10, 10, 0)


def test_lifecycle_keeps_counts_consistent():
    from shared import store

    first, second = register(1), register(2)
    assert register(1) == "duplicate"
    assert store.get_by_code(store.get(first).code).id == first

    assert store.mark_issued(first) is True
    assert store.mark_issued(first) is False  # a duplicate message changes nothing
    assert store.check_in(first) == "ok"
    assert store.check_in(first) == "already"
    assert store.check_in(second) == "not_issued"

    assert store.cancel(second) is True
    assert register(2) != "duplicate"  # the email is free again after cancelling
    stats = store.stats(event())
    assert (stats["held"], stats["pending"], stats["issued"], stats["cancelled"], stats["checked_in"]) == (
        2,
        1,
        1,
        1,
        1,
    )


def test_undoing_a_check_in_keeps_the_count_right():
    from shared import store

    first = register(1)
    store.mark_issued(first)
    assert store.check_in(first) == "ok"
    assert store.stats(event())["checked_in"] == 1
    assert store.undo_check_in(first) is True
    assert store.undo_check_in(first) is False
    assert store.get(first).checked_in_at is None
    assert store.stats(event())["checked_in"] == 0


def test_a_signup_after_the_deadline_counts_as_confirmed():
    from dataclasses import replace
    from datetime import timedelta

    from shared import store
    from shared.config import now

    EVENT["ev"] = replace(event(), confirm_deadline=(now() - timedelta(hours=1)).isoformat())
    store.update_event(event())
    late = register(1)
    assert store.get(late).confirmed_at is not None
    assert late not in store.unconfirmed(event().id)


def test_documents_carry_an_expiry_for_ttl():
    from shared import gcp, store

    registration_id = register(1)
    doc = gcp._db().collection("registrations").document(registration_id).get().to_dict()
    # The TTL policy (infra/data.tf) deletes the document after this time
    assert doc["expire_at"] > event().starts
    assert store.get_event(event().id).title == event().title  # the event itself round-trips


def test_an_event_id_is_taken_once():
    from shared import store

    with pytest.raises(store.EventExists):
        store.create_event(event())


def test_publish_reaches_the_topic():
    from google.cloud import pubsub_v1

    from shared import bus

    publisher, subscriber = pubsub_v1.PublisherClient(), pubsub_v1.SubscriberClient()
    topic = publisher.topic_path(PROJECT, "registrations")
    subscription = subscriber.subscription_path(PROJECT, f"test-{uuid.uuid4().hex[:8]}")
    try:
        publisher.create_topic(name=topic)
    except Exception:
        pass  # already created by an earlier test
    subscriber.create_subscription(name=subscription, topic=topic)

    bus.publish("registrations", {"registration_id": "abc"})

    response = subscriber.pull(request={"subscription": subscription, "max_messages": 1}, timeout=10)
    assert [json.loads(m.message.data) for m in response.received_messages] == [{"registration_id": "abc"}]


def test_a_freed_seat_goes_to_the_first_in_line_once():
    from shared import store

    EVENT["ev"] = replace(event(), seat_limit=1)
    store.update_event(event())
    holder, first, second = register(1), register(2), register(3)
    assert store.waitlist_position(second) == 2

    assert store.cancel(holder)
    assert store.promote_next(event()) == first
    assert store.promote_next(event()) is None  # a duplicate event finds no free seat
    assert (status(first), status(second)) == ("pending", "waitlisted")
    assert store.waitlist_position(second) == 1
    stats = store.stats(event())
    assert (stats["held"], stats["waitlisted"], stats["cancelled"]) == (1, 1, 1)


def test_a_new_signup_goes_behind_people_already_waiting():
    from shared import store

    EVENT["ev"] = replace(event(), seat_limit=1)
    store.update_event(event())
    holder, first = register(1), register(2)
    assert store.cancel(holder)  # the seat is free; waitlist-promoter has not run yet
    late = register(3)
    assert status(late) == "waitlisted"
    assert (store.waitlist_position(first), store.waitlist_position(late)) == (1, 2)


def test_a_promoted_seat_can_be_found_and_published_again():
    from shared import store

    EVENT["ev"] = replace(event(), seat_limit=1)
    store.update_event(event())
    holder, first = register(1), register(2)
    assert store.get(holder).promoted_at is None
    assert store.mark_issued(holder)  # holds a seat, but no longer waits for a ticket
    assert store.pending(event().id) == []

    assert store.cancel(holder)
    assert store.promote_next(event()) == first
    assert [reg.id for reg in store.pending(event().id)] == [first]
    assert store.pending(event().id)[0].promoted_at


def test_raising_the_seat_limit_opens_a_seat_for_the_next_in_line():
    from shared import bus, store, waitlist

    EVENT["ev"] = replace(event(), seat_limit=1)
    store.update_event(event())
    _, first, second = register(1), register(2), register(3)
    assert waitlist.fill_free_seats(event()) == []

    EVENT["ev"] = replace(event(), seat_limit=2)
    store.update_event(event())
    published = []
    original, bus.publish = bus.publish, lambda topic, data: published.append(data)
    try:
        assert waitlist.fill_free_seats(event()) == [first]
    finally:
        bus.publish = original
    assert published == [{"registration_id": first}]
    assert status(second) == "waitlisted"


def test_only_unconfirmed_seats_are_listed_for_release():
    from shared import store

    confirmed, unconfirmed = register(1), register(2)
    assert store.confirm(confirmed) == "ok"
    assert store.confirm(confirmed) == "already"
    assert store.unconfirmed(event().id) == [unconfirmed]
    assert store.stats(event())["confirmed"] == 1


def test_an_email_counts_as_sent_once_it_is_recorded():
    from shared import store

    key = f"ticket_issued:{uuid.uuid4().hex}"
    assert store.email_was_sent(key) is False
    store.record_email_sent(key)
    store.record_email_sent(key)  # a repeat delivery changes nothing
    assert store.email_was_sent(key) is True


def test_a_sign_in_code_works_once_and_locks_after_five_wrong_tries():
    from shared import store
    from shared.config import now

    email = f"{uuid.uuid4().hex[:8]}@example.com"
    expires = (now() + timedelta(minutes=10)).isoformat(timespec="seconds")
    store.save_login_code(email, "right", expires)
    assert store.use_login_code(email, "wrong") == "wrong"
    assert store.use_login_code(email, "right") == "ok"
    assert store.use_login_code(email, "right") == "missing"

    store.save_login_code(email, "right", expires)
    assert [store.use_login_code(email, "wrong") for _ in range(5)][-1] == "locked"
    assert store.use_login_code(email, "right") == "locked"


def test_a_reminder_is_remembered():
    from shared import store

    key = f"reminded:{uuid.uuid4().hex}"
    assert store.get_meta(key) is None
    store.set_meta(key, "2026-11-04T09:00:00")
    assert store.get_meta(key) == "2026-11-04T09:00:00"


def test_a_seat_given_just_before_the_deadline_counts_as_confirmed():
    from shared import store
    from shared.config import now

    ev = replace(event(), confirm_deadline=(now() + timedelta(hours=3)).isoformat())
    store.update_event(ev)
    assert store.register(ev, "이름", f"{uuid.uuid4().hex[:8]}@example.com", "").confirmed_at
