"""Abuse protection for the signup API: a rate limit and a bot check.

Without these, one script could take every seat in a second. Two layers:

  1. Rate limit: at most N signups per minute from one IP address, counted in
     each instance's memory. Cheap and good enough against a single script; a
     shared counter (Firestore, Memorystore) would also cover many instances.
  2. reCAPTCHA Enterprise (score-based, invisible): the page gets a token from
     Google, and the server asks Google how likely the visitor is a human.
     Local runs have no site key, so the check is skipped.
"""

import logging
import os
import threading
import time
from collections import defaultdict, deque

from .config import get_settings

log = logging.getLogger("guard")

# reCAPTCHA scores run from 0.0 (bot) to 1.0 (human). Low on purpose: a wrong refusal
# costs a real person their seat, and a bot that slips through still meets the rate limit.
MIN_SCORE = 0.3

_hits: dict = defaultdict(deque)
_lock = threading.Lock()


def client_ip(headers, fallback: str) -> str:
    # Google's front end appends the address it saw to X-Forwarded-For, so the
    # last entry is the real one; anything before it was sent by the client and
    # could be made up to dodge the limit
    forwarded = headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or fallback


CODES_PER_MINUTE_PER_IP = 5  # sign-in code emails one address may ask for
GOOGLE_SIGN_INS_PER_MINUTE_PER_IP = 30  # no email is sent; a campus shares one address


def allow(ip: str, kind: str = "signup", limit: int | None = None) -> bool:
    """Sliding one-minute window per IP address and kind (signup, login)."""
    limit = limit or get_settings().signups_per_minute_per_ip
    now = time.monotonic()
    with _lock:
        hits = _hits[(kind, ip)]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        if len(_hits) > 10_000:  # forget idle addresses so memory stays small
            for key in [k for k, v in _hits.items() if not v]:
                del _hits[key]
        return True


def human(token: str, action: str) -> bool:
    """Ask reCAPTCHA Enterprise whether this token came from a real visitor."""
    site_key = get_settings().recaptcha_site_key
    if not site_key:
        return True
    if not token:
        return False

    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    project = os.environ.get("GCP_PROJECT") or project
    try:
        response = AuthorizedSession(credentials).post(
            f"https://recaptchaenterprise.googleapis.com/v1/projects/{project}/assessments",
            json={"event": {"token": token, "siteKey": site_key, "expectedAction": action}},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
    except Exception:
        # If Google cannot be reached, let the person through rather than block
        # everyone; the rate limit still applies
        log.exception("reCAPTCHA assessment failed")
        return True

    properties = result.get("tokenProperties", {})
    score = result.get("riskAnalysis", {}).get("score", 0.0)
    ok = properties.get("valid") and properties.get("action") == action and score >= MIN_SCORE
    if not ok:
        log.warning(
            "signup rejected by reCAPTCHA: valid=%s action=%s score=%s",
            properties.get("valid"),
            properties.get("action"),
            score,
        )
    return bool(ok)
