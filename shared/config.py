"""All settings come from environment variables.

Events are not settings: organizers create them on the admin pages and they
live in the database (shared/store.py).

On Cloud Run, Terraform sets them (infra/run.tf) and every change creates a new
revision. Locally, dev.py loads them from .env.
"""

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _emails(name: str) -> frozenset:
    return frozenset(e.strip().lower() for e in os.environ.get(name, "").split(",") if e.strip())


def _parse_time(value: str) -> datetime:
    """ISO 8601 time; a time without an offset is read as Korea time."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


@dataclass(frozen=True)
class Settings:
    # Secrets: on Cloud Run these come from Secret Manager, mounted as env vars
    ticket_secret: str  # signs QR tickets (web, ticket-worker)
    session_secret: str  # signs sign-in cookies and hashes sign-in codes (web only)
    smtp_password: str  # the email provider's key; empty until someone sets it up

    # Wiring between services
    data_dir: Path
    public_base_url: str
    push_endpoint: str
    waitlist_promoter_url: str

    # Everyone signs in the same way (an emailed code). These lists say who may
    # also check people in at the door (staff) and run events (admin).
    admin_emails: frozenset
    staff_emails: frozenset

    # Sign in with Google: the OAuth client's ID, a public value. Empty: Google's button is not shown.
    google_client_id: str

    # Email (shared/mail.py)
    site_name: str  # the product name: header, page titles, the sender name and the [prefix] of subjects
    organizer: str  # who runs it: the footer credit, the host block, the terms and the privacy policy
    contact_email: str  # shown in the footer, the terms and the privacy policy; optional
    privacy_officer: str  # 개인정보 보호책임자, named in the privacy policy
    mail_backend: str  # outbox | smtp | log
    smtp_host: str
    smtp_port: int
    smtp_user: str  # the SMTP login; for Resend, the word resend
    mail_from: str  # the address emails come from, on a verified domain; a secret on Google Cloud

    # Abuse protection on signup
    recaptcha_site_key: str  # empty: no bot check (local)
    signups_per_minute_per_ip: int

    # Personal data is deleted this many days after the event (Firestore TTL)
    retention_days: int

    # Local demo knobs for the worker
    worker_delay_ms: int
    worker_fail_rate: float

    # Set by Cloud Run itself; "local" when running on a laptop
    service_name: str
    revision: str
    on_cloud_run: bool  # only Cloud Run sets these: K_REVISION for a service, CLOUD_RUN_JOB for a job
    instance_id: str


def _secret(name: str, local_default: str) -> str:
    """A signing key. Only a laptop gets the default: it is in the repo, so anyone could forge with it."""
    return _env(name, "" if os.environ.get("BACKEND") == "gcp" else local_default)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        ticket_secret=_secret("TICKET_SECRET", "local-dev-secret-change-me"),
        session_secret=_secret("SESSION_SECRET", "local-session-secret-change-me"),
        smtp_password=_env("SMTP_PASSWORD", ""),
        data_dir=Path(_env("DATA_DIR", str(ROOT / ".data"))),
        public_base_url=_env("PUBLIC_BASE_URL", "").rstrip("/"),  # empty: use the request's own address
        push_endpoint=_env("PUSH_ENDPOINT", "http://localhost:8081/"),
        waitlist_promoter_url=_env("WAITLIST_PROMOTER_URL", "http://localhost:8082/"),
        admin_emails=_emails("ADMIN_EMAILS"),
        staff_emails=_emails("STAFF_EMAILS") | _emails("ADMIN_EMAILS"),  # admins can also check people in
        google_client_id=_env("GOOGLE_CLIENT_ID", ""),
        site_name=_env("SITE_NAME", "Cloud Ticketing"),
        organizer=_env("ORGANIZER", "GDGoC Yonsei 클라우드 파트"),
        contact_email=_env("CONTACT_EMAIL", ""),  # empty: no address is published anywhere
        privacy_officer=_env("PRIVACY_OFFICER", "클라우드 파트 운영진 대표"),
        mail_backend=_env("MAIL_BACKEND", "log" if os.environ.get("BACKEND") == "gcp" else "outbox"),
        smtp_host=_env("SMTP_HOST", "smtp.resend.com"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER", "resend"),
        mail_from=_env("MAIL_FROM", ""),
        recaptcha_site_key=_env("RECAPTCHA_SITE_KEY", ""),
        # A whole campus can share one address, and an opening brings everyone at once
        signups_per_minute_per_ip=int(_env("SIGNUPS_PER_MINUTE_PER_IP", "100")),
        retention_days=int(_env("RETENTION_DAYS", "30")),
        worker_delay_ms=int(_env("WORKER_DELAY_MS", "0")),  # .env.example sets 1200 for the local demo
        worker_fail_rate=float(_env("WORKER_FAIL_RATE", "0")),
        service_name=_env("K_SERVICE", "local"),
        revision=_env("K_REVISION", "local"),
        on_cloud_run=bool(os.environ.get("K_REVISION") or os.environ.get("CLOUD_RUN_JOB")),
        instance_id=secrets.token_hex(3),
    )


def now() -> datetime:
    return datetime.now(UTC).astimezone(KST)
