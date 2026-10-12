from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Yerevan"
UTC = timezone.utc

# Сеансы после полуночи (00:15, 01:00) кинотеатры показывают в расписании
# предыдущего дня. До этого часа считаем, что сеанс относится к следующим суткам.
NIGHT_CUTOFF_HOUR = 5

WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_RU = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def tz_of(name: str = DEFAULT_TZ) -> ZoneInfo:
    return ZoneInfo(name)


def local_session_start(day: date, hh: int, mm: int, tz: ZoneInfo) -> datetime:
    """Дата расписания + время сеанса на сайте -> aware datetime в зоне кинотеатра."""
    if hh < NIGHT_CUTOFF_HOUR:
        day = day + timedelta(days=1)
    return datetime.combine(day, time(hh % 24, mm), tzinfo=tz)


def schedule_day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(NIGHT_CUTOFF_HOUR, 0), tzinfo=tz)
    return start, start + timedelta(days=1)


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def from_iso_utc(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


def fmt_when(dt_utc: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    """«сегодня, 19:30» / «завтра, 11:15» / «пт 16 окт, 21:00»."""
    local = dt_utc.astimezone(tz)
    now_local = (now or datetime.now(tz)).astimezone(tz)
    delta = (local.date() - now_local.date()).days
    hm = local.strftime("%H:%M")
    if delta == 0:
        return f"сегодня, {hm}"
    if delta == 1:
        return f"завтра, {hm}"
    return f"{WEEKDAYS_RU[local.weekday()]} {local.day} {MONTHS_RU[local.month - 1]}, {hm}"
