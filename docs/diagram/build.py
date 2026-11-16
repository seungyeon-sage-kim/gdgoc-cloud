"""Draw docs/architecture.svg with the official Google Cloud icons (2025 icon set).

Google gives its own icon only to core products (Cloud Run, Cloud Storage); every
other product uses its category icon, labelled with the product name. The icons
are embedded as data URIs so the SVG is one self-contained file.

    python docs/diagram/build.py        → docs/architecture.svg, and docs/architecture.png
                                          when rsvg-convert is installed (brew install librsvg)
Icons: https://cloud.google.com/icons
"""

import base64
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE.parent / "architecture.svg"
PNG = HERE.parent / "architecture.png"

DY = 174  # lane 1 has a third row (emails); everything below it moves down by this much
W, H = 1640, 1010 + DY
FONT = "Pretendard, 'Apple SD Gothic Neo', 'Noto Sans KR', Arial, sans-serif"
INK, SUB, LINE, BLUE, STEP = "#202124", "#5F6368", "#3C4043", "#1A73E8", "#174EA6"

ICON = {
    "run": "CloudRun-512-color-rgb.svg",
    "storage": "Cloud_Storage-512-color.svg",
    "db": "Databases-512-color.svg",
    "integration": "IntegrationServices-512-color.svg",
    "security": "SecurityIdentity-512-color.svg",
    "devops": "DevOps-512-color.svg",
    "observability": "Observability-512-color.svg",
}

CW, CH = 250, 74  # card size
out: list = []


def icon_uri(key: str) -> str:
    data = (HERE / "icons" / ICON[key]).read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode()


def text(x, y, s, size=14, weight=400, fill=INK, anchor="start"):
    out.append(
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{s}</text>'
    )


def group(x, y, w, h, fill, label):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}"/>')
    text(x + 16, y + 26, label, 14, 700, "#3C4043")


def card(x, y, icon, role, product, w=CW, dashed=False, h=CH):
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    out.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#fff" stroke="{LINE}" stroke-width="1.6"{dash}/>'
    )
    if icon:
        out.append(f'<image x="{x + 14}" y="{y + 17}" width="40" height="40" href="{icon_uri(icon)}"/>')
    tx = x + (66 if icon else 18)
    text(tx, y + 32, role, 15, 700)
    text(tx, y + 54, product, 13, 400, SUB)
    return {"l": x, "r": x + w, "t": y, "b": y + h, "cx": x + w / 2, "cy": y + h / 2}


def arrow(points, label=None, at=None, dashed=False, step=None, step_at=None):
    d = "M " + " L ".join(f"{px} {py}" for px, py in points)
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    out.append(f'<path d="{d}" fill="none" stroke="{LINE}" stroke-width="1.8"{dash} marker-end="url(#head)"/>')
    if label:
        lx, ly = at
        text(lx, ly, label, 12.5, 500, SUB, "middle")
    if step:
        sx, sy = step_at
        out.append(f'<circle cx="{sx}" cy="{sy}" r="12" fill="{STEP}"/>')
        text(sx, sy + 4.5, str(step), 13, 700, "#fff", "middle")


def person(x, y, label):
    # Material "person" glyph (Apache 2.0)
    out.append(
        f'<g transform="translate({x},{y}) scale(1.25)" fill="{SUB}"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></g>'
    )
    text(x + 40, y + 20, label, 14, 600)


# ---------- canvas ----------
out.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    '<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
    f'<path d="M0 0 L10 5 L0 10 z" fill="{LINE}"/></marker></defs>'
    f'<rect width="{W}" height="{H}" fill="#fff"/>'
)

# ---------- outside Google Cloud ----------
out.append(f'<rect x="24" y="96" width="232" height="170" rx="12" fill="#fff" stroke="{LINE}" stroke-width="1.6"/>')
text(42, 126, "사용자 (브라우저)", 14, 700, "#3C4043")
person(44, 146, "회원")
text(84, 182, "봇 확인: reCAPTCHA (선택)", 11.5, 500, SUB)
person(44, 196, "스태프, 운영진")
text(84, 232, "Google, 이메일 코드 로그인", 11.5, 500, SUB)

out.append(
    f'<rect x="24" y="{788 + DY}" width="232" height="190" rx="12" fill="#fff" stroke="{LINE}" stroke-width="1.6"/>'
)
# GitHub mark (Octicons, MIT)
out.append(
    f'<g transform="translate(42,{806 + DY}) scale(1.5)" fill="{INK}"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></g>'
)
text(78, 826 + DY, "GitHub", 15, 700)
gha = {"l": 40, "r": 240, "t": 850 + DY, "b": 960 + DY}
out.append(
    f'<rect x="40" y="{850 + DY}" width="200" height="110" rx="10" fill="#fff" stroke="{LINE}" stroke-width="1.6"/>'
)
text(58, 878 + DY, "GitHub Actions", 15, 700)
text(58, 902 + DY, "검사, 빌드, 배포", 13, 400, SUB)
text(58, 924 + DY, "후보 리비전 smoke test", 13, 400, SUB)
text(58, 946 + DY, "통과 시 트래픽 전환", 13, 400, SUB)


