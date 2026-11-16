"""What every part of web shares: templates and formatting, who is signed in, and the event helpers."""

import re
import zlib
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from shared import store, text, tokens
from shared.config import KST, get_settings, now
from shared.models import WAITLISTED, Event

HERE = Path(__file__).parent
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL = 254  # the longest address the standard allows
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- formatting for templates ----------

WEEKDAYS = text.WEEKDAYS
fmt_datetime, fmt_date, fmt_clock, as_time = text.datetime_ko, text.date, text.clock, text.as_time


def fmt_time(value) -> str:
    if not value:
        return ""
    value = as_time(value)
    return f"{value.month}/{value.day} {value.hour:02d}:{value.minute:02d}"


def fmt_input(value) -> str:
    """The value a <input type="datetime-local"> expects, in Korea time."""
    return as_time(value).astimezone(KST).strftime("%Y-%m-%dT%H:%M") if value else ""


templates.env.filters["datetime"] = fmt_datetime
templates.env.filters["time"] = fmt_time
templates.env.filters["date"] = fmt_date
templates.env.filters["clock"] = fmt_clock
templates.env.filters["input"] = fmt_input
templates.env.globals["weekdays"] = WEEKDAYS


# What a ticket is called, the same on every screen, and its badge color.
def ticket_badge(reg, ev) -> tuple[str, str]:
    if reg.checked_in_at:
        return "입장 완료", "success"
    if reg.status == "cancelled":
        return "취소됨", ""
    if ev.ends <= now():
        return ("대기 종료" if reg.status == "waitlisted" else "미입장"), ""
    if reg.status == "waitlisted":
        return "대기 중", "warning"
    if reg.confirmed_at:
        return "참석 확인 완료", "success"
    return "참석 확인 필요", "warning"


def ticket_card(reg, ev) -> dict:
    """The event page's card for someone holding a ticket. The page and its script both show this."""
    label, tone = ticket_badge(reg, ev)
    if reg.checked_in_at:
        title, badge, bar = label, "", label
        about = f"{reg.name}님, {fmt_datetime(reg.checked_in_at)}에 입장했습니다."
    elif reg.status == "waitlisted":
        position = store.waitlist_position(reg.id)
        title, badge, bar = "대기 신청 완료", label, f"대기 {position}번"
        about = f"{reg.name}님은 대기 {position}번입니다. 자리가 생기면 자동으로 배정되고 티켓이 발급됩니다."
    elif reg.confirmed_at:
        title, badge, bar = label, "", label
        about = f"{reg.name}님, 행사 당일 입구에서 QR 코드를 보여 주세요. 신청 번호 {reg.code}"
    else:
        title, badge, bar = "신청 완료", label, label
        about = f"{reg.name}님, {fmt_datetime(ev.deadline)}까지 티켓 페이지에서 참석 확인을 눌러 주세요. 신청 번호 {reg.code}"
    return {"title": title, "badge": badge, "tone": tone, "bar": bar, "text": about}


templates.env.globals["ticket_badge"] = ticket_badge

# What each event state is called on cards and tags, and the tag's color
STATE_LABELS = {
    "upcoming": ("오픈 예정", "warning"),
    "open": ("신청 가능", "success"),
    "waitlist": ("대기 신청", "warning"),
    "full": ("마감", "danger"),
    "closed": ("신청 종료", ""),
    "ended": ("종료", ""),
}
templates.env.globals["state_labels"] = STATE_LABELS


# ---------- poster tint ----------

EVENT_HUES = ("blue", "red", "yellow", "green", "sky", "pink")


def event_hue(event_id: str) -> str:
    """One color per event, the same every time: it tints the event's poster."""
    return EVENT_HUES[zlib.crc32(event_id.encode()) % len(EVENT_HUES)]


templates.env.globals["event_hue"] = event_hue


# ---------- pages ----------


def asset_version() -> str:
    """Changes whenever a CSS/JS file changes, so browsers never use a stale copy."""
    newest = max(p.stat().st_mtime for p in (HERE / "static").rglob("*") if p.is_file())
    return str(int(newest))


def render(request: Request, name: str, nav: str = "", status_code: int = 200, **context) -> HTMLResponse:
    settings = get_settings()
    context.update(
        settings=settings,
        nav=nav,
        v=asset_version(),
        me=current_email(request),
        is_staff=has_role(request, "staff"),
        is_admin=has_role(request, "admin"),
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def not_found(request: Request, message: str = "페이지를 찾을 수 없습니다") -> HTMLResponse:
    return render(request, "not_found.html", status_code=404, message=message)


# ---------- who is signed in ----------


def current_email(request: Request) -> str | None:
    return tokens.verify(request.cookies.get("session", ""), "session")


def has_role(request: Request, role: str) -> bool:
    """role is "staff" (door check-in) or "admin" (events); admins are staff too."""
    email = current_email(request)
    settings = get_settings()
    return bool(email) and email in (settings.admin_emails if role == "admin" else settings.staff_emails)


# ---------- answers and input ----------


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def valid_email(email: str) -> bool:
    # Length first: a very long string makes this pattern backtrack for minutes
    return len(email) <= MAX_EMAIL and bool(EMAIL.match(email))


# ---------- events ----------


def event_state(ev: Event, counts: dict | None = None) -> dict:
    counts = counts or store.stats(ev)
    current = now()
    if current >= ev.ends:
        state = "ended"
    elif current < ev.opens:
        state = "upcoming"
    elif current >= ev.closes:
        state = "closed"
    else:
        # No seat for a new signup can still mean a place in line for a cancelled one
        state = {"pending": "open", WAITLISTED: "waitlist", None: "full"}[
            ev.signup_status(counts["held"], counts["waitlisted"])
        ]
    return {
        "state": state,
        "seat_limit": counts["seat_limit"],
        "seats_left": 0 if counts["waitlisted"] else counts["seats_free"],  # for a new signup: a free seat is theirs
        "held": counts["held"],
        "waitlisted": counts["waitlisted"],
        "opens_at": ev.opens_at,
        "closes_at": ev.closes_at,
        "server_time": current.isoformat(),
    }


def visible_event(request: Request, event_id: str) -> Event | None:
    """A published event, or any event for an organizer previewing a draft."""
    ev = store.get_event(event_id)
    if ev is None or not (ev.published or has_role(request, "admin")):
        return None
    return ev


def poster_url(ev: Event, kind: str = "web") -> str | None:
    if not ev.poster_version:
        return None
    return f"/media/posters/{ev.id}/{kind}.jpg?v={ev.poster_version}"


def card(ev: Event) -> dict:
    """What the homepage needs to draw one event card."""
    return {"ev": ev, "status": event_state(ev), "poster": poster_url(ev)}
