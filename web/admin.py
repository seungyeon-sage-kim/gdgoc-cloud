"""Running events, for organizers: create, edit, publish, posters, the attendee list."""

import csv
import io
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from shared import blob, poster, store, waitlist
from shared.config import KST, now
from shared.models import Event, usual_schedule
from web.auth import admin_gate
from web.common import current_email, event_state, not_found, poster_url, render

EVENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
MAX_POSTER_BYTES = 10 * 1024 * 1024

log = logging.getLogger("web")
router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    if gate := admin_gate(request):
        return gate
    rows = [{"ev": ev, "status": event_state(ev)} for ev in reversed(store.list_events())]
    return render(request, "admin_events.html", nav="admin", rows=rows)


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request):
    """How the service itself is doing: failed messages, which revision answered, email mode."""
    if gate := admin_gate(request):
        return gate
    return render(request, "admin_system.html", nav="admin", dead_letters=store.list_dead_letters())


def new_event_defaults() -> Event:
    """A blank form, prefilled the way most events are set: two weeks out at 19:00, signups open now."""
    starts = (now() + timedelta(days=14)).replace(hour=19, minute=0, second=0, microsecond=0)
    opens = now().replace(second=0, microsecond=0)
    return Event(
        id="",
        title="",
        summary="",
        description="",
        venue="",
        seat_limit=30,
        waitlist_limit=100,
        published=False,
        **usual_schedule(starts, opens),
    )


EVENT_TIME_FIELDS = ("starts_at", "ends_at", "opens_at", "closes_at", "confirm_deadline")


def read_event_form(form, existing: Event | None) -> tuple[Event, list]:
    """The event the form describes, and what is wrong with it (empty when it is fine)."""
    errors = []
    text = {k: str(form.get(k, "")).strip() for k in ("id", "title", "summary", "description", "venue")}
    times = {}
    for field in EVENT_TIME_FIELDS:
        try:
            times[field] = datetime.fromisoformat(str(form.get(field, ""))).replace(tzinfo=KST).isoformat()
        except ValueError:
            errors.append("날짜와 시간을 모두 입력해 주세요.")
            times[field] = (existing or new_event_defaults()).__dict__[field]
    numbers = {}
    for field, low in (("seat_limit", 1), ("waitlist_limit", 0)):
        try:
            numbers[field] = int(form.get(field, ""))
            if not low <= numbers[field] <= 5000:
                raise ValueError
        except ValueError:
            errors.append("정원과 대기 정원은 0에서 5000 사이의 숫자로 입력해 주세요.")
            numbers[field] = getattr(existing, field, 0)

    event_id = existing.id if existing else text["id"].lower()
    if not existing and not EVENT_ID.match(event_id):
        errors.append("주소는 영문 소문자, 숫자, 하이픈으로 3자에서 40자까지 입력해 주세요.")
    if not 1 <= len(text["title"]) <= 80:
        errors.append("행사 이름을 80자 이내로 입력해 주세요.")
    if not text["venue"]:
        errors.append("장소를 입력해 주세요.")

    ev = Event(
        id=event_id,
        title=text["title"],
        summary=text["summary"][:120],
        description=text["description"][:4000],
        venue=text["venue"][:120],
        **times,
        **numbers,
        published=form.get("published") == "on",
        poster_version=existing.poster_version if existing else "",
        created_at=existing.created_at if existing else "",
    )
    if not errors:
        if ev.ends <= ev.starts:
            errors.append("종료 시간은 시작 시간보다 늦어야 합니다.")
        if ev.closes <= ev.opens:
            errors.append("신청 마감은 신청 시작보다 늦어야 합니다.")
        if ev.closes > ev.ends:
            errors.append("신청 마감은 행사 종료보다 빨라야 합니다.")
        if ev.deadline >= ev.starts:
            errors.append("참석 확인 마감은 행사 시작보다 빨라야 합니다.")
    return ev, errors


@router.get("/admin/events/new", response_class=HTMLResponse)
def admin_new_event(request: Request):
    if gate := admin_gate(request):
        return gate
    return render(request, "admin_edit.html", nav="admin", ev=new_event_defaults(), creating=True, errors=[])


@router.post("/admin/events/new", response_class=HTMLResponse)
async def admin_create_event(request: Request):
    if gate := admin_gate(request):
        return gate
    ev, errors = read_event_form(await request.form(), None)
    if not errors:
        try:
            store.create_event(ev)
        except store.EventExists:
            errors.append("이미 사용 중인 주소입니다. 다른 주소를 입력해 주세요.")
    if errors:
        return render(request, "admin_edit.html", nav="admin", ev=ev, creating=True, errors=errors, status_code=422)
    log.info("event %s created by %s", ev.id, current_email(request))
    return RedirectResponse(f"/admin/events/{ev.id}?notice=created", status_code=303)


