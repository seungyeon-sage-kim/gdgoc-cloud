"""The Google Cloud side of store.py, bus.py and blob.py (BACKEND=gcp).

Same function names, same behaviour, different services underneath:

  store  → Firestore     registrations, a seat counter per event, small settings
  bus    → Pub/Sub       publish; the push subscription calls ticket-worker
  blob   → Cloud Storage the assets bucket (QR codes, posters)

A change to a registrations/ document (for example a cancellation) is itself an
event: Eventarc's Firestore trigger delivers it to waitlist-promoter.

Clients are created on first use and reused; Cloud Run gives them credentials
from the service's own identity, so there are no keys anywhere.
"""

import hashlib
import json
import logging
import os
import random
import secrets
import time
from datetime import datetime, timedelta
from functools import lru_cache

from google.api_core.exceptions import Aborted, AlreadyExists, DeadlineExceeded, NotFound
from google.cloud import firestore, pubsub_v1, storage
from google.cloud.firestore_v1 import FieldFilter, Increment

from .config import get_settings, now
from .models import (
    ACTIVE,
    MAX_CODE_ATTEMPTS,
    WAITLISTED,
    AlreadyRegistered,
    Event,
    EventExists,
    Full,
    Registration,
    new_ticket_code,
)
from .runtime import current_trace, event

PROJECT = os.environ.get("GCP_PROJECT", "")
log = logging.getLogger(__name__)


# ---------- Firestore (store) ----------
#
# registrations/{id}     one document per signup
# codes/{code}           code → id, so a door code is unique and quick to look up
# emails/{hash}          one active signup per email per event
# events/{event_id}      the event itself: title, times, seat limits
# event_counts/{id}      seat counter and status counts, updated in the same
#                        transaction as the change they count. Kept apart from
#                        events/ so an organizer editing an event never
#                        collides with the signup transactions.
#
# A cancellation is an update of a registrations/ document: Eventarc's Firestore
# trigger delivers it to waitlist-promoter, which calls promote_next().
# users/{hash}           a signed-in person's name and 소속 (hash of the email)
# login_codes/{hash}     the one outstanding sign-in code per email, as a hash
# sent_emails/{key}      emails already sent, e.g. "promoted:{registration id}"
# meta/{key}             small settings


@lru_cache
def _db() -> firestore.Client:
    return firestore.Client(project=PROJECT or None)


def _counts_ref(event_id: str):
    return _db().collection("event_counts").document(event_id)


def _email_ref(event_id: str, email: str):
    key = hashlib.sha256(f"{event_id}:{email}".encode()).hexdigest()
    return _db().collection("emails").document(key)


def _to_registration(snapshot) -> Registration | None:
    if not snapshot.exists:
        return None
    data = snapshot.to_dict()
    return Registration(**{field: data.get(field) for field in Registration.__dataclass_fields__})


def _run(attempt):
    """Run a transaction, retrying when Firestore aborts it for contention.

    During a rush every signup touches the same counter document, and Firestore
    aborts transactions that collide. Waiting a random, growing moment and trying
    again spreads them out (the backoff Firestore's documentation recommends).
    """
    fn = getattr(attempt, "to_wrap", attempt)  # @firestore.transactional keeps the function here
    where = getattr(fn, "__qualname__", "transaction").split(".")[0]  # the store function, e.g. "register"
    for n in range(20):
        try:
            # One try per transaction; the library would retry at once, without waiting
            result = attempt(_db().transaction(max_attempts=1))
            if n:  # contention: countable in Logging as event="transaction_retried"
                event(log, "transaction_retried", f"{where} committed on attempt {n + 1}", where=where, attempts=n + 1)
            return result
        except (Aborted, ValueError) as exc:
            # The library reports a failed commit as ValueError("Failed to commit ...")
            if isinstance(exc, ValueError) and not str(exc).startswith("Failed to commit"):
                raise
            if n == 19:
                event(log, "transaction_gave_up", f"{where} gave up after 20 attempts", where=where, attempts=20)
                raise
            time.sleep(random.uniform(0.05, 0.25) * 2 ** min(n, 4))


def init() -> None:
    """Nothing to create: Terraform made the database, collections appear on first write."""


