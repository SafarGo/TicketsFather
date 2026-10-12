from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class CinemaInfo:
    key: str
    name: str
    chain: str
    city: str = "yerevan"
    address: str | None = None


@dataclass
class RawSession:
    """Сеанс в том виде, как его отдал сайт кинотеатра (до привязки к фильму)."""

    cinema_key: str
    external_id: str
    title_raw: str
    starts_at: datetime  # tz-aware
    url: str  # прямая ссылка на покупку билета на этот сеанс
    price: int | None = None  # в драмах (AMD)
    hall: str | None = None
    hall_class: str = "standard"  # standard | vip | small
    fmt: str | None = None  # 2D / 3D / IMAX
    language_label: str | None = None
    age: str | None = None
    poster_url: str | None = None
    movie_url: str | None = None
    badge: str | None = None  # например «Специальная цена»


@dataclass
class RawPromo:
    chain: str
    title: str
    url: str
    description: str | None = None
    image_url: str | None = None


@dataclass
class FetchResult:
    sessions: list[RawSession] = field(default_factory=list)
    # (cinema_key, дата расписания), которые удалось загрузить полностью.
    # Только внутри этих дат сеансы, пропавшие с сайта, считаются отменёнными.
    covered: set[tuple[str, date]] = field(default_factory=set)
    promos: list[RawPromo] = field(default_factory=list)
    cinemas: list[CinemaInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
