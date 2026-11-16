"""Messages between services (Pub/Sub).

publish("registrations", {...}) is all the web service calls. On Google Cloud that
puts the message on a Pub/Sub topic, and a push subscription POSTs it to the
ticket worker. Locally there is no Pub/Sub, so this file does the push itself,
with the same request format and the same habits:

  - delivery happens in the background (the signup request does not wait)
  - a failed delivery is retried with growing waits
  - after the last attempt the message goes to a dead-letter list
  - a message can arrive more than once, so receivers must be idempotent
"""

import base64
import json
import logging
import os
import secrets
import threading
import time

import httpx

from . import store
from .config import get_settings, now
from .runtime import current_trace

log = logging.getLogger("bus")

TOPIC_PATHS = {"registrations": "/", "emails": "/mail"}

# Local deliveries run in background threads. A web service lives on, but a job
# exits when its work is done, so it calls wait_for_deliveries() first.
_pending: list = []
_pending_lock = threading.Lock()

MAX_ATTEMPTS = 5
FIRST_WAIT_SECONDS = 1.0  # then 2, 4, 8 (Pub/Sub's retry policy uses longer waits)


def deliver_with_retries(url: str, body: bytes, headers: dict, label: str, on_give_up=None) -> None:
    """POST until the receiver answers 2xx, retrying with exponential backoff."""
    wait = FIRST_WAIT_SECONDS
    error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(url, content=body, headers=headers, timeout=30)
            if response.is_success:
                log.info("%s delivered (attempt %d)", label, attempt)
                return
            error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        log.warning("%s failed (attempt %d/%d): %s", label, attempt, MAX_ATTEMPTS, error)
        if attempt < MAX_ATTEMPTS:
            time.sleep(wait)
            wait *= 2
    log.error("%s gave up after %d attempts", label, MAX_ATTEMPTS)
    if on_give_up:
        on_give_up(MAX_ATTEMPTS, error)


def deliver_in_background(*args) -> None:
    """deliver_with_retries(*args) in a thread that wait_for_deliveries() can wait for."""
    thread = threading.Thread(target=deliver_with_retries, args=args, daemon=True)
    with _pending_lock:
        _pending[:] = [t for t in _pending if t.is_alive()]
        _pending.append(thread)
    thread.start()


def wait_for_deliveries(timeout: float = 120) -> None:
    """Block until local background deliveries finish (on Google Cloud there are none:
    publishing already waits for Pub/Sub)."""
    deadline = time.monotonic() + timeout
    with _pending_lock:
        threads = list(_pending)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))


def push_envelope(data: dict, message_id: str | None = None, attributes: dict | None = None) -> dict:
    """The exact JSON body a Pub/Sub push subscription sends."""
    return {
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id or secrets.token_hex(8),
            "publishTime": now().isoformat(),
            "attributes": attributes or {},
        },
        "subscription": "projects/local/subscriptions/ticket-worker",
    }


def read_envelope(body: dict) -> dict:
    """Unwrap a push request back into the published data."""
    return json.loads(base64.b64decode(body["message"]["data"]))


def trace_of(body: dict) -> str:
    """The trace of the request that published this message (empty if none)."""
    return (body.get("message", {}).get("attributes") or {}).get("trace", "")


def publish(topic: str, data: dict) -> None:
    settings = get_settings()
    envelope = push_envelope(data, attributes={"trace": current_trace()} if current_trace() else {})
    label = f"{topic} message {envelope['message']['messageId']}"

    def give_up(attempts: int, error: str) -> None:
        store.add_dead_letter(topic, json.dumps(data), attempts, error)

    # Each topic has its own push subscription; locally that is a path on ticket-worker
    url = settings.push_endpoint.rstrip("/") + TOPIC_PATHS.get(topic, "/")
    deliver_in_background(url, json.dumps(envelope).encode(), {"content-type": "application/json"}, label, give_up)


# On Google Cloud, publish puts the message on the real Pub/Sub topic (shared/gcp.py)
if os.environ.get("BACKEND") == "gcp":
    from .gcp import publish  # noqa: E402, F401, F811
