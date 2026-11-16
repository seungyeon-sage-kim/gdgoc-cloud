"""Signing in: Google or a code sent by email, and the gates in front of staff pages."""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from shared import guard, mail, store, tokens
from shared.config import get_settings, now
from shared.runtime import event
from web.common import current_email, error, has_role, render, valid_email

log = logging.getLogger("web")
router = APIRouter()


# Two ways in, both ending in the same session: Google, or a code. For a code, a person
# types their email, receives a 6-digit code, and types it back; that proves they own the
# address. No password is stored anywhere. The session is a signed
# cookie holding the email. Staff and admin are not separate accounts: they are
# emails on STAFF_EMAILS / ADMIN_EMAILS.

SESSION_DAYS = 30
COOKIE = {"httponly": True, "samesite": "lax", "max_age": 60 * 60 * 24 * SESSION_DAYS}
CODE_MINUTES = 10
RESEND_SECONDS = 60


def code_hash(email: str, code: str) -> str:
    """The code is stored only as a keyed hash: a leaked database row does not reveal it."""
    key = get_settings().session_secret.encode()
    return hmac.new(key, f"login:{email}:{code}".encode(), hashlib.sha256).hexdigest()


def safe_next(target: str) -> str:
    # Only paths on this site. Browsers read a backslash as a slash and drop tabs and
    # newlines, so "//evil.example", "/\\evil.example" and "/\t/evil.example" would all leave it
    on_this_site = target.startswith("/") and not target.startswith("//")
    return target if on_this_site and "\\" not in target and target.isprintable() else "/me"


class EmailIn(BaseModel):
    email: str


class CodeIn(BaseModel):
    email: str
    code: str
    next: str = "/me"
    name: str = ""  # from the sign-up form


class GoogleIn(BaseModel):
    credential: str
    next: str = "/me"


def login_page(request: Request, mode: str = "login", **context) -> HTMLResponse:
    """mode is login or signup: the same page with a different form."""
    response = render(request, "login.html", mode=mode, mail_ready=mail.enabled(), **context)
    # Google's button has to see which site loaded it, and the site's default policy hides that.
    # These are the values Google asks for. Only the origin is sent on HTTPS, never the path.
    response.headers["Referrer-Policy"] = (
        "strict-origin-when-cross-origin" if get_settings().on_cloud_run else "no-referrer-when-downgrade"
    )
    return response


def signed_in(email: str, target: str, name: str = "") -> JSONResponse:
    """The end of every sign-in: the account exists, and the browser holds a session."""
    if store.get_user(email) is None:
        store.save_user(email, name)
    response = JSONResponse({"next": safe_next(target)})
    # Secure only on Cloud Run: on http://localhost, Safari drops a Secure cookie and sign-in fails
    secure = get_settings().on_cloud_run
    response.set_cookie("session", tokens.sign(email, "session"), **COOKIE, secure=secure)
    event(log, "login", "signed in", result="ok", staff=email in get_settings().staff_emails)
    return response