# ---------- legend ----------
def legend(y0, title, items, marker):
    text(30, y0, title, 14, 700, "#3C4043")
    for i, label in enumerate(items):
        y = y0 + 30 + i * 32
        out.append(f'<circle cx="42" cy="{y}" r="11" fill="{STEP}"/>')
        text(42, y + 4.5, marker(i), 12.5, 700, "#fff", "middle")
        text(62, y + 5, label, 13, 500)


legend(
    300,
    "신청 한 건의 흐름",
    [
        "회원이 신청",
        "좌석 확인과 저장 (트랜잭션)",
        "신청 메시지 발행, 즉시 응답",
        "Pub/Sub가 worker 호출 (재시도)",
        "서명된 QR 저장",
        "발급 완료로 변경",
    ],
    lambda i: str(i + 1),
)
legend(
    530,
    "빈 좌석 발생 시",
    [
        "취소 (본인, 또는 housekeeping)",
        "Firestore 변경을 Eventarc가 전달",
        "대기 1번 배정, 발급 메시지 발행",
    ],
    lambda i: "ABC"[i],
)
text(30, 668, "(선택) production_extras = true일 때만 생성", 12.5, 500, SUB)

# ---------- Google Cloud boundary ----------
GX, GY, GW, GH = 290, 24, 1326, 962 + DY
out.append(f'<rect x="{GX}" y="{GY}" width="{GW}" height="{GH}" rx="14" fill="{BLUE}"/>')
text(GX + 22, GY + 32, "Google Cloud", 19, 700, "#fff")
text(GX + GW - 22, GY + 32, "서울 리전 (asia-northeast3)", 13.5, 500, "#E8F0FE", "end")
out.append(f'<rect x="{GX + 8}" y="{GY + 48}" width="{GW - 16}" height="{GH - 56}" rx="10" fill="#fff"/>')

C1, C2, C3, C4 = 330, 640, 950, 1310  # card columns

# lanes
group(312, 86, 912, 330 + DY, "#E8F0FE", "신청, 티켓 발급, 메일 (Pub/Sub)")
group(312, 430 + DY, 912, 150, "#E6F4EA", "대기 명단 자동 배정 (Eventarc)")
group(312, 594 + DY, 912, 150, "#FEF7E0", "정기 작업 (Cloud Scheduler)")
group(1240, 86, 360, 658 + DY, "#F1F3F4", "데이터, 로그, 알림")
group(312, 758 + DY, 1288, 214, "#FCE8E6", "배포 (CI/CD)")

# request path
web = card(C1, 146, "run", "공개 웹 서비스", "Cloud Run: web")
secret = card(C1, 300, "security", "비밀값", "Secret Manager")
topic = card(C2, 146, "integration", "신청 메시지", "Pub/Sub: registrations")
dlq = card(C2, 300, "integration", "실패 메시지 보관", "Pub/Sub: dead-letter", dashed=True)
emails = card(C2, 454, "integration", "메일 요청", "Pub/Sub: emails")
text(C2 + 16, emails["b"] + 22, "web, ticket-worker, housekeeping이 발행", 12.5, 500, SUB)
text(C2 + 16, emails["b"] + 44, "ticket-worker가 Resend SMTP로 발송 (외부)", 12.5, 500, SUB)
worker = card(C3, 146, "run", "티켓 발급 (비공개)", "Cloud Run: ticket-worker")
assets = card(C3, 300, "storage", "QR 코드, 포스터", "Cloud Storage: assets")

# waitlist
promoter = card(C2, 478 + DY, "run", "대기자 배정 (비공개)", "Cloud Run: waitlist-promoter")
trigger = card(C3, 478 + DY, "integration", "변경 이벤트 전달", "Eventarc")

# deadline
sched = card(C2, 642 + DY, "integration", "5분마다 실행", "Cloud Scheduler")
job = card(C3, 642 + DY, "run", "행사별 할 일 처리", "Cloud Run 잡: housekeeping")
text(job["cx"], job["b"] + 20, "pre-warm, 참석 확인 요청, 미확인 자리 해제", 12.5, 600, SUB, "middle")