# ---------- events ----------


def create_event(ev: Event) -> None:
    ev.created_at = ev.created_at or now().isoformat(timespec="seconds")
    try:
        _db().collection("events").document(ev.id).create(ev.__dict__)  # fails if the id is taken
    except AlreadyExists as exc:
        raise EventExists() from exc


def update_event(ev: Event) -> None:
    _db().collection("events").document(ev.id).set(ev.__dict__)


def _to_event(snapshot) -> Event | None:
    data = snapshot.to_dict() if snapshot.exists else None
    if not data:
        return None
    return Event(**{field: data[field] for field in Event.__dataclass_fields__ if field in data})


def get_event(event_id: str) -> Event | None:
    return _to_event(_db().collection("events").document(event_id).get())


def list_events() -> list:
    """Every event, earliest first. A club has dozens, so this reads them all."""
    return sorted((_to_event(doc) for doc in _db().collection("events").stream()), key=lambda ev: ev.starts_at)


# ---------- registrations ----------


def register(ev: Event, name: str, email: str, affiliation: str) -> Registration:
    settings = get_settings()
    email = email.strip().lower()
    db = _db()

    # The seat check and the writes happen in one transaction. If two signups
    # read the same counter at once, Firestore aborts one and retries it, so the
    # last seat can only go to one person.
    @firestore.transactional
    def attempt(tx):
        snapshot = _counts_ref(ev.id).get(transaction=tx)
        counts = (snapshot.to_dict() or {}) if snapshot.exists else {}
        held, waiting = counts.get("held", 0), counts.get(WAITLISTED, 0)
        if _email_ref(ev.id, email).get(transaction=tx).exists:
            raise AlreadyRegistered()
        status = ev.signup_status(held, waiting)
        if status is None:
            raise Full()
        code = new_ticket_code()
        while db.collection("codes").document(code).get(transaction=tx).exists:
            code = new_ticket_code()

        # Signed up at the last minute: no time left to confirm, so count it as confirmed
        confirmed = now().isoformat(timespec="seconds") if status == "pending" and now() >= ev.confirm_cutoff else None
        reg = Registration(
            id=secrets.token_urlsafe(12),
            code=code,
            event_id=ev.id,
            name=name.strip(),
            email=email,
            affiliation=affiliation.strip(),
            status=status,
            created_at=now().isoformat(timespec="microseconds"),  # exact queue order for the waitlist
            issued_at=None,
            checked_in_at=None,
            cancelled_at=None,
            confirmed_at=confirmed,
        )
        # Personal data has an end date: a Firestore TTL policy on expire_at
        # deletes these documents automatically (infra/data.tf)
        expire_at = ev.starts + timedelta(days=settings.retention_days)
        tx.set(db.collection("registrations").document(reg.id), reg.__dict__ | {"expire_at": expire_at})
        tx.set(db.collection("codes").document(code), {"id": reg.id, "expire_at": expire_at})
        tx.set(_email_ref(ev.id, email), {"id": reg.id, "expire_at": expire_at})
        if status == WAITLISTED:
            tx.set(_counts_ref(ev.id), {WAITLISTED: waiting + 1}, merge=True)
        else:
            counted = {"held": held + 1, "pending": Increment(1)}
            tx.set(_counts_ref(ev.id), counted | ({"confirmed": Increment(1)} if confirmed else {}), merge=True)
        return reg

    return _run(attempt)


def get(registration_id: str) -> Registration | None:
    return _to_registration(_db().collection("registrations").document(registration_id).get())


def get_by_code(code: str) -> Registration | None:
    found = _db().collection("codes").document(code.strip().upper()).get()
    return get(found.get("id")) if found.exists else None


def mark_issued(registration_id: str) -> bool:
    ref = _db().collection("registrations").document(registration_id)

    @firestore.transactional
    def attempt(tx) -> bool:
        reg = _to_registration(ref.get(transaction=tx))
        if reg is None or reg.status != "pending":
            return False
        tx.update(ref, {"status": "issued", "issued_at": now().isoformat(timespec="seconds")})
        tx.set(_counts_ref(reg.event_id), {"pending": Increment(-1), "issued": Increment(1)}, merge=True)
        return True

    return _run(attempt)


