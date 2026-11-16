"""ticket-worker: a private service that receives Pub/Sub push messages.

  /       "registrations" topic: sign a ticket token, draw the QR code, save it
          to storage and mark the registration as issued
  /mail   "emails" topic: send one email (shared/mail.py)

Rules this file follows, because Pub/Sub delivers at least once:
  - return 2xx only when the work is done; any error makes Pub/Sub retry
  - doing the same message twice must be harmless (idempotent)

On Cloud Run this service is private: only the Pub/Sub push subscription's
service account has permission (roles/run.invoker) to call it.
"""

import logging
import random
import time

from fastapi import Body, FastAPI, Response

from shared import blob, bus, mail, store, tokens
from shared.config import get_settings
from shared.runtime import event, reset_trace, setup, use_trace

log = logging.getLogger("ticket-worker")

app = FastAPI(title="ticket-worker", docs_url=None, redoc_url=None)
setup(app, "ticket-worker", secrets=("TICKET_SECRET",))


@app.post("/")
def handle_push(envelope: dict = Body(...)):
    # Log under the trace of the signup request that published this message
    token = use_trace(bus.trace_of(envelope))
    try:
        return issue(envelope)
    finally:
        reset_trace(token)


def issue(envelope: dict):
    settings = get_settings()
    data = bus.read_envelope(envelope)
    registration_id = data["registration_id"]

    reg = store.get(registration_id)
    if reg is None or reg.status != "pending":
        # Already issued, cancelled, or unknown: nothing to do. Answer 2xx so
        # Pub/Sub stops redelivering. This is what makes the handler idempotent.
        log.info("skip %s (status %s)", registration_id, reg.status if reg else "missing")
        return Response(status_code=204)

    # Stand-ins for real work (an email provider call, for example) so the
    # "issuing" state is visible locally, plus an optional failure rate to
    # watch retries and the dead-letter list.
    time.sleep(settings.worker_delay_ms / 1000)
    if random.random() < settings.worker_fail_rate:
        log.warning("simulated failure for %s", registration_id)
        return Response("simulated failure", status_code=500)

    blob.put(f"tickets/{registration_id}.svg", tokens.ticket_qr(reg.event_id, registration_id))
    # Ask for the email first. If that fails, the 5xx brings this message back; had the
    # ticket been marked issued already, the retry would skip it and nobody would be
    # told. A repeated request is harmless: mail.send sends each email once.
    # A person moved up from the waitlist gets "자리 배정", everyone else "신청 완료"
    kind = "promoted" if reg.promoted_at else "ticket_issued"
    if not mail.request(kind, reg.email, registration_id=registration_id):
        return Response("could not queue the email", status_code=500)
    if store.mark_issued(registration_id):  # False for a duplicate delivery: count each ticket once
        event(log, "ticket_issued", f"issued ticket {reg.code} ({registration_id})")
    return Response(status_code=204)


@app.post("/mail")
def handle_mail(envelope: dict = Body(...)):
    token = use_trace(bus.trace_of(envelope))
    try:
        message = bus.read_envelope(envelope)
        try:
            mail.send(message)
        except Exception:
            # Answer 5xx: Pub/Sub delivers it again later, then dead-letters it
            log.exception("could not send %s email", message.get("kind"))
            return Response("send failed", status_code=500)
        return Response(status_code=204)
    finally:
        reset_trace(token)
