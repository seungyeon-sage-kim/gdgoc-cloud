"""Email: sign-in codes, signup confirmations, waitlist and attendance notices.

Nobody sends mail in the middle of a request. web and the jobs call request(),
which publishes {"kind": ..., "to": ..., ...} on the "emails" topic; Pub/Sub
pushes it to ticket-worker's /mail, which calls send(). A slow or failing mail
server then delays a message, never a signup, and Pub/Sub retries it.

Where the mail goes depends on MAIL_BACKEND:

  outbox  (local default) each email is saved as a file in DATA_DIR/outbox and
          shown at http://localhost:8080/dev/outbox, so the whole service works
          without a mail account
  smtp    the email provider's SMTP server, Resend by default (MAIL_FROM, SMTP_PASSWORD),
          port 587 with STARTTLS. Cloud Run blocks port 25, not 587.
  log     (on Google Cloud until SMTP is set up) nothing is sent. Sign-in codes
          for organizers are written to the log, so the first organizer can sign
          in and set things up; everyone else is told email is not set up yet.
"""

import json
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import bus, store, text
from .config import get_settings, now

log = logging.getLogger("mail")


def request(kind: str, to: str, **data) -> bool:
    """Ask for an email to be sent (in the background, through Pub/Sub).

    Returns False if the message could not be published. An email is never a
    reason to undo what already happened (a signup, a cancellation), so the
    caller decides whether that matters.
    """
    try:
        bus.publish("emails", {"kind": kind, "to": to, **data})
        return True
    except Exception:
        log.exception("could not queue %s email", kind)
        return False


def enabled() -> bool:
    """Whether email reaches people. False only in "log" mode."""
    return get_settings().mail_backend != "log"


# ---------- what each email says ----------
#
# Registration emails carry the address and the registration id, not the text. The
# text is written when the email is sent, from the current data, so a waitlist
# position or a changed venue is always up to date.

REGISTRATION_KINDS = ("ticket_issued", "waitlisted", "promoted", "confirm_reminder", "released", "cancelled")


def base_url() -> str:
    return get_settings().public_base_url or "http://localhost:8080"


def render(message: dict) -> tuple[str, str]:
    """(subject, plain-text body) for one requested email."""
    kind = message["kind"]
    site = get_settings().site_name
    if kind == "login_code":
        return (
            f"[{site}] 인증 코드 {message['code']}",
            f"인증 코드: {message['code']}\n\n"
            "화면에 이 6자리 코드를 입력해 주세요. 코드는 10분 동안 유효합니다.\n"
            "직접 요청하지 않았다면 이 메일을 무시해 주세요.",
        )
    if kind not in REGISTRATION_KINDS:
        raise ValueError(f"unknown email kind: {kind}")

    reg = store.get(message["registration_id"])
    ev = store.get_event(reg.event_id)
    ticket = f"{base_url()}/t/{reg.id}"
    page = f"{base_url()}/events/{ev.id}"
    details = f"일시: {text.datetime_ko(ev.starts)}\n장소: {ev.venue}\n신청 번호: {reg.code}"
    confirm_note = (
        f"{text.datetime_ko(ev.deadline)}까지 티켓 페이지에서 참석 확인을 눌러 주세요. "
        "확인하지 않은 자리는 대기 중인 분께 넘어갑니다."
        if now() < ev.deadline and not reg.confirmed_at
        else "행사 당일 입구에서 티켓의 QR 코드를 보여 주세요."
    )

    if kind == "ticket_issued":
        subject = f"신청 완료: {ev.title}"
        body = (
            f"{reg.name}님, {ev.title} 참가 신청이 완료되었습니다.\n\n{details}\n\n"
            f"티켓 (입장 QR 코드): {ticket}\n\n{confirm_note}\n"
            "참석이 어려우면 티켓 페이지에서 신청을 취소해 주세요."
        )
    elif kind == "waitlisted":
        position = store.waitlist_position(reg.id)
        subject = f"대기 신청 완료: {ev.title}"
        body = (
            f"{reg.name}님, {ev.title}의 정원이 모두 차서 대기 명단에 등록되었습니다."
            + (f" 현재 대기 {position}번입니다." if position else "")
            + f"\n\n자리가 생기면 신청 순서대로 자동 배정되고, 이 주소로 알려 드립니다.\n대기 상태: {ticket}"
        )
    elif kind == "promoted":
        subject = f"자리 배정: {ev.title}"
        body = (
            f"{reg.name}님, 취소 자리가 생겨 {ev.title}에 참가할 수 있게 되었습니다.\n\n{details}\n\n"
            f"티켓 (입장 QR 코드): {ticket}\n\n{confirm_note}"
        )
    elif kind == "confirm_reminder":
        subject = f"참석 확인 요청: {ev.title}"
        body = (
            f"{reg.name}님, {ev.title} 행사는 {text.datetime_ko(ev.starts)}에 열립니다.\n\n"
            f"{text.datetime_ko(ev.deadline)}까지 참석 확인을 눌러 주세요. "
            "확인하지 않은 자리는 대기 중인 분께 넘어갑니다.\n"
            f"참석 확인: {ticket}\n\n참석이 어려우면 같은 페이지에서 신청을 취소해 주세요."
        )
    elif kind == "released":
        subject = f"자리 반환 안내: {ev.title}"
        body = (
            f"{reg.name}님, {text.datetime_ko(ev.deadline)}까지 참석 확인이 되지 않아 "
            f"{ev.title}의 자리가 대기 중인 분께 넘어갔습니다.\n\n"
            f"자리가 남아 있으면 다시 신청할 수 있습니다: {page}"
        )
    else:  # cancelled
        subject = f"신청 취소: {ev.title}"
        body = f"{reg.name}님, {ev.title} 신청이 취소되었습니다. (신청 번호 {reg.code})\n\n다시 신청하기: {page}"
    footer = f"\n\n이 메일은 {site}에서 보냈습니다."
    return f"[{site}] {subject}", body + footer