def cancel(registration_id: str, only_unconfirmed: bool = False) -> bool:
    ref = _db().collection("registrations").document(registration_id)

    @firestore.transactional
    def attempt(tx) -> bool:
        reg = _to_registration(ref.get(transaction=tx))
        if reg is None or reg.status not in (*ACTIVE, WAITLISTED) or reg.checked_in_at:
            return False
        if only_unconfirmed and reg.confirmed_at:
            return False  # confirmed after the deadline job listed it: keep the seat
        tx.update(ref, {"status": "cancelled", "cancelled_at": now().isoformat(timespec="seconds")})
        tx.delete(_email_ref(reg.event_id, reg.email))
        counts = {reg.status: Increment(-1), "cancelled": Increment(1)}
        if reg.status in ACTIVE:
            counts["held"] = Increment(-1)  # a seat is free: Eventarc tells waitlist-promoter
        if reg.confirmed_at:
            counts["confirmed"] = Increment(-1)
        tx.set(_counts_ref(reg.event_id), counts, merge=True)
        return True

    return _run(attempt)


def _waitlist_query(event_id: str):
    # Needs the composite index (event_id, status, created_at): infra/data.tf
    return (
        _db()
        .collection("registrations")
        .where(filter=FieldFilter("event_id", "==", event_id))
        .where(filter=FieldFilter("status", "==", WAITLISTED))
        .order_by("created_at")
    )


def promote_next(ev: Event) -> str | None:
    """Give a free seat to the first person on the waitlist. Returns their id, or None.

    Eventarc delivers at least once, so this runs more than once per cancellation.
    That is harmless: the free-seat check is inside the transaction, so a second
    run finds no free seat and changes nothing.
    """

    @firestore.transactional
    def attempt(tx) -> str | None:
        counts = _counts_ref(ev.id).get(transaction=tx).to_dict() or {}
        if counts.get("held", 0) >= ev.seat_limit:
            return None
        first = list(_waitlist_query(ev.id).limit(1).stream(transaction=tx))
        if not first:
            return None
        update = {"status": "pending", "promoted_at": now().isoformat(timespec="seconds")}
        # Promoted at the last minute: no time left to confirm, so count it as confirmed
        if now() >= ev.confirm_cutoff:
            update["confirmed_at"] = now().isoformat(timespec="seconds")
        tx.update(first[0].reference, update)
        tx.set(
            _counts_ref(ev.id),
            {
                "held": Increment(1),
                "pending": Increment(1),
                WAITLISTED: Increment(-1),
                **({"confirmed": Increment(1)} if "confirmed_at" in update else {}),
            },
            merge=True,
        )
        return first[0].id

    return _run(attempt)


def waitlist_position(registration_id: str) -> int | None:
    reg = get(registration_id)
    if reg is None or reg.status != WAITLISTED:
        return None
    ahead = _waitlist_query(reg.event_id).where(filter=FieldFilter("created_at", "<", reg.created_at)).count().get()
    return ahead[0][0].value + 1


def confirm(registration_id: str) -> str:
    """Returns 'ok', 'already' or 'invalid'."""
    ref = _db().collection("registrations").document(registration_id)

    @firestore.transactional
    def attempt(tx) -> str:
        reg = _to_registration(ref.get(transaction=tx))
        if reg is None or reg.status not in ACTIVE:
            return "invalid"
        if reg.confirmed_at:
            return "already"
        tx.update(ref, {"confirmed_at": now().isoformat(timespec="seconds")})
        tx.set(_counts_ref(reg.event_id), {"confirmed": Increment(1)}, merge=True)
        return "ok"

    return _run(attempt)


def unconfirmed(event_id: str) -> list:
    """Seats held by people who did not press 참석 확인."""
    query = (
        _db()
        .collection("registrations")
        .where(filter=FieldFilter("event_id", "==", event_id))
        .where(filter=FieldFilter("status", "in", list(ACTIVE)))
    )
    return [doc.id for doc in query.stream() if not doc.get("confirmed_at") and not doc.get("checked_in_at")]