# data
fs = card(C4, 146, "db", "행사, 신청, 대기 명단", "Firestore", w=270, h=414 + DY)
text(C4 + 18, 230, "트랜잭션으로 좌석 보호", 12.5, 500, SUB)
text(C4 + 18, 252, "계정, 로그인 코드, 발송 기록", 12.5, 500, SUB)
text(C4 + 18, 274, "백업, 시점 복구 7일 (선택)", 12.5, 500, SUB)
text(C4 + 18, 296, "행사 30일 뒤 자동 삭제 (TTL)", 12.5, 500, SUB)
logs = card(C4, 642 + DY, "observability", "로그, SLO와 알림 (선택)", "Cloud Logging, Monitoring", w=270)

# delivery
wif = card(C2, 806 + DY, "security", "키 없는 로그인", "Workload Identity Federation")
registry = card(C3, 806 + DY, "devops", "이미지 저장소", "Artifact Registry")
tf = card(C4, 806 + DY, None, "인프라 코드", "Terraform: infra/", w=270)
text(C4 + 135, 908 + DY, "모든 자원과 권한, state는 Cloud Storage", 12.5, 500, SUB, "middle")

# ---------- arrows: one signup ----------
arrow([(256, web["cy"]), (web["l"], web["cy"])], step=1, step_at=(292, web["cy"] - 20))
arrow([(web["cx"], web["t"]), (web["cx"], 126), (fs["cx"], 126), (fs["cx"], fs["t"])], step=2, step_at=(800, 126))
arrow([(web["r"], web["cy"]), (topic["l"], topic["cy"])], step=3, step_at=(610, web["cy"] - 18))
arrow([(topic["r"], topic["cy"]), (worker["l"], worker["cy"])], step=4, step_at=(920, topic["cy"] - 18))
arrow([(worker["r"], worker["cy"]), (fs["l"], worker["cy"])], step=6, step_at=(1255, worker["cy"] - 18))
arrow([(worker["cx"], worker["b"]), (worker["cx"], assets["t"])], step=5, step_at=(worker["cx"] + 22, 260))
arrow([(topic["cx"], topic["b"]), (topic["cx"], dlq["t"])], dashed=True)
text(topic["cx"] + 10, 272, "5회 실패 시", 12.5, 500, SUB, "start")
arrow([(secret["cx"], secret["t"]), (secret["cx"], web["b"])], dashed=True)
text(secret["cx"] + 10, 272, "환경 변수로 주입", 12.5, 500, SUB, "start")

# ---------- arrows: an email ----------
arrow([(emails["cx"], emails["t"]), (emails["cx"], dlq["b"])], dashed=True)
text(emails["cx"] + 10, 426, "50회 실패 시", 12.5, 500, SUB, "start")
arrow([(emails["r"], emails["cy"]), (920, emails["cy"]), (920, worker["b"] - 13), (worker["l"], worker["b"] - 13)])
text(928, 432, "push /mail", 12.5, 500, SUB, "start")

# ---------- arrows: a seat is freed ----------
arrow([(fs["l"], trigger["cy"]), (trigger["r"], trigger["cy"])], step="B", step_at=(1255, trigger["cy"] - 18))
arrow([(trigger["l"], trigger["cy"]), (promoter["r"], promoter["cy"])])
arrow(
    [(promoter["l"], promoter["cy"]), (612, promoter["cy"]), (612, topic["b"] - 14), (topic["l"], topic["b"] - 14)],
    step="C",
    step_at=(612, 290),
)
arrow([(sched["r"], sched["cy"]), (job["l"], job["cy"])])
arrow(
    [(job["r"], job["cy"]), (1262, job["cy"]), (1262, 540 + DY), (fs["l"], 540 + DY)],
    step="A",
    step_at=(1262, 600 + DY),
)
text(1250, 628 + DY, "미확인 자리 취소", 12.5, 500, SUB, "end")

# ---------- arrows: delivery ----------
arrow([(gha["r"], 870 + DY), (wif["l"], 870 + DY)])
text(440, 862 + DY, "토큰 교환 (키 없음)", 12.5, 500, SUB, "middle")
arrow([(gha["r"], 930 + DY), (900, 930 + DY), (900, registry["b"] - 18), (registry["l"], registry["b"] - 18)])
text(580, 922 + DY, "이미지 4개 push (커밋 SHA 태그)", 12.5, 500, SUB, "middle")
arrow([(registry["cx"], registry["t"]), (registry["cx"], 776 + DY), (1170, 776 + DY), (1170, 748 + DY)])
text(1178, 772 + DY, "web, ticket-worker, waitlist-promoter, housekeeping 이미지 교체", 12.5, 500, SUB, "start")

out.append("</svg>")
OUT.write_text("\n".join(out), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")

# The PNG (for the README and the guides) at twice the size, with the fonts installed here
if shutil.which("rsvg-convert"):
    subprocess.run(["rsvg-convert", "--zoom", "2", "--output", str(PNG), str(OUT)], check=True)
    print(f"wrote {PNG} ({PNG.stat().st_size // 1024} KB)")
else:
    print("rsvg-convert not found: architecture.png not updated")