# ---------- sending ----------


def send(message: dict) -> bool:
    """Send one email now. Returns False for a repeat delivery (already sent).
    Raises on failure, so Pub/Sub delivers it again.

    Pub/Sub delivers at least once, and so does this. The record is made after sending:
    a crash between the two repeats an email now and then, and never loses a ticket's."""
    if "registration_id" in message:
        # Removed while this message waited (data retention, for example). Retrying cannot help.
        if store.get(message["registration_id"]) is None:
            log.info("registration %s is gone; not sending %s", message["registration_id"], message["kind"])
            return False
        key = f"{message['kind']}:{message['registration_id']}"
    else:
        key = None
    if key and store.email_was_sent(key):
        log.info("already sent %s; skipping the repeat", key)
        return False
    _deliver(message)
    if key:
        store.record_email_sent(key)
    return True


# Domains reserved for tests and examples (RFC 2606, RFC 6761). Nobody receives mail there, so
# real mail never goes to them; the load test signs up thousands of @example.com addresses.
RESERVED_DOMAINS = ("example.com", "example.net", "example.org", "test", "example", "invalid", "localhost")


def reserved_address(to: str) -> bool:
    """Whether an address is at a reserved domain (or one under it, like mail.example.com or x.test)."""
    domain = to.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    return any(domain == d or domain.endswith(f".{d}") for d in RESERVED_DOMAINS)


def _deliver(message: dict) -> None:
    settings = get_settings()
    to = message["to"]
    if settings.mail_backend == "smtp" and reserved_address(to):
        # Done, not failed: no retry, and the address is no one's
        log.info("test address, not sent: %s to %s", message["kind"], to)
        return
    subject, body = render(message)

    if settings.mail_backend == "outbox":
        _save_to_outbox(to, subject, body)
    elif settings.mail_backend == "smtp":
        _send_smtp(to, subject, body)
    else:
        organizer = to in settings.staff_emails
        if message["kind"] == "login_code" and organizer:
            # Readable only by people who can read this project's logs: its owners
            log.warning("email is not set up; sign-in code for organizer %s: %s", to, message["code"])
        else:
            log.info("email is not set up; not sent: %s to %s", message["kind"], to)


def _save_to_outbox(to: str, subject: str, body: str) -> None:
    folder = get_settings().data_dir / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    sent_at = now()
    name = f"{sent_at.strftime('%Y%m%d-%H%M%S-%f')}.json"
    (folder / name).write_text(
        json.dumps({"to": to, "subject": subject, "body": body, "sent_at": sent_at.isoformat()}, ensure_ascii=False)
    )
    log.info("saved email to outbox: %s to %s", subject, to)


def outbox(limit: int = 30) -> list:
    """The newest emails in the local outbox (for /dev/outbox)."""
    folder = get_settings().data_dir / "outbox"
    if not folder.exists():
        return []
    files = sorted(folder.glob("*.json"), reverse=True)[:limit]
    return [json.loads(f.read_text()) for f in files]


def _send_smtp(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    email = EmailMessage()
    email["From"] = formataddr((settings.site_name, settings.mail_from))
    email["To"] = to
    email["Subject"] = subject
    email["Message-ID"] = make_msgid(domain=settings.mail_from.rsplit("@", 1)[-1])
    email.set_content(body)
    # Per step: a provider that hangs fails fast, and Pub/Sub tries again later
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(email)
    # Not the subject or the address: a sign-in email's subject holds its code, and the
    # logs must not hold what lets someone sign in, or who is signing up.
    log.info("sent email through %s", settings.smtp_host)
