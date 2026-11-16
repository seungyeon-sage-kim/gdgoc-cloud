"""What anyone can reach: the event list and pages, tickets, and signing up."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from shared import blob, bus, guard, mail, store
from shared.config import get_settings, now
from shared.models import Event
from shared.runtime import event
from web.common import (
    card,
    current_email,
    error,
    event_state,
    has_role,
    not_found,
    poster_url,
    render,
    ticket_card,
    valid_email,
    visible_event,
)

log = logging.getLogger("web")
router = APIRouter()


# ---------- pages ----------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, view: str = "upcoming", q: str = "", only_open: str = Query("", alias="open")):
    """Published events as cards: upcoming soonest first, or past latest first.
    q searches the title, summary, venue and description; open=1 keeps events you can sign up for now."""
    past = view == "past"
    current = now()
    events = [ev for ev in store.list_events() if ev.published and (ev.ends <= current) == past]
    if past:
        events.reverse()
    query = q.strip().lower()
    if query:
        events = [ev for ev in events if query in f"{ev.title} {ev.summary} {ev.venue} {ev.description}".lower()]
    cards = [card(ev) for ev in events]
    if only_open and not past:
        cards = [c for c in cards if c["status"]["state"] in ("open", "waitlist")]
    return render(request, "home.html", nav="home", cards=cards, past=past, q=q.strip(), only_open=bool(only_open))


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: str):
    ev = visible_event(request, event_id)
    if ev is None:
        return not_found(request, "행사를 찾을 수 없습니다")
    # Signed in: fill the form with their details, and find their own registration
    # so the card shows it on any device
    email = current_email(request)
    profile = (store.get_user(email) or {"email": email}) if email else None
    mine = (
        next(
            (r for r in store.registrations_for_email(email) if r.event_id == ev.id and r.status != "cancelled"),
            None,
        )
        if email
        else None
    )
    return render(
        request,
        "event.html",
        nav="home",
        ev=ev,
        status=event_state(ev),
        poster=poster_url(ev),
        og_image=poster_url(ev, "og"),
        profile=profile,
        mine=mine,
        mine_card=ticket_card(mine, ev) if mine else None,
        seat_counts_as_confirmed=now() >= ev.confirm_cutoff,
        mail_ready=mail.enabled(),
    )


@router.get("/t/{registration_id}", response_class=HTMLResponse)
def ticket_page(request: Request, registration_id: str):
    reg = store.get(registration_id)
    ev = store.get_event(reg.event_id) if reg else None
    if ev is None:
        return not_found(request, "티켓을 찾을 수 없습니다")
    return render(
        request,
        "ticket.html",
        nav="ticket",
        reg=reg,
        ev=ev,
        ended=ev.ends <= now(),
        can_cancel=now() < ev.starts,
        position=store.waitlist_position(reg.id),
        can_confirm=reg.status in store.ACTIVE and not reg.confirmed_at and now() < ev.deadline,
    )


@router.get("/t/{registration_id}/qr.svg")
def ticket_qr(registration_id: str):
    data = blob.get(f"tickets/{registration_id}.svg")
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=300"})


def ics_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_event(ev: Event, base: str) -> list:
    def stamp(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    return [
        "BEGIN:VEVENT",
        f"UID:{ev.id}@gdgoc-tickets",
        f"DTSTAMP:{stamp(now())}",
        f"DTSTART:{stamp(ev.starts)}",
        f"DTEND:{stamp(ev.ends)}",
        f"SUMMARY:{ics_text(ev.title)}",
        f"LOCATION:{ics_text(ev.venue)}",
        f"DESCRIPTION:{ics_text(ev.description)}",
        f"URL:{base}/events/{ev.id}",
        "END:VEVENT",
    ]


def ics_response(events: list, request: Request, filename: str) -> Response:
    base = get_settings().public_base_url or str(request.base_url).rstrip("/")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GDGoC Yonsei Cloud Part//Cloud Ticketing//KO",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_text(get_settings().site_name)}",
    ]
    for ev in events:
        lines += ics_event(ev, base)
    lines.append("END:VCALENDAR")
    return Response(
        "\r\n".join(lines) + "\r\n",
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/events/{event_id}/calendar.ics")
def event_calendar(request: Request, event_id: str):
    """'캘린더에 추가': one iCalendar event that Google, Apple and Outlook calendars all import."""
    ev = visible_event(request, event_id)
    if ev is None:
        return Response(status_code=404)
    return ics_response([ev], request, f"{ev.id}.ics")


@router.get("/calendar.ics")
def all_events_calendar(request: Request):
    """'캘린더 구독': every published upcoming session; calendar apps re-read it now and then."""
    current = now()
    events = [ev for ev in store.list_events() if ev.published and ev.ends > current]
    return ics_response(events, request, "cloud-ticketing.ics")


@router.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return render(request, "terms.html")


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return render(request, "privacy.html")


@router.get("/design", response_class=HTMLResponse)
def design_page(request: Request):
    # The component sheet is for people working on the site, not for visitors
    if get_settings().on_cloud_run and not has_role(request, "admin"):
        return not_found(request)
    from shared.demo import make_event

    return render(request, "design.html", nav="design", ev=make_event())


@router.get("/media/posters/{event_id}/{name}")
def poster_file(event_id: str, name: str):
    data = blob.get(f"posters/{event_id}/{name}")
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type=blob.content_type(name), headers={"Cache-Control": "public, max-age=86400"})


# ---------- API ----------


class SignupIn(BaseModel):
    name: str
    email: str
    affiliation: str = ""
    recaptcha_token: str = ""


@router.get("/api/events/{event_id}/status")
def api_status(request: Request, event_id: str):
    ev = visible_event(request, event_id)
    if ev is None:
        return error(404, "not_found", "행사를 찾을 수 없습니다.")
    return event_state(ev)


@router.post("/api/events/{event_id}/registrations", status_code=201)
def api_register(request: Request, event_id: str, body: SignupIn):
    # Abuse protection first: it is cheaper than a database transaction
    if not guard.allow(guard.client_ip(request.headers, request.client.host if request.client else "")):
        event(log, "signup", "signup rate-limited", result="rate_limited")
        return error(429, "too_many", "잠시 후 다시 시도해 주세요.")
    if not guard.human(body.recaptcha_token, "signup"):
        event(log, "signup", "signup rejected by bot check", result="bot_suspected")
        return error(
            403, "bot_suspected", "자동 신청으로 의심되어 처리하지 못했습니다. 새로고침 후 다시 시도해 주세요."
        )

    ev = store.get_event(event_id)
    if ev is None or not ev.published:
        return error(404, "not_found", "행사를 찾을 수 없습니다.")
    name, email = body.name.strip(), body.email.strip()
    if not 1 <= len(name) <= 40:
        return error(422, "invalid_name", "이름을 1자에서 40자 사이로 입력해 주세요.")
    if not valid_email(email):
        return error(422, "invalid_email", "이메일 형식을 확인해 주세요.")
    if len(body.affiliation) > 60:
        return error(422, "invalid_affiliation", "소속은 60자 이내로 입력해 주세요.")

    state = event_state(ev)["state"]
    if state == "upcoming":
        return error(403, "not_open", "아직 신청 기간이 아닙니다.")
    if state in ("closed", "ended"):
        return error(403, "closed", "신청이 마감되었습니다.")

    try:
        reg = store.register(ev, name, email, body.affiliation)
    except store.Full:
        event(log, "signup", "signup refused: seats and waitlist full", result="full", event_id=ev.id)
        return error(409, "full", "자리와 대기 명단이 모두 마감되었습니다.")
    except store.AlreadyRegistered:
        event(log, "signup", "signup refused: duplicate email", result="duplicate", event_id=ev.id)
        return error(409, "duplicate", "이미 이 이메일로 신청한 기록이 있습니다.")
    counts = store.stats(ev)
    event(
        log,
        "signup",
        f"signup {reg.code} {reg.status}",
        result=reg.status,
        event_id=ev.id,
        waitlisted=counts["waitlisted"],
    )

    # Work that waits on other systems (storage, email) happens in the ticket worker, not
    # in this request. The seat is already saved, so a failed publish must not fail the
    # signup: the housekeeping job publishes it again within minutes (repair).
    if reg.status == "pending":  # a waitlisted person gets a ticket only when promoted
        try:
            bus.publish("registrations", {"registration_id": reg.id})
        except Exception:
            log.exception("publish failed for %s; the housekeeping job will retry", reg.id)
    else:
        mail.request("waitlisted", reg.email, registration_id=reg.id)
    return {
        "id": reg.id,
        "code": reg.code,
        "status": reg.status,
        "confirmed": reg.confirmed_at is not None,
        "ticket_url": f"/t/{reg.id}",
    }


@router.get("/api/registrations/{registration_id}")
def api_registration(registration_id: str):
    reg = store.get(registration_id)
    if not reg:
        return error(404, "not_found", "티켓을 찾을 수 없습니다.")
    ev = store.get_event(reg.event_id)
    card = ticket_card(reg, ev) if ev else None
    deadline = ev.confirm_deadline if ev else None
    return reg.public() | {"waitlist_position": store.waitlist_position(reg.id), "card": card, "deadline": deadline}


@router.post("/api/registrations/{registration_id}/confirm")
def api_confirm(registration_id: str):
    """참석 확인: seats not confirmed by the deadline go to the waitlist (jobs/housekeeping.py)."""
    reg = store.get(registration_id)
    ev = store.get_event(reg.event_id) if reg else None
    if ev and now() >= ev.deadline:
        return error(409, "deadline_passed", "참석 확인 기한이 지났습니다.")
    if store.confirm(registration_id) == "invalid":
        return error(409, "cannot_confirm", "확인할 수 없는 티켓입니다.")
    return store.get(registration_id).public()


@router.post("/api/registrations/{registration_id}/cancel")
def api_cancel(registration_id: str):
    reg = store.get(registration_id)
    ev = store.get_event(reg.event_id) if reg else None
    if ev and ev.starts <= now():
        return error(409, "event_started", "행사가 시작된 뒤에는 취소할 수 없습니다.")
    if not store.cancel(registration_id):
        return error(409, "cannot_cancel", "취소할 수 없는 티켓입니다.")
    reg = store.get(registration_id)
    mail.request("cancelled", reg.email, registration_id=reg.id)
    return reg.public()
