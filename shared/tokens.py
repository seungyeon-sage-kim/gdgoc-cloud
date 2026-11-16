"""Signed values: the QR ticket token and its image, and the session cookie.

A signed value is `payload.signature`, where the signature is an HMAC of the
payload made with a secret: TICKET_SECRET for tickets, SESSION_SECRET for
sessions. Two secrets, so ticket-worker (which signs tickets) cannot forge an
admin session. Anyone can read the payload, but nobody can
change it or invent a new one without the secret. That is why the secret lives
in Secret Manager and never in the code or the repo.
"""

import base64
import hashlib
import hmac
import io

import segno

from .config import get_settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: str, purpose: str) -> str:
    settings = get_settings()
    key = (settings.session_secret if purpose == "session" else settings.ticket_secret).encode()
    if not key:  # never sign with an empty key: anyone could then make a valid signature
        raise RuntimeError(f"no signing key for {purpose}")
    # The purpose is mixed in so a session cookie can never be used as a ticket
    digest = hmac.new(key, f"{purpose}:{payload}".encode(), hashlib.sha256).digest()
    return _b64(digest[:16])


def sign(value: str, purpose: str) -> str:
    payload = _b64(value.encode())
    return f"{payload}.{_signature(payload, purpose)}"


def verify(token: str, purpose: str) -> str | None:
    """Returns the original value, or None if the token was changed or forged."""
    try:
        payload, signature = token.strip().split(".", 1)
    except ValueError:
        return None
    # As bytes: compare_digest raises on a non-ASCII string, and anyone can send one
    if not hmac.compare_digest(signature.encode(), _signature(payload, purpose).encode()):
        return None
    try:
        return _unb64(payload).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def ticket_token(event_id: str, registration_id: str) -> str:
    return sign(f"{event_id}/{registration_id}", "ticket")


def ticket_qr(event_id: str, registration_id: str) -> bytes:
    """The ticket token as a QR code, an SVG with a transparent background."""
    out = io.BytesIO()
    code = segno.make(ticket_token(event_id, registration_id), error="m")
    code.save(out, kind="svg", scale=8, border=1, dark="#17181c", light=None, xmldecl=False)
    return out.getvalue()


def read_ticket_token(token: str) -> tuple[str, str] | None:
    """Returns (event id, registration id) if the token is genuine, None if it was forged."""
    value = verify(token, "ticket")
    if not value or "/" not in value:
        return None
    event_id, _, registration_id = value.partition("/")
    return event_id, registration_id