@router.get("/admin/events/{event_id}", response_class=HTMLResponse)
def admin_event(request: Request, event_id: str, notice: str = ""):
    if gate := admin_gate(request):
        return gate
    ev = store.get_event(event_id)
    if ev is None:
        return not_found(request, "행사를 찾을 수 없습니다")
    counts = store.stats(ev)
    return render(
        request,
        "admin_event.html",
        nav="admin",
        ev=ev,
        counts=counts,
        status=event_state(ev, counts),
        registrations=store.list_registrations(ev.id),
        poster=poster_url(ev),
        notice=notice,
    )


@router.get("/admin/events/{event_id}/edit", response_class=HTMLResponse)
def admin_edit_event(request: Request, event_id: str):
    if gate := admin_gate(request):
        return gate
    ev = store.get_event(event_id)
    if ev is None:
        return not_found(request, "행사를 찾을 수 없습니다")
    return render(request, "admin_edit.html", nav="admin", ev=ev, creating=False, errors=[])


@router.post("/admin/events/{event_id}/edit", response_class=HTMLResponse)
async def admin_save_event(request: Request, event_id: str):
    if gate := admin_gate(request):
        return gate
    existing = store.get_event(event_id)
    if existing is None:
        return not_found(request, "행사를 찾을 수 없습니다")
    ev, errors = read_event_form(await request.form(), existing)
    if errors:
        return render(request, "admin_edit.html", nav="admin", ev=ev, creating=False, errors=errors, status_code=422)
    store.update_event(ev)
    log.info("event %s edited by %s", ev.id, current_email(request))
    if ev.seat_limit > existing.seat_limit:
        try:
            waitlist.fill_free_seats(ev)  # new seats go to the people already waiting
        except Exception:
            log.exception("could not fill the new seats of %s; housekeeping will", ev.id)
    return RedirectResponse(f"/admin/events/{ev.id}?notice=saved", status_code=303)


@router.post("/admin/events/{event_id}/publish")
def admin_publish(request: Request, event_id: str, published: str = Form(...)):
    if gate := admin_gate(request):
        return gate
    ev = store.get_event(event_id)
    if ev is None:
        return RedirectResponse("/admin", status_code=303)
    store.update_event(replace(ev, published=published == "1"))
    notice = "published" if published == "1" else "hidden"
    return RedirectResponse(f"/admin/events/{ev.id}?notice={notice}", status_code=303)


@router.post("/admin/events/{event_id}/poster")
async def admin_poster(request: Request, event_id: str, file: UploadFile = File(...)):
    if gate := admin_gate(request):
        return gate
    ev = store.get_event(event_id)
    if ev is None:
        return RedirectResponse("/admin", status_code=303)
    back = f"/admin/events/{ev.id}"
    data = await file.read(MAX_POSTER_BYTES + 1)
    if len(data) > MAX_POSTER_BYTES or not (file.content_type or "").startswith("image/"):
        return RedirectResponse(f"{back}?notice=poster-invalid", status_code=303)
    try:
        images = poster.render(data)
    except ValueError:
        return RedirectResponse(f"{back}?notice=poster-invalid", status_code=303)
    for name, jpeg in images.items():
        blob.put(f"posters/{ev.id}/{name}", jpeg)
    store.update_event(replace(ev, poster_version=now().strftime("%Y%m%d%H%M%S")))
    log.info("poster for %s replaced by %s", ev.id, current_email(request))
    return RedirectResponse(f"{back}?notice=poster-uploaded", status_code=303)


def csv_safe(value) -> str:
    # A name like "=HYPERLINK(...)" would run as a formula in Excel; a leading ' keeps it text
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@router.get("/admin/events/{event_id}/registrations.csv")
def admin_csv(request: Request, event_id: str):
    if gate := admin_gate(request):
        return gate
    ev = store.get_event(event_id)
    if ev is None:
        return Response(status_code=404)
    # Personal data leaves the system here, so record who took it
    log.info("attendee CSV for %s exported by %s", ev.id, current_email(request))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["code", "name", "email", "affiliation", "status", "created_at", "confirmed_at", "checked_in_at"])
    for reg in store.list_registrations(ev.id):
        writer.writerow(
            map(
                csv_safe,
                [
                    reg.code,
                    reg.name,
                    reg.email,
                    reg.affiliation,
                    reg.status,
                    reg.created_at,
                    reg.confirmed_at or "",
                    reg.checked_in_at or "",
                ],
            )
        )
    return Response(
        "﻿" + out.getvalue(),  # BOM so Excel opens Korean text correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{ev.id}-registrations.csv"'},
    )
