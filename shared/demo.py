"""Sample events for a fresh local run (dev.py) and for the tests.

On Google Cloud nothing is seeded: organizers create events on the admin pages.
"""

import io
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import blob, poster, store, text, tokens
from .config import now
from .models import Event, usual_schedule


def make_event(
    event_id: str = "cloud-night",
    starts_in: timedelta = timedelta(days=7),
    opened_ago: timedelta = timedelta(days=1),
    seat_limit: int = 30,
    waitlist_limit: int = 100,
    published: bool = True,
    **overrides,
) -> Event:
    """A sample event starting at 19:00 a given time from now, on the usual schedule."""
    starts = (now() + starts_in).replace(hour=19, minute=0, second=0, microsecond=0)
    opens = (now() - opened_ago).replace(second=0, microsecond=0)
    values = dict(
        id=event_id,
        title="Cloud Run 실전 나이트",
        summary="클라우드 파트 정규 세션",
        description=(
            "직접 만든 서비스를 Cloud Run에 배포하고, 실제 트래픽이 몰릴 때 무슨 일이 일어나는지 함께 지켜보는 저녁입니다.\n"
            "노트북을 꼭 챙겨 주세요. 간단한 간식이 준비되어 있습니다."
        ),
        venue="연세대학교 학술정보관 5층 세미나룸",
        seat_limit=seat_limit,
        waitlist_limit=waitlist_limit,
        published=published,
        **usual_schedule(starts, opens),
    )
    return Event(**(values | overrides))


# Sample attendees, so a fresh local run looks like a club that holds sessions
NAMES = (
    "김민준 이서연 박도윤 최지우 정하준 강서윤 조은우 윤지민 장시우 임채원 한지호 오수아 서예준 신하은 권주원 "
    "황유진 안건우 송다은 류현우 전소율 홍승민 고윤서 문태윤 양지아 손우진 배나연 백준서 허민서 남궁도현 노예린 "
    "심재원 유하린 구성민 원지안 하승우 차서영 주민혁 우채은 나준호 마예나"
).split()
MAJORS = ("컴퓨터과학과", "산업공학과", "전기전자공학부", "경영학과", "응용통계학과", "수학과", "인공지능학과", "")


def add_attendees(ev, count: int, checked_in: int = 0) -> None:
    """count signups in order; past the seat limit they join the waitlist."""
    for n, name in enumerate(NAMES[:count]):
        reg = store.register(ev, name, f"student{n + 1}@yonsei.example", MAJORS[n % len(MAJORS)])
        if store.mark_issued(reg.id):  # waitlisted signups have no ticket yet
            blob.put(f"tickets/{reg.id}.svg", tokens.ticket_qr(ev.id, reg.id))
        if n < checked_in:
            store.check_in(reg.id)