def check_in(registration_id: str) -> str:
    ref = _db().collection("registrations").document(registration_id)

    @firestore.transactional
    def attempt(tx) -> str:
        reg = _to_registration(ref.get(transaction=tx))
        status = reg.door_status() if reg else "cancelled"
        if status != "ready":
            return status
        tx.update(ref, {"checked_in_at": now().isoformat(timespec="seconds")})
        tx.set(_counts_ref(reg.event_id), {"checked_in": Increment(1)}, merge=True)
        return "ok"

    return _run(attempt)


def undo_check_in(registration_id: str) -> bool:
    ref = _db().collection("registrations").document(registration_id)

    @firestore.transactional
    def attempt(tx) -> bool:
        reg = _to_registration(ref.get(transaction=tx))
        if reg is None or not reg.checked_in_at:
            return False
        tx.update(ref, {"checked_in_at": None})
        tx.set(_counts_ref(reg.event_id), {"checked_in": Increment(-1)}, merge=True)
        return True

    return _run(attempt)


def pending(event_id: str) -> list:
    """Seats saved but not yet turned into a ticket."""
    query = (
        _db()
        .collection("registrations")
        .where(filter=FieldFilter("event_id", "==", event_id))
        .where(filter=FieldFilter("status", "==", "pending"))
    )
    return [_to_registration(doc) for doc in query.stream()]


def stats(ev: Event) -> dict:
    # One document read: the counts are kept up to date by the transactions above
    counts = _counts_ref(ev.id).get().to_dict() or {}
    held = counts.get("held", 0)
    return {
        "seat_limit": ev.seat_limit,
        "held": held,
        "seats_free": max(ev.seat_limit - held, 0),
        "pending": counts.get("pending", 0),
        "issued": counts.get("issued", 0),
        "cancelled": counts.get("cancelled", 0),
        "waitlisted": counts.get(WAITLISTED, 0),
        "confirmed": counts.get("confirmed", 0),
        "checked_in": counts.get("checked_in", 0),
    }


def list_registrations(event_id: str) -> list:
    query = _db().collection("registrations").where(filter=FieldFilter("event_id", "==", event_id))
    regs = [_to_registration(doc) for doc in query.stream()]
    return sorted(regs, key=lambda reg: reg.created_at, reverse=True)


def registrations_for_email(email: str) -> list:
    """Everything one person signed up for, newest first (마이페이지). One equality
    filter, so Firestore's automatic single-field index is enough."""
    query = _db().collection("registrations").where(filter=FieldFilter("email", "==", email.strip().lower()))
    return sorted((_to_registration(doc) for doc in query.stream()), key=lambda reg: reg.created_at, reverse=True)


# ---------- accounts ----------


