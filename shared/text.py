"""Korean date and time wording, shared by the pages and the emails."""

from datetime import datetime

WEEKDAYS = "월화수목금토일"


def as_time(value) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def date(value) -> str:
    """11월 20일 (금)"""
    value = as_time(value)
    return f"{value.month}월 {value.day}일 ({WEEKDAYS[value.weekday()]})"


def clock(value) -> str:
    """오후 7:00"""
    value = as_time(value)
    return f"{'오전' if value.hour < 12 else '오후'} {value.hour % 12 or 12}:{value.minute:02d}"


def datetime_ko(value) -> str:
    """11월 20일 (금) 오후 7:00"""
    return f"{date(value)} {clock(value)}"
