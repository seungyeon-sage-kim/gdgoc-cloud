"""waitlist-promoter: a private service started by Firestore changes (Eventarc).

When a registration document changes, Firestore reports "document updated" and
an Eventarc trigger delivers it here as a CloudEvent. If a seat-holding
registration became cancelled (the person cancelled, or the housekeeping job
released it), the first person on the waitlist gets the seat and a
"registered" message goes to Pub/Sub, so ticket-worker issues their QR.

Eventarc delivers at least once and every update of a registration arrives
here, so this handler:
  - ignores changes that did not free a seat
  - is idempotent: promote_next() checks for a free seat inside a transaction,
    so a duplicate event finds nothing to do
  - answers 2xx only after the work is done; an error makes Eventarc retry

A seat given out here whose message then fails to publish is not lost: the
housekeeping job publishes it again (jobs/housekeeping.py).
"""

import logging

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from google.events.cloud.firestore_v1 import DocumentEventData

from shared import store, waitlist
from shared.runtime import event, setup

log = logging.getLogger("waitlist-promoter")

app = FastAPI(title="waitlist-promoter", docs_url=None, redoc_url=None)
setup(app, "waitlist-promoter")


def statuses(body: bytes) -> tuple:
    """(status before, status after, document name) from a Firestore "document updated" event.
    Eventarc sends these as protobuf, decoded with Google's google-events library."""
    event = DocumentEventData.deserialize(body)

    def status(doc):
        return doc.fields["status"].string_value if "status" in doc.fields else ""

    return status(event.old_value), status(event.value), event.value.name


@app.post("/")
async def handle_event(request: Request):
    before, after, name = statuses(await request.body())
    # The work below is blocking (Firestore, Pub/Sub), so run it off the event loop
    return await run_in_threadpool(promote, before, after, name)


def promote(before: str, after: str, subject: str) -> Response:
    """subject: the changed document, ".../documents/registrations/{id}"."""
    if not (before in store.ACTIVE and after == "cancelled"):
        return Response(status_code=204)  # not a freed seat: nothing to do
    cancelled = store.get(subject.rsplit("/", 1)[-1])
    ev = store.get_event(cancelled.event_id) if cancelled else None
    if ev is None:
        log.warning("no event for %s; nothing to promote", subject)
        return Response(status_code=204)

    promoted = waitlist.fill_free_seats(ev)
    event(
        log,
        "promotion",
        f"seat freed ({subject}): promoted {promoted or 'nobody (no free seat or empty waitlist)'}",
        promoted=len(promoted),
        event_id=ev.id,
        waitlisted=store.stats(ev)["waitlisted"],
    )
    return Response(status_code=204)