def _person_key(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


def _code_ref(email: str):
    return _db().collection("login_codes").document(_person_key(email))


def save_login_code(email: str, code_hash: str, expires_at: str) -> None:
    _code_ref(email).set(
        {
            "code_hash": code_hash,
            "expires_at": expires_at,
            "attempts": 0,
            "sent_at": now().isoformat(timespec="seconds"),
            "expire_at": datetime.fromisoformat(expires_at) + timedelta(days=1),  # TTL clean-up
        }
    )


def code_sent_at(email: str) -> str | None:
    snapshot = _code_ref(email).get()
    return snapshot.get("sent_at") if snapshot.exists else None


def use_login_code(email: str, code_hash: str) -> str:
    """Returns 'ok' (and the code is used up), 'wrong', 'expired', 'locked' or 'missing'."""
    ref = _code_ref(email)

    @firestore.transactional
    def attempt(tx) -> str:
        snapshot = ref.get(transaction=tx)
        if not snapshot.exists:
            return "missing"
        code = snapshot.to_dict()
        if code["attempts"] >= MAX_CODE_ATTEMPTS:
            return "locked"
        if now() >= datetime.fromisoformat(code["expires_at"]):
            return "expired"
        if code["code_hash"] == code_hash:
            tx.delete(ref)
            return "ok"
        tx.update(ref, {"attempts": Increment(1)})
        return "locked" if code["attempts"] + 1 >= MAX_CODE_ATTEMPTS else "wrong"

    return _run(attempt)


def get_user(email: str) -> dict | None:
    snapshot = _db().collection("users").document(_person_key(email)).get()
    return snapshot.to_dict() if snapshot.exists else None


def save_user(email: str, name: str = "", affiliation: str = "") -> None:
    ref = _db().collection("users").document(_person_key(email))
    created = (ref.get().to_dict() or {}).get("created_at") or now().isoformat(timespec="seconds")
    ref.set({"email": email, "name": name, "affiliation": affiliation, "created_at": created})


def delete_user(email: str) -> None:
    _db().collection("users").document(_person_key(email)).delete()
    _code_ref(email).delete()


def email_was_sent(key: str) -> bool:
    return _db().collection("sent_emails").document(key).get().exists


def record_email_sent(key: str) -> None:
    _db().collection("sent_emails").document(key).set(
        {"sent_at": now().isoformat(timespec="seconds"), "expire_at": now() + timedelta(days=90)}
    )


def add_dead_letter(topic: str, data: str, attempts: int, error: str) -> None:
    """Not used in the cloud: Pub/Sub moves failed messages to the dead-letter topic itself."""


def list_dead_letters() -> list:
    """Look at the dead-letter subscription without taking the messages off it."""
    subscription = os.environ.get("DEAD_LETTER_SUBSCRIPTION", "")
    if not subscription:
        return []
    subscriber = _subscriber()
    path = subscriber.subscription_path(PROJECT, subscription)
    try:
        # return_immediately: an empty queue answers "nothing" now instead of waiting
        response = subscriber.pull(
            request={"subscription": path, "max_messages": 50, "return_immediately": True}, timeout=3
        )
    except DeadlineExceeded:
        return []
    received = list(response.received_messages)
    if received:
        # Deadline 0 hands them straight back, so they stay for the next look
        subscriber.modify_ack_deadline(
            request={"subscription": path, "ack_ids": [m.ack_id for m in received], "ack_deadline_seconds": 0}
        )
    return [
        {
            "topic": m.message.attributes.get("CloudPubSubDeadLetterSourceSubscription", "registrations").rsplit(
                "/", 1
            )[-1],
            "data": m.message.data.decode(errors="replace"),
            "attempts": m.message.attributes.get("CloudPubSubDeadLetterSourceDeliveryCount", "?"),
            "error": "delivery failed",
            "created_at": m.message.attributes.get("CloudPubSubDeadLetterSourceTopicPublishTime", ""),
        }
        for m in received
    ]


def get_meta(key: str) -> str | None:
    doc = _db().collection("meta").document(key).get()
    return doc.get("value") if doc.exists else None


def set_meta(key: str, value: str) -> None:
    _db().collection("meta").document(key).set({"value": value})


# ---------- Pub/Sub (bus) ----------


@lru_cache
def _publisher() -> pubsub_v1.PublisherClient:
    return pubsub_v1.PublisherClient()


@lru_cache
def _subscriber() -> pubsub_v1.SubscriberClient:
    return pubsub_v1.SubscriberClient()


def publish(topic: str, data: dict) -> None:
    # Wait for Pub/Sub to accept the message before the request returns. Cloud Run
    # gives an instance CPU only while it handles a request, so a background
    # thread started here could stall after the response is sent.
    path = _publisher().topic_path(PROJECT, topic)
    # The trace rides along as an attribute, so ticket-worker's logs join this request's trace
    attributes = {"trace": current_trace()} if current_trace() else {}
    _publisher().publish(path, json.dumps(data).encode(), **attributes).result(timeout=30)


# ---------- Cloud Storage (blob) ----------


@lru_cache
def _storage() -> storage.Client:
    return storage.Client(project=PROJECT or None)


def _blob(name: str):
    bucket = os.environ["ASSETS_BUCKET"]
    return _storage().bucket(bucket).blob(name)


def put(name: str, data: bytes) -> None:
    from .blob import content_type

    _blob(name).upload_from_string(data, content_type=content_type(name))


def get_blob(name: str) -> bytes | None:
    try:
        return _blob(name).download_as_bytes()
    except NotFound:
        return None


def exists(name: str) -> bool:
    return _blob(name).exists()
