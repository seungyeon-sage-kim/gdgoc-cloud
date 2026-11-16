"""housekeeping: a Cloud Run job that Cloud Scheduler starts every 5 minutes.

With many events there is no single opening time or deadline to schedule. So
instead of one Scheduler job per date, one job runs often and does whatever is
due for each event. Every step is safe to repeat and raises on trouble. A step
that fails holds up nothing else, the next run tries it again, and the run exits
as failed so someone hears about it ("reconcile on a timer"):

  pre-warm  from PREWARM_MINUTES_BEFORE before any event's signups open until
            PREWARM_MINUTES_AFTER after, keep PREWARM_INSTANCES web instances
            warm (Cloud Run Admin API), then go back to WEB_MIN_INSTANCES. The
            opening rush meets warm instances instead of ~6 s cold starts (docs/cold-start.md).
  remind    from a day before the confirmation deadline, email 참석 확인 요청 to
            everyone who has not confirmed (once per event)
  release   once the deadline has passed and the event has not started, cancel
            the seats nobody confirmed. Each cancellation is a Firestore update,
            so Eventarc hands the seat to waitlist-promoter, and the person gets
            자리 반환 안내.
  repair    catch what an event-driven step missed. A seat free while people wait
            (an Eventarc delivery that never arrived) goes to the next in line. A
            seat saved but never turned into a ticket (a publish that failed, a
            message that ran out of retries) is published again.

It has no URL: it runs, prints what it did, and exits.

Run locally:  python -m jobs.housekeeping                  (pre-warm only prints)
              python -m jobs.housekeeping --force-release  (release every event now, for a demo)
              python -m jobs.housekeeping --check          (read what the steps read, change nothing)

A deploy tries a new image with --check (scripts/deploy.sh), so trying it never sends
reminders or releases seats. The next Scheduler run does the real work.
"""

import logging
import os
import sys
from datetime import datetime, timedelta

from shared import bus, mail, store, waitlist
from shared.config import now
from shared.runtime import configure_logging, event

log = logging.getLogger("housekeeping")

REMIND_BEFORE = timedelta(hours=24)
STUCK_AFTER = timedelta(minutes=5)  # a ticket normally takes seconds; a duplicate message is harmless


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


def prewarm(events: list, apply: bool = True) -> None:
    """Keep web warm around any event's opening time; otherwise back to its usual minimum."""
    before = timedelta(minutes=_env_int("PREWARM_MINUTES_BEFORE", 15))
    after = timedelta(minutes=_env_int("PREWARM_MINUTES_AFTER", 60))
    opening = [ev for ev in events if ev.published and ev.opens - before <= now() < ev.opens + after]
    usual = _env_int("WEB_MIN_INSTANCES", 0)
    wanted = max(_env_int("PREWARM_INSTANCES", 3), usual) if opening else usual

    service = os.environ.get("WEB_SERVICE", "")  # projects/…/locations/…/services/web
    if not service:
        print(f"pre-warm: web should keep {wanted} instances warm (WEB_SERVICE not set: nothing changed)")
        return
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    url = f"https://run.googleapis.com/v2/{service}"
    answer = session.get(url, timeout=30)
    answer.raise_for_status()  # a refused read must not look like a minimum of 0
    current = answer.json().get("scaling", {}).get("minInstanceCount", 0)
    if current == wanted:
        return
    if not apply:
        print(f"pre-warm: web minimum instances would go {current} → {wanted}")
        return
    # The service-level minimum, changed without a new revision. Terraform ignores
    # this field (run.tf), so the change is not drift.
    response = session.patch(
        url, params={"updateMask": "scaling.minInstanceCount"}, json={"scaling": {"minInstanceCount": wanted}}
    )
    response.raise_for_status()
    names = ", ".join(ev.id for ev in opening) or "none opening"
    print(f"pre-warm: web minimum instances {current} → {wanted} ({names})")
    event(log, "prewarm", f"web minimum instances {current} → {wanted}", instances=wanted)


