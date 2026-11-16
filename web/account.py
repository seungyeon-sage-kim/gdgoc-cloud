"""마이페이지: a signed-in person's tickets and profile, and leaving."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared import mail, store
from shared.config import KST, get_settings, now
from shared.runtime import event
from web.common import as_time, card, current_email, not_found, poster_url, render

log = logging.getLogger("web")
router = APIRouter()


def my_registrations(email: str) -> tuple[list, list]:
    """(upcoming, past) rows of {reg, ev} for one person."""
    events: dict = {}
    upcoming, past = [], []
    current = now()
    for reg in store.registrations_for_email(email):
        if reg.event_id not in events:
            events[reg.event_id] = store.get_event(reg.event_id)
        ev = events[reg.event_id]
        if ev is None:
            continue
        (upcoming if ev.ends > current and reg.status != "cancelled" else past).append(
            {"reg": reg, "ev": ev, "poster": poster_url(ev)}
        )
    upcoming.sort(key=lambda row: row["ev"].starts_at)
    past.sort(key=lambda row: row["ev"].starts_at, reverse=True)
    return upcoming, past


def my_profile(email: str) -> dict:
    return store.get_user(email) or {"email": email, "name": "", "affiliation": ""}


@router.get("/me", response_class=HTMLResponse)
def my_page(request: Request, view: str = "upcoming"):
    email = current_email(request)
    if not email:
        return RedirectResponse("/login?next=/me", status_code=303)
    upcoming, past = my_registrations(email)
    tab = "past" if view == "past" else "tickets"
    suggested = []
    if tab == "tickets" and not upcoming:
        current = now()
        cards = [card(ev) for ev in store.list_events() if ev.published and ev.ends > current]
        suggested = [c for c in cards if c["status"]["state"] in ("open", "waitlist")][:3]
    return render(
        request,
        "me.html",
        nav="me",
        tab=tab,
        profile=my_profile(email),
        upcoming=upcoming,
        past=past,
        suggested=suggested,
        upcoming_count=len(upcoming),
        past_count=len(past),
    )


@router.get("/me/profile", response_class=HTMLResponse)
def my_profile_page(request: Request, notice: str = ""):
    email = current_email(request)
    if not email:
        return RedirectResponse("/login?next=/me/profile", status_code=303)
    upcoming, past = my_registrations(email)
    profile = my_profile(email)
    return render(
        request,
        "me_profile.html",
        nav="me",
        tab="profile",
        profile=profile,
        joined=as_time(profile["created_at"]).astimezone(KST) if profile.get("created_at") else None,
        upcoming_count=len(upcoming),
        past_count=len(past),
        notice=notice,
    )


@router.post("/me/profile")
def save_profile(request: Request, name: str = Form(""), affiliation: str = Form("")):
    email = current_email(request)
    if not email:
        return RedirectResponse("/login?next=/me/profile", status_code=303)
    store.save_user(email, name.strip()[:40], affiliation.strip()[:60])
    return RedirectResponse("/me/profile?notice=saved", status_code=303)


@router.post("/me/delete")
def delete_account(request: Request):
    """회원 탈퇴: the profile goes, and so do this person's seats at events still to come."""
    email = current_email(request)
    if not email:
        return RedirectResponse("/login", status_code=303)
    upcoming, _ = my_registrations(email)
    for row in upcoming:
        if row["ev"].starts > now():
            store.cancel(row["reg"].id)
    store.delete_user(email)
    event(log, "account_deleted", "account deleted", cancelled=len(upcoming))
    response = RedirectResponse("/account-deleted", status_code=303)
    response.delete_cookie("session")
    return response


@router.get("/account-deleted", response_class=HTMLResponse)
def account_deleted(request: Request):
    return render(
        request,
        "message.html",
        title="회원 탈퇴가 완료되었습니다",
        text="프로필을 삭제하고 예정된 행사의 신청을 취소했습니다. 지난 신청 기록은 개인정보처리방침의 보관 기간이 지나면 자동으로 삭제됩니다.",
    )


@router.get("/dev/outbox", response_class=HTMLResponse)
def dev_outbox(request: Request):
    """Local only: the emails the service would have sent."""
    if get_settings().mail_backend != "outbox":
        return not_found(request)
    return render(request, "dev_outbox.html", emails=mail.outbox())
