"""What a registration is, shared by both stores (SQLite locally, Firestore on Google Cloud)."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

ACTIVE = ("pending", "issued")  # statuses that hold a seat
WAITLISTED = "waitlisted"

# A seat given this close to the deadline leaves no time to answer, so it counts as confirmed
LATE_SEAT = timedelta(hours=6)
MAX_CODE_ATTEMPTS = 5  # wrong tries before a sign-in code stops working


def usual_schedule(starts: datetime, opens: datetime) -> dict:
    """The times most events use: 2.5 hours long, signups close at the start,
    attendance confirmation closes at 18:00 the day before."""
    return {
        "starts_at": starts.isoformat(),
        "ends_at": (starts + timedelta(hours=2, minutes=30)).isoformat(),
        "opens_at": opens.isoformat(),
        "closes_at": starts.isoformat(),
        "confirm_deadline": (starts - timedelta(days=1)).replace(hour=18).isoformat(),
    }


def new_ticket_code() -> str:
    """A short code a person can read out at the door, e.g. "K7Q-4XM"."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
    raw = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


@dataclass
class Registration:
    id: str
    code: str
    event_id: str
    name: str
    email: str
    affiliation: str
    status: str
    created_at: str
    issued_at: str | None
    checked_in_at: str | None
    cancelled_at: str | None
    confirmed_at: str | None = None
    promoted_at: str | None = None  # moved up from the waitlist: decides which email they get

    def door_status(self) -> str:
        """Where this ticket stands at the door. Only "ready" can be let in."""
        if self.status == "cancelled":
            return "cancelled"
        if self.checked_in_at:
            return "already"
        if self.status == WAITLISTED:
            return "waitlisted"
        return "ready" if self.status == "issued" else "not_issued"

    def public(self) -> dict:
        """What the attendee's own ticket page may see (no email)."""
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "status": self.status,
            "checked_in": self.checked_in_at is not None,
            "confirmed": self.confirmed_at is not None,
            "checked_in_at": self.checked_in_at,
            "created_at": self.created_at,
            "event_id": self.event_id,
        }


@dataclass
class Event:
    """One event. Organizers create and edit these on the admin pages.

    Times are ISO 8601 strings with the Korea offset, like the registration times.
    """

    id: str  # appears in the address: /events/{id}
    title: str
    summary: str  # one line under the title
    description: str
    venue: str
    starts_at: str
    ends_at: str
    opens_at: str  # signups open
    closes_at: str  # signups close
    confirm_deadline: str  # unconfirmed seats are released at this time
    seat_limit: int
    waitlist_limit: int
    published: bool = False  # hidden from the public until an organizer publishes it
    poster_version: str = ""  # set when a poster is uploaded; empty means the date cover
    created_at: str = ""

    @property
    def starts(self) -> datetime:
        return datetime.fromisoformat(self.starts_at)

    @property
    def ends(self) -> datetime:
        return datetime.fromisoformat(self.ends_at)

    @property
    def opens(self) -> datetime:
        return datetime.fromisoformat(self.opens_at)

    @property
    def closes(self) -> datetime:
        return datetime.fromisoformat(self.closes_at)

    @property
    def deadline(self) -> datetime:
        return datetime.fromisoformat(self.confirm_deadline)

    def signup_status(self, held: int, waiting: int) -> str | None:
        """What a new signup gets: a seat, a place in line, or None if both are full.
        A seat free while people wait is theirs."""
        if held < self.seat_limit and not waiting:
            return "pending"
        return WAITLISTED if waiting < self.waitlist_limit else None

    @property
    def confirm_cutoff(self) -> datetime:
        """A seat given from this moment on counts as confirmed."""
        return self.deadline - LATE_SEAT


class EventExists(Exception):
    """An event with this id already exists."""


class Full(Exception):
    """All seats and the whole waitlist are taken."""


class AlreadyRegistered(Exception):
    """This email already holds a seat."""