def google_claims(credential: str, client_id: str) -> dict | None:
    """What Google's signed ID token says, once its signature, audience, issuer and expiry check out."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        return id_token.verify_oauth2_token(credential, google_requests.Request(), client_id, clock_skew_in_seconds=10)
    except (ValueError, GoogleAuthError):
        return None


def google_owns_address(claims: dict) -> bool:
    """Google vouches for an address only on Gmail and on Workspace domains. An outside address on a
    personal Google account may have changed hands since it was verified, and here the address is the account."""
    email = str(claims.get("email", "")).lower()
    return bool(claims.get("email_verified")) and (email.endswith("@gmail.com") or bool(claims.get("hd")))


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = "/me"):
    if current_email(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return login_page(request, nav="me", next=safe_next(next))


@router.get("/signup", response_class=HTMLResponse)
def signup(request: Request, next: str = "/me"):
    if current_email(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return login_page(request, mode="signup", nav="me", next=safe_next(next))


def send_code(request: Request, email: str) -> JSONResponse | None:
    """Email a 6-digit code. Returns the error to send back, or None once it is on its way."""
    if not valid_email(email):
        return error(422, "invalid_email", "이메일 형식을 확인해 주세요.")
    ip = guard.client_ip(request.headers, request.client.host if request.client else "")
    if not guard.allow(ip, "code", guard.CODES_PER_MINUTE_PER_IP):
        return error(429, "too_many", "잠시 후 다시 시도해 주세요.")
    sent = store.code_sent_at(email)
    if sent and (now() - datetime.fromisoformat(sent)).total_seconds() < RESEND_SECONDS:
        return error(429, "too_soon", "코드를 방금 보냈습니다. 1분 뒤에 다시 받을 수 있습니다.")
    if not mail.enabled() and email not in get_settings().staff_emails:
        return error(503, "mail_off", "이메일 발송이 아직 설정되지 않아 로그인 코드를 보낼 수 없습니다.")

    code = f"{secrets.randbelow(10**6):06d}"
    expires = (now() + timedelta(minutes=CODE_MINUTES)).isoformat(timespec="seconds")
    store.save_login_code(email, code_hash(email, code), expires)
    if not mail.request("login_code", email, code=code):
        return error(503, "mail_failed", "코드를 보내지 못했습니다. 잠시 후 다시 시도해 주세요.")
    event(log, "login_code", "sign-in code requested")
    return None


@router.post("/api/auth/code")
def api_send_code(request: Request, body: EmailIn):
    email = body.email.strip().lower()
    return send_code(request, email) or {"sent": True, "email": email}


@router.post("/api/auth/verify")
def api_verify_code(request: Request, body: CodeIn):
    email, code = body.email.strip().lower(), body.code.strip()
    result = store.use_login_code(email, code_hash(email, code))
    if result != "ok":
        event(log, "login", "sign-in refused", result=result)
        messages = {
            "wrong": (401, "코드가 맞지 않습니다. 메일에 적힌 6자리 숫자를 확인해 주세요."),
            "locked": (429, "잘못 입력한 횟수가 많습니다. 코드를 다시 받아 주세요."),
            "expired": (410, "코드가 만료되었습니다. 코드를 다시 받아 주세요."),
            "missing": (410, "코드가 만료되었습니다. 코드를 다시 받아 주세요."),
        }
        status, message = messages[result]
        return error(status, result, message)
    return signed_in(email, body.next, body.name.strip()[:40])


@router.post("/api/auth/google")
def api_google_sign_in(request: Request, body: GoogleIn):
    client_id = get_settings().google_client_id
    if not client_id:
        return error(404, "not_configured", "Google 로그인이 설정되지 않았습니다.")
    ip = guard.client_ip(request.headers, request.client.host if request.client else "")
    if not guard.allow(ip, "google", guard.GOOGLE_SIGN_INS_PER_MINUTE_PER_IP):
        return error(429, "too_many", "잠시 후 다시 시도해 주세요.")
    claims = google_claims(body.credential, client_id)
    if claims is None:
        event(log, "login", "sign-in refused", result="invalid_google_token")
        return error(401, "invalid_token", "Google 로그인을 확인하지 못했습니다. 다시 시도해 주세요.")
    if not google_owns_address(claims):
        event(log, "login", "sign-in refused", result="google_address_not_vouched")
        hint = (
            "이메일 코드로 로그인해 주세요."
            if mail.enabled()
            else "Gmail 또는 학교, 회사 Google 계정으로 로그인해 주세요."
        )
        return error(403, "address_not_verified", f"이 Google 계정의 이메일 주소를 확인할 수 없습니다. {hint}")
    return signed_in(str(claims["email"]).strip().lower(), body.next, str(claims.get("name", "")))


@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session")
    return response


# ---------- gates for staff pages ----------


def staff_gate(request: Request, role: str, target: str) -> HTMLResponse:
    """Signed out: the sign-in form, right here. Signed in without the role: say so."""
    nav = "admin" if role == "admin" else "checkin"
    if current_email(request):
        return render(request, "gate.html", nav=nav, role=role, status_code=403)
    heading = "운영진 로그인" if role == "admin" else "스태프 로그인"
    intro = (
        "행사를 만들고 신청 현황을 관리하는 운영진 페이지입니다. 등록된 운영진 이메일의 계정으로 로그인합니다."
        if role == "admin"
        else "입구에서 티켓을 확인하는 스태프 페이지입니다. 등록된 스태프 이메일의 계정으로 로그인합니다."
    )
    return login_page(request, nav=nav, next=target, heading=heading, intro=intro)


def admin_gate(request: Request) -> HTMLResponse | None:
    if has_role(request, "admin"):
        return None
    return staff_gate(request, "admin", request.url.path)
