"""Data store: events, registrations, check-ins, dead letters, small settings.

Locally this is SQLite in .data/app.db. On Google Cloud the same functions are
backed by Firestore; the rest of the code only calls these functions, so it
never knows which one it is talking to.

The important part is register(): the seat check and the insert happen inside
one transaction, so two people can never take the last seat at the same time.
When the seats are full, people join a waitlist instead. A cancelled seat goes
to the first person waiting: on Google Cloud, Firestore reports the change and
Eventarc calls waitlist-promoter; locally, cancel() sends the same event itself.
While anyone is waiting, a new signup queues behind them, even if a seat has just
come free: that seat is already theirs.
"""

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime

from .config import get_settings, now
from .models import (  # noqa: F401
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    venue            TEXT NOT NULL,
    starts_at        TEXT NOT NULL,
    ends_at          TEXT NOT NULL,
    opens_at         TEXT NOT NULL,
    closes_at        TEXT NOT NULL,
    confirm_deadline TEXT NOT NULL,
    seat_limit       INTEGER NOT NULL,
    waitlist_limit   INTEGER NOT NULL,
    published        INTEGER NOT NULL DEFAULT 0,
    poster_version   TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registrations (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    event_id      TEXT NOT NULL,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    affiliation   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,            -- waitlisted | pending | issued | cancelled
    created_at    TEXT NOT NULL,
    issued_at     TEXT,
    checked_in_at TEXT,
    cancelled_at  TEXT,
    confirmed_at  TEXT,                     -- pressed "참석 확인" before the deadline
    promoted_at   TEXT                      -- moved up from the waitlist
);
-- One active registration per email per event
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_email
    ON registrations (event_id, email) WHERE status != 'cancelled';

-- People who signed in: what prefills their signups
CREATE TABLE IF NOT EXISTS users (
    email       TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    affiliation TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- The one sign-in code each email may have outstanding, as a hash
CREATE TABLE IF NOT EXISTS login_codes (
    email      TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS registrations_by_email ON registrations (email, created_at);

-- One row per email already sent for a registration, e.g. "promoted:abc123"
CREATE TABLE IF NOT EXISTS sent_emails (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL,
    data       TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    error      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

EVENT_FIELDS = [f.name for f in fields(Event)]


def _db_path():
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "app.db"


@contextmanager
def _connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if write:
            # BEGIN IMMEDIATE takes the write lock now, so the seat count we read
            # cannot change before we insert. This is the transaction.
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _row(row: sqlite3.Row | None) -> Registration | None:
    return Registration(**dict(row)) if row else None


def _event(row: sqlite3.Row | None) -> Event | None:
    if row is None:
        return None
    data = dict(row)
    data["published"] = bool(data["published"])
    return Event(**data)


# ---------- events ----------


def create_event(ev: Event) -> None:
    ev.created_at = ev.created_at or now().isoformat(timespec="seconds")
    with _connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM events WHERE id = ?", (ev.id,)).fetchone():
            raise EventExists()
        columns = ", ".join(EVENT_FIELDS)
        conn.execute(
            f"INSERT INTO events ({columns}) VALUES ({', '.join(':' + f for f in EVENT_FIELDS)})",
            ev.__dict__ | {"published": int(ev.published)},
        )


def update_event(ev: Event) -> None:
    with _connect(write=True) as conn:
        assignments = ", ".join(f"{f} = :{f}" for f in EVENT_FIELDS if f != "id")
        conn.execute(f"UPDATE events SET {assignments} WHERE id = :id", ev.__dict__ | {"published": int(ev.published)})


def get_event(event_id: str) -> Event | None:
    with _connect() as conn:
        return _event(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())


def list_events() -> list:
    """Every event, published or not, earliest first. A club has dozens, not thousands."""
    with _connect() as conn:
        return [_event(r) for r in conn.execute("SELECT * FROM events ORDER BY starts_at").fetchall()]


# ---------- registrations ----------


def _new_code(conn: sqlite3.Connection) -> str:
    while True:
        code = new_ticket_code()
        if not conn.execute("SELECT 1 FROM registrations WHERE code = ?", (code,)).fetchone():
            return code


def register(ev: Event, name: str, email: str, affiliation: str) -> Registration:
    email = email.strip().lower()
    with _connect(write=True) as conn:
        taken = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? AND status IN (?, ?)",
            (ev.id, *ACTIVE),
        ).fetchone()[0]
        existing = conn.execute(
            "SELECT 1 FROM registrations WHERE event_id = ? AND email = ? AND status != 'cancelled'",
            (ev.id, email),
        ).fetchone()
        if existing:
            raise AlreadyRegistered()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? AND status = ?",
            (ev.id, WAITLISTED),
        ).fetchone()[0]
        status = ev.signup_status(taken, waiting)
        if status is None:
            raise Full()
        # Signed up at the last minute: no time left to confirm, so count it as confirmed
        confirmed = now().isoformat(timespec="seconds") if status == "pending" and now() >= ev.confirm_cutoff else None
        reg = Registration(
            id=secrets.token_urlsafe(12),
            code=_new_code(conn),
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
        conn.execute(
            "INSERT INTO registrations (id, code, event_id, name, email, affiliation, status,"
            " created_at, issued_at, checked_in_at, cancelled_at, confirmed_at, promoted_at)"
            " VALUES (:id, :code, :event_id, :name, :email, :affiliation, :status,"
            " :created_at, :issued_at, :checked_in_at, :cancelled_at, :confirmed_at, :promoted_at)",
            reg.__dict__,
        )
        return reg


def get(registration_id: str) -> Registration | None:
    with _connect() as conn:
        return _row(conn.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,)).fetchone())


def get_by_code(code: str) -> Registration | None:
    with _connect() as conn:
        return _row(conn.execute("SELECT * FROM registrations WHERE code = ?", (code.strip().upper(),)).fetchone())


def mark_issued(registration_id: str) -> bool:
    """Returns False if it was already issued (or cancelled). Safe to call twice."""
    with _connect(write=True) as conn:
        cur = conn.execute(
            "UPDATE registrations SET status = 'issued', issued_at = ? WHERE id = ? AND status = 'pending'",
            (now().isoformat(timespec="seconds"), registration_id),
        )
        return cur.rowcount == 1


def cancel(registration_id: str, only_unconfirmed: bool = False) -> bool:
    """only_unconfirmed: the deadline job's cancel, which must not release a seat
    whose owner confirmed after the job listed it."""
    with _connect(write=True) as conn:
        row = conn.execute("SELECT status FROM registrations WHERE id = ?", (registration_id,)).fetchone()
        cur = conn.execute(
            "UPDATE registrations SET status = 'cancelled', cancelled_at = ?"
            " WHERE id = ? AND status IN (?, ?, ?) AND checked_in_at IS NULL"
            + (" AND confirmed_at IS NULL" if only_unconfirmed else ""),
            (now().isoformat(timespec="seconds"), registration_id, *ACTIVE, WAITLISTED),
        )
        changed = cur.rowcount == 1
    if changed:
        _document_updated(registration_id, row["status"], "cancelled")
    return changed


def _document_updated(registration_id: str, old_status: str, new_status: str) -> None:
    """Local stand-in for Eventarc's Firestore trigger: the same protobuf event it delivers."""
    from google.events.cloud.firestore_v1 import Document, DocumentEventData, Value

    from .bus import deliver_in_background

    def document(status):
        return Document(name=f"registrations/{registration_id}", fields={"status": Value(string_value=status)})

    body = DocumentEventData.serialize(DocumentEventData(old_value=document(old_status), value=document(new_status)))
    deliver_in_background(
        get_settings().waitlist_promoter_url,
        body,
        {"content-type": "application/protobuf"},
        f"document.updated {registration_id}",
    )


def promote_next(ev: Event) -> str | None:
    """Give a free seat to the first person on the waitlist. Returns their id, or None.

    Safe to run twice for the same cancellation: the seat check is inside the
    transaction, so the second run finds no free seat and does nothing.
    """
    with _connect(write=True) as conn:
        taken = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? AND status IN (?, ?)",
            (ev.id, *ACTIVE),
        ).fetchone()[0]
        if taken >= ev.seat_limit:
            return None
        row = conn.execute(
            "SELECT id FROM registrations WHERE event_id = ? AND status = ? ORDER BY created_at, rowid LIMIT 1",
            (ev.id, WAITLISTED),
        ).fetchone()
        if row is None:
            return None
        # Promoted at the last minute: no time left to confirm, so count it as confirmed
        confirmed = now().isoformat(timespec="seconds") if now() >= ev.confirm_cutoff else None
        conn.execute(
            "UPDATE registrations SET status = 'pending', confirmed_at = ?, promoted_at = ? WHERE id = ?",
            (confirmed, now().isoformat(timespec="seconds"), row["id"]),
        )
        return row["id"]