def _at(days: int, hour: int = 19) -> datetime:
    return (now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


# id, title, summary, description, venue, starts in (days), signups open (days from now),
# seats, signups, checked in, poster color
SESSIONS = [
    (
        "cloud-run-night",
        "Cloud Run 실전 나이트",
        "클라우드 파트 정규 세션",
        "직접 만든 서비스를 Cloud Run에 배포하고, 실제 트래픽이 몰릴 때 무슨 일이 일어나는지 함께 지켜보는 저녁입니다.\n"
        "노트북을 꼭 챙겨 주세요. 간단한 간식이 준비되어 있습니다.",
        "연세대학교 학술정보관 5층 세미나룸",
        7,
        -1,
        30,
        12,
        0,
        "#1f3f8f",
    ),
    (
        "kubernetes-intro",
        "쿠버네티스 입문: Pod부터 Service까지",
        "클라우드 파트 정규 세션",
        "컨테이너를 여러 대에서 운영할 때 쿠버네티스가 무엇을 대신해 주는지, Pod와 Deployment, Service를 직접 만들며 익힙니다.",
        "연세대학교 공학원 B039",
        12,
        -3,
        40,
        38,
        0,
        "#24584a",
    ),
    (
        "serverless-study",
        "서버리스 스터디: Cloud Functions와 Pub/Sub",
        "클라우드 파트 스터디",
        "이벤트가 들어올 때만 실행되는 함수를 만들고, Pub/Sub으로 서비스 사이에 메시지를 주고받는 구조를 실습합니다.",
        "연세대학교 제1공학관 A588",
        9,
        -4,
        20,
        23,
        0,
        "#6b2d5c",
    ),
    (
        "terraform-workshop",
        "Terraform으로 인프라 만들기",
        "클라우드 파트 실습 세션",
        "Terraform으로 Cloud Run 서비스와 Firestore를 코드로 만들고 지워 봅니다. 노트북과 Google 계정이 필요합니다.",
        "연세대학교 공학원 B039",
        21,
        5,
        40,
        0,
        0,
        "#9a4a24",
    ),
    (
        "github-actions-cicd",
        "GitHub Actions로 CI/CD 만들기",
        "클라우드 파트 실습 세션",
        "테스트, 이미지 빌드, 배포까지 push 한 번으로 이어지는 파이프라인을 만들고, 실패했을 때 되돌리는 방법을 살펴봅니다.",
        "연세대학교 학술정보관 5층 세미나룸",
        28,
        12,
        35,
        0,
        0,
        "#2f3a4a",
    ),
    (
        "cloud-cost",
        "클라우드 비용, 어디서 새는가",
        "클라우드 파트 세미나",
        "무료 크레딧이 끝난 뒤 청구서가 어떻게 생기는지, 예산 알림과 정리 스크립트로 비용을 지키는 방법을 이야기합니다.",
        "연세대학교 백양누리 B102",
        35,
        20,
        60,
        0,
        0,
        "#7a5a12",
    ),
    (
        "docker-basics",
        "Docker와 컨테이너 기초",
        "클라우드 파트 정규 세션",
        "컨테이너가 무엇인지, 이미지를 어떻게 만드는지 직접 빌드하며 살펴봤습니다.",
        "연세대학교 제1공학관 A685",
        -14,
        -30,
        40,
        40,
        34,
        "#0f5c6e",
    ),
    (
        "gcp-onboarding",
        "GCP 시작하기: 프로젝트와 결제 설정",
        "클라우드 파트 정규 세션",
        "Google Cloud 프로젝트를 만들고, 결제 계정과 예산 알림, IAM 권한을 안전하게 설정하는 방법을 함께 따라 했습니다.",
        "연세대학교 학술정보관 5층 세미나룸",
        -28,
        -45,
        40,
        36,
        31,
        "#3d3f8f",
    ),
    (
        "linux-shell",
        "리눅스 셸 기초",
        "클라우드 파트 스터디",
        "서버에 접속해 파일을 다루고, 프로세스와 로그를 확인하는 기본 명령을 익혔습니다.",
        "연세대학교 제1공학관 A588",
        -42,
        -60,
        30,
        27,
        25,
        "#4a4a4a",
    ),
]


def seed() -> None:
    """Nine Cloud Part sessions to look at, created only when the database has none."""
    if store.list_events():
        return
    for event_id, title, summary, description, venue, starts, opens, seats, signups, checked, color in SESSIONS:
        starts_at = _at(starts)
        ev = make_event(
            event_id,
            starts_in=timedelta(days=starts),
            title=title,
            summary=summary,
            description=description,
            venue=venue,
            seat_limit=seats,
            waitlist_limit=20,
            opens_at=_at(opens, hour=12).isoformat(),
            starts_at=starts_at.isoformat(),
            ends_at=(starts_at + timedelta(hours=2, minutes=30)).isoformat(),
            closes_at=starts_at.isoformat(),
            confirm_deadline=(starts_at - timedelta(days=1)).replace(hour=18).isoformat(),
        )
        store.create_event(ev)
        add_attendees(ev, signups, checked)
        add_poster(ev, color)


# ---------- sample posters ----------
# Club events come with a poster. These are drawn here so a fresh local run has
# some; they go through the same path as an organizer's upload (shared/poster.py).

FONT_DIRS = [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/usr/share/fonts")]


def _font(weight: str, size: int):
    for folder in FONT_DIRS:
        for path in folder.rglob(f"Pretendard-{weight}.otf") if folder.exists() else []:
            return ImageFont.truetype(str(path), size)
    return None


def _wrap(draw, text: str, font, width: int) -> list:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line]


def poster_image(ev, color: str) -> bytes | None:
    """A square typographic poster: the title large, the date and place below. None without the font."""
    bold, medium = _font("Bold", 96), _font("Medium", 38)
    if bold is None or medium is None:
        return None
    size, margin = 1200, 96
    image = Image.new("RGB", (size, size), color)
    draw = ImageDraw.Draw(image)
    # One quiet shape for depth: a lighter circle reaching in from the top right
    shade = Image.new("RGB", (size, size), "#ffffff")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((size * 0.45, -size * 0.35, size * 1.35, size * 0.55), fill=26)
    image.paste(shade, (0, 0), mask)

    draw.text((margin, margin), "GDGoC Yonsei 클라우드 파트", font=medium, fill="#ffffffcc")
    lines = _wrap(draw, ev.title, bold, size - 2 * margin)
    y = size - margin - 2 * 52 - 40 - len(lines) * 116
    for line in lines:
        draw.text((margin, y), line, font=bold, fill="#ffffff")
        y += 116
    y += 40
    when = f"{text.date(ev.starts)} {text.clock(ev.starts)}"
    draw.text((margin, y), when, font=medium, fill="#ffffff")
    draw.text((margin, y + 56), ev.venue, font=medium, fill="#ffffffb3")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def add_poster(ev, color: str) -> None:
    data = poster_image(ev, color)
    if data is None:
        return
    for name, jpeg in poster.render(data).items():
        blob.put(f"posters/{ev.id}/{name}", jpeg)
    store.update_event(replace(ev, poster_version=now().strftime("%Y%m%d%H%M%S")))


def add_rush_event(minutes: float) -> Event:
    """An event whose signups open a few minutes from now, to watch the opening rush."""
    ev = make_event(
        "rush-demo",
        starts_in=timedelta(days=3),
        opened_ago=-timedelta(minutes=minutes),
        title="오픈 러시 데모",
        summary="신청 오픈 순간의 트래픽을 지켜보는 데모 행사",
    )
    if store.get_event(ev.id):
        store.update_event(ev)
    else:
        store.create_event(ev)
    return ev


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Add sample events to the local database")
    parser.add_argument("--open-in", type=float, help="also add an event whose signups open in this many minutes")
    args = parser.parse_args()
    store.init()
    seed()
    if args.open_in is not None:
        rush = add_rush_event(args.open_in)
        print(f"rush-demo: signups open at {rush.opens_at} (http://localhost:8080/events/rush-demo)")
