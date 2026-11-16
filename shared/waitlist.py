"""Filling free seats from the waitlist, the same way wherever a free seat appears:
a cancellation (waitlist-promoter), a raised 정원 (web), or the housekeeping job's check."""

from . import bus, store
from .config import now
from .models import Event


def fill_free_seats(ev: Event) -> list:
    """Give every free seat to the next person waiting. Returns their ids.

    Runs until no seat is free, because one call may find several. Safe to repeat:
    promote_next() checks for a free seat inside a transaction. If a publish fails,
    the seat stays saved and the housekeeping job publishes it again.
    """
    if now() >= ev.starts:
        return []
    promoted = []
    while (registration_id := store.promote_next(ev)) is not None:
        promoted.append(registration_id)
        # Same message as a normal signup: ticket-worker issues the QR (and says "자리 배정")
        bus.publish("registrations", {"registration_id": registration_id})
    return promoted