def waitlist_position(registration_id: str) -> int | None:
    with _connect() as conn:
        reg = conn.execute(
            "SELECT event_id, status, created_at FROM registrations WHERE id = ?", (registration_id,)
        ).fetchone()
        if reg is None or reg["status"] != WAITLISTED:
            return None
        ahead = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? AND status = ? AND created_at < ?",
            (reg["event_id"], WAITLISTED, reg["created_at"]),
        ).fetchone()[0]
        return ahead + 1


def confirm(registration_id: str) -> str:
    """Returns 'ok', 'already' or 'invalid'."""
    with _connect(write=True) as conn:
        row = conn.execute("SELECT status, confirmed_at FROM registrations WHERE id = ?", (registration_id,)).fetchone()
        if row is None or row["status"] not in ACTIVE:
            return "invalid"
        if row["confirmed_at"]:
            return "already"
        conn.execute(
            "UPDATE registrations SET confirmed_at = ? WHERE id = ?",
            (now().isoformat(timespec="seconds"), registration_id),
        )
        return "ok"


def unconfirmed(event_id: str) -> list:
    """Seats held by people who did not press 참석 확인."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM registrations WHERE event_id = ? AND status IN (?, ?)"
            " AND confirmed_at IS NULL AND checked_in_at IS NULL",
            (event_id, *ACTIVE),
        ).fetchall()
    return [r["id"] for r in rows]


def check_in(registration_id: str) -> str:
    """Returns 'ok', or why not (Registration.door_status). One atomic update."""
    with _connect(write=True) as conn:
        reg = _row(conn.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,)).fetchone())
        status = reg.door_status() if reg else "cancelled"
        if status != "ready":
            return status
        conn.execute(
            "UPDATE registrations SET checked_in_at = ? WHERE id = ?",
            (now().isoformat(timespec="seconds"), registration_id),
        )
        return "ok"


def undo_check_in(registration_id: str) -> bool:
    """입장 취소: staff let in the wrong ticket. Returns False if it had not been checked in."""
    with _connect(write=True) as conn:
        cur = conn.execute(
            "UPDATE registrations SET checked_in_at = NULL WHERE id = ? AND checked_in_at IS NOT NULL",
            (registration_id,),
        )
        return cur.rowcount == 1


def pending(event_id: str) -> list:
    """Seats saved but not yet turned into a ticket."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM registrations WHERE event_id = ? AND status = 'pending'", (event_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def stats(ev: Event) -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, SUM(checked_in_at IS NOT NULL) AS checked"
            " FROM registrations WHERE event_id = ? GROUP BY status",
            (ev.id,),
        ).fetchall()
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? AND status IN (?, ?) AND confirmed_at IS NOT NULL",
            (ev.id, *ACTIVE),
        ).fetchone()[0]
    by_status = {r["status"]: r["n"] for r in rows}
    held = by_status.get("pending", 0) + by_status.get("issued", 0)
    return {
        "seat_limit": ev.seat_limit,
        "held": held,
        "seats_free": max(ev.seat_limit - held, 0),
        "pending": by_status.get("pending", 0),
        "issued": by_status.get("issued", 0),
        "cancelled": by_status.get("cancelled", 0),
        "waitlisted": by_status.get(WAITLISTED, 0),
        "confirmed": confirmed,
        "checked_in": sum(r["checked"] or 0 for r in rows),
    }


