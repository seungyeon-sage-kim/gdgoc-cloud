"""Door check-in, for staff: look a ticket up first, then let the person in."""

import logging
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from shared import store, tokens
from shared.config import KST, now
from shared.models import Event
from shared.runtime import event
from web.auth import staff_gate
from web.common import current_email, error, has_role, not_found, render

log = logging.getLogger("web")
router = APIRouter()


@router.get("/checkin", response_class=HTMLResponse)
def checkin_pick(request: Request):
    """Staff first pick the event they are working at."""
    if not has_role(request, "staff"):
        return staff_gate(request, "staff", "/checkin")
    recent = now() - timedelta(days=1)
    events = [ev for ev in store.list_events() if ev.published and ev.ends > recent]
    return render(request, "checkin_pick.html", nav="checkin", events=events)


@router.get("/checkin/{event_id}", response_class=HTMLResponse)
def checkin_page(request: Request, event_id: str):
    if not has_role(request, "staff"):
        return RedirectResponse("/checkin", status_code=303)
    ev = store.get_event(event_id)
    if ev is None:
        return not_found(request, "행사를 찾을 수 없습니다")
    return render(request, "checkin.html", nav="checkin", ev=ev, counts=store.stats(ev))


@router.get("/api/events/{event_id}/attendees")
def api_attendees(request: Request, event_id: str):
    """The door's guest list: everyone holding a seat, to find by name when a QR will not scan."""
    if not has_role(request, "staff"):
        return error(401, "unauthorized", "스태프 로그인이 필요합니다.")
    regs = [r for r in store.list_registrations(event_id) if r.status in store.ACTIVE]
    return [
        {"code": r.code, "name": r.name, "affiliation": r.affiliation, "checked_in": r.checked_in_at is not None}
        for r in sorted(regs, key=lambda r: r.name)
    ]


class CheckinIn(BaseModel):
    event_id: str
    value: str


def door_ticket(ev: Event, value: str) -> tuple:
    """(registration, None) for a scanned QR or a typed code, or (None, why not)."""
    value = value.strip()
    # A QR holds a signed token ("payload.signature"); people read out the short code
    if "." in value:
        signed = tokens.read_ticket_token(value)
        if not signed:
            return None, "invalid"
        if signed[0] != ev.id:  # the event is part of what was signed
            return None, "wrong_event"
        reg = store.get(signed[1])
        if not reg:
            return None, "invalid"
    else:
        reg = store.get_by_code(value)
        if not reg:
            return None, "unknown"
    if reg.event_id != ev.id:
        # A real ticket, but for another event: say so instead of letting it in
        return None, "wrong_event"
    return reg, None


def door_attendee(reg) -> dict:
    return reg.public() | {"affiliation": reg.affiliation}


@router.post("/api/checkin/lookup")
def api_checkin_lookup(request: Request, body: CheckinIn):
    """What staff see before letting someone in. Changes nothing: /api/checkin does that."""
    if not has_role(request, "staff"):
        return error(401, "unauthorized", "스태프 로그인이 필요합니다.")
    ev = store.get_event(body.event_id)
    if ev is None:
        return error(404, "not_found", "행사를 찾을 수 없습니다.")
    reg, problem = door_ticket(ev, body.value)
    if problem:
        return {"result": problem}
    off_day = ev.starts.astimezone(KST).date() != now().astimezone(KST).date()
    return {"result": reg.door_status(), "attendee": door_attendee(reg), "off_day": off_day}


@router.post("/api/checkin")
def api_checkin(request: Request, body: CheckinIn):
    if not has_role(request, "staff"):
        return error(401, "unauthorized", "스태프 로그인이 필요합니다.")
    ev = store.get_event(body.event_id)
    if ev is None:
        return error(404, "not_found", "행사를 찾을 수 없습니다.")
    reg, problem = door_ticket(ev, body.value)
    if problem:
        return {"result": problem, "counts": store.stats(ev)}
    result = store.check_in(reg.id)
    event(
        log,
        "checkin",
        f"check-in {reg.code} {result}",
        result=result,
        event_id=ev.id,
        staff=current_email(request),
    )
    reg = store.get(reg.id)
    return {"result": result, "attendee": door_attendee(reg), "counts": store.stats(ev)}


@router.post("/api/checkin/undo")
def api_checkin_undo(request: Request, body: CheckinIn):
    """입장 취소: the ticket goes back to not entered."""
    if not has_role(request, "staff"):
        return error(401, "unauthorized", "스태프 로그인이 필요합니다.")
    ev = store.get_event(body.event_id)
    if ev is None:
        return error(404, "not_found", "행사를 찾을 수 없습니다.")
    reg, problem = door_ticket(ev, body.value)
    if problem:
        return {"result": problem, "counts": store.stats(ev)}
    result = "undone" if store.undo_check_in(reg.id) else "not_checked_in"
    event(
        log,
        "checkin_undo",
        f"check-in undo {reg.code} {result}",
        result=result,
        event_id=ev.id,
        staff=current_email(request),
    )
    reg = store.get(reg.id)
    return {"result": result, "attendee": door_attendee(reg), "counts": store.stats(ev)}