def remind(ev) -> None:
    """참석 확인 요청, once per event, to everyone holding a seat they have not confirmed.
    With email off nobody can be told, so this never counts as done and release leaves the seats."""
    in_window = ev.deadline - REMIND_BEFORE <= now() < ev.deadline
    if not in_window or not mail.enabled() or store.get_meta(f"reminded:{ev.id}"):
        return
    ids = store.unconfirmed(ev.id)
    queued = [mail.request("confirm_reminder", store.get(i).email, registration_id=i) for i in ids]
    if not all(queued):
        # Not marked as done: the next run asks again, and mail.send drops the repeats
        raise RuntimeError(f"{ev.id}: could not queue {queued.count(False)} of {len(ids)} reminders")
    store.set_meta(f"reminded:{ev.id}", now().isoformat(timespec="seconds"))
    print(f"{ev.id}: asked {len(ids)} people to confirm")
    event(log, "reminder", f"{ev.id}: asked {len(ids)} people to confirm", reminded=len(ids), event_id=ev.id)


def release(ev, force: bool = False) -> None:
    """After the deadline (before the start), cancel seats nobody confirmed."""
    if not (force or ev.deadline <= now() < ev.starts):
        return
    ids = store.unconfirmed(ev.id)
    # A cancelled ticket cannot be restored, so nobody loses a seat without having been
    # asked. If the job was down for the whole reminder window, the seats stay.
    if ids and not force and not store.get_meta(f"reminded:{ev.id}"):
        # An error in the log, not a failed run: the job has nothing to fix, and a failed
        # run would make every deploy roll back until the event starts
        log.error("%s: keeping %d unconfirmed seats: the reminder never went out", ev.id, len(ids))
        return
    released = 0
    for registration_id in ids:
        # only_unconfirmed: someone may confirm between listing and cancelling
        if store.cancel(registration_id, only_unconfirmed=True):
            released += 1
            mail.request("released", store.get(registration_id).email, registration_id=registration_id)
    if released:
        print(f"{ev.id}: released {released} unconfirmed seats; waitlist-promoter fills them")
        event(log, "release", f"{ev.id}: released {released} unconfirmed seats", released=released, event_id=ev.id)


def repair(ev) -> None:
    """Finish what an event-driven step left undone. Safe to repeat, like the rest."""
    promoted = waitlist.fill_free_seats(ev)
    if promoted:
        print(f"{ev.id}: gave {len(promoted)} free seats to people waiting")
        event(log, "promotion", f"{ev.id}: job promoted {len(promoted)}", promoted=len(promoted), event_id=ev.id)
    if now() >= ev.ends:
        return
    # The email kind comes from the registration, so a republished message loses nothing
    stuck = [
        r for r in store.pending(ev.id) if now() - datetime.fromisoformat(r.promoted_at or r.created_at) >= STUCK_AFTER
    ]
    for reg in stuck:
        bus.publish("registrations", {"registration_id": reg.id})
    if stuck:
        print(f"{ev.id}: published {len(stuck)} stuck tickets again")
        event(log, "requeue", f"{ev.id}: published {len(stuck)} stuck tickets again", stuck=len(stuck), event_id=ev.id)


def check(events: list) -> None:
    """Make the reads every step makes: settings, events, web's minimum, each event's
    seats. A broken image, setting or permission fails here; nothing is written."""
    prewarm(events, apply=False)
    for ev in events:
        store.unconfirmed(ev.id)
        store.pending(ev.id)
    print(f"check: read {len(events)} events, changed nothing")


def attempt(label: str, step, *args) -> bool:
    """Run one step. Its failure is logged and holds up nothing else."""
    try:
        step(*args)
        return True
    except Exception:
        log.exception("%s failed", label)
        return False


def main() -> int:
    configure_logging("housekeeping")
    store.init()
    events = store.list_events()
    if "--check" in sys.argv:
        return 0 if attempt("check", check, events) else 1
    force = "--force-release" in sys.argv

    results = [attempt("pre-warm", prewarm, events)]
    for ev in events:
        if ev.published:
            results.append(attempt(f"remind {ev.id}", remind, ev))
        results.append(attempt(f"release {ev.id}", release, ev, force))
        results.append(attempt(f"repair {ev.id}", repair, ev))
    # Locally, emails and the "document updated" event leave in background
    # threads; exiting now would drop them (no waitlist promotion, no email)
    bus.wait_for_deliveries()
    # A failed run is what the alert looks for (monitoring.tf)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