def list_registrations(event_id: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM registrations WHERE event_id = ? ORDER BY created_at DESC", (event_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def registrations_for_email(email: str) -> list:
    """Everything one person signed up for, newest first (마이페이지)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM registrations WHERE email = ? ORDER BY created_at DESC", (email.strip().lower(),)
        ).fetchall()
    return [_row(r) for r in rows]


# ---------- accounts ----------


def save_login_code(email: str, code_hash: str, expires_at: str) -> None:
    """Replace this email's sign-in code. Any earlier code stops working."""
    with _connect(write=True) as conn:
        conn.execute(
            "INSERT INTO login_codes (email, code_hash, expires_at, attempts, sent_at) VALUES (?, ?, ?, 0, ?)"
            " ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash, expires_at = excluded.expires_at,"
            " attempts = 0, sent_at = excluded.sent_at",
            (email, code_hash, expires_at, now().isoformat(timespec="seconds")),
        )


def code_sent_at(email: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT sent_at FROM login_codes WHERE email = ?", (email,)).fetchone()
        return row["sent_at"] if row else None


def use_login_code(email: str, code_hash: str) -> str:
    """Returns 'ok' (and the code is used up), 'wrong', 'expired', 'locked' or 'missing'.

    Every wrong try counts, so a 6-digit code cannot be found by trying them all.
    """
    with _connect(write=True) as conn:
        row = conn.execute("SELECT * FROM login_codes WHERE email = ?", (email,)).fetchone()
        if row is None:
            return "missing"
        if row["attempts"] >= MAX_CODE_ATTEMPTS:
            return "locked"
        if now() >= datetime.fromisoformat(row["expires_at"]):
            return "expired"
        if row["code_hash"] == code_hash:
            conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
            return "ok"
        conn.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
        return "locked" if row["attempts"] + 1 >= MAX_CODE_ATTEMPTS else "wrong"


def get_user(email: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def save_user(email: str, name: str = "", affiliation: str = "") -> None:
    with _connect(write=True) as conn:
        conn.execute(
            "INSERT INTO users (email, name, affiliation, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(email) DO UPDATE SET name = excluded.name, affiliation = excluded.affiliation",
            (email, name, affiliation, now().isoformat(timespec="seconds")),
        )


def delete_user(email: str) -> None:
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))


def email_was_sent(key: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM sent_emails WHERE key = ?", (key,)).fetchone() is not None


def record_email_sent(key: str) -> None:
    """Pub/Sub delivers at least once, so the mail sender records "kind:registration" and skips repeats."""
    with _connect(write=True) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_emails (key, sent_at) VALUES (?, ?)", (key, now().isoformat(timespec="seconds"))
        )


def add_dead_letter(topic: str, data: str, attempts: int, error: str) -> None:
    with _connect(write=True) as conn:
        conn.execute(
            "INSERT INTO dead_letters (topic, data, attempts, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (topic, data, attempts, error, now().isoformat(timespec="seconds")),
        )


def list_dead_letters() -> list:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM dead_letters ORDER BY id DESC LIMIT 50").fetchall()]


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _connect(write=True) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# On Google Cloud the same functions run against Firestore (shared/gcp.py)
if os.environ.get("BACKEND") == "gcp":
    from .gcp import (  # noqa: E402, F401, F811
        add_dead_letter,
        cancel,
        check_in,
        code_sent_at,
        confirm,
        create_event,
        delete_user,
        email_was_sent,
        get,
        get_by_code,
        get_event,
        get_meta,
        get_user,
        init,
        list_dead_letters,
        list_events,
        list_registrations,
        mark_issued,
        pending,
        promote_next,
        record_email_sent,
        register,
        registrations_for_email,
        save_login_code,
        save_user,
        set_meta,
        stats,
        unconfirmed,
        undo_check_in,
        update_event,
        use_login_code,
        waitlist_position,
    )
