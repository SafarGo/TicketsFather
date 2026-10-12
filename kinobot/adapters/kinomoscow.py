"""КиноМосква (moscowcinema.com).

Главная страница отдаёт расписание на 7 дней обычным HTML: на каждый день
своя таблица <div id="schedule-N">. У дня есть бейдж («Специальная цена»,
«Премьера»). Ссылка на покупку у каждого сеанса прямая: /ru/halls/<id сеанса>.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from ..models import CinemaInfo, FetchResult, RawPromo, RawSession
from ..timeutil import DEFAULT_TZ, local_session_start, tz_of
from .base import Adapter, make_http_session

log = logging.getLogger(__name__)

HOME_URL = "https://moscowcinema.com/ru"
CINEMA_KEY = "kinomoscow"
CHAIN = "КиноМосква"
CINEMA = CinemaInfo(key=CINEMA_KEY, name="КиноМосква", chain=CHAIN, city="yerevan", address="ул. Абовяна 18")

_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})")


def _price(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def _resolve_date(day: int, month: int, today: date) -> date:
    d = date(today.year, month, day)
    if d < today - timedelta(days=60):  # переход через Новый год
        d = date(today.year + 1, month, day)
    return d


def _hall_class(hall: str) -> str:
    low = hall.lower()
    if "vip" in low:
        return "vip"
    if "малый" in low:
        return "small"
    return "standard"


def _poster_map(soup: BeautifulSoup) -> dict[str, str]:
    posters: dict[str, str] = {}
    for li in soup.select("li.moCarousel__slide"):
        a = li.find("a", href=True)
        img = li.find("img", src=True)
        if not a or not img:
            continue
        if "/ongoing/" in a["href"] or "/upcoming/" in a["href"]:
            posters[a["href"]] = img["src"]
    return posters


def _parse_row(tr, day: date, badge: str | None, tz, posters: dict[str, str]) -> RawSession | None:
    time_el = tr.select_one(".ScheduleTable__time span")
    link = tr.select_one(".ScheduleTable__link a[href]")
    movie_a = tr.select_one(".ScheduleTable__movie a[href]")
    if not (time_el and link and movie_a):
        return None
    m = _TIME.search(time_el.get_text())
    if not m:
        return None
    url = link["href"]
    hall_el = tr.select_one(".ScheduleTable__hall")
    hall = hall_el.get_text(" ", strip=True) if hall_el else ""
    lang_el = tr.select_one(".ScheduleTable__lang")
    age_el = tr.select_one(".ScheduleTable__age")
    price_el = tr.select_one(".ScheduleTable__price")
    movie_url = movie_a["href"]
    return RawSession(
        cinema_key=CINEMA_KEY,
        external_id=url.rstrip("/").rsplit("/", 1)[-1],
        title_raw=movie_a.get_text(strip=True),
        starts_at=local_session_start(day, int(m.group(1)), int(m.group(2)), tz),
        url=url,
        price=_price(price_el.get_text()) if price_el else None,
        hall=hall or None,
        hall_class=_hall_class(hall),
        fmt=None,
        language_label=lang_el.get_text(strip=True) if lang_el else None,
        age=age_el.get_text(strip=True) if age_el else None,
        poster_url=posters.get(movie_url),
        movie_url=movie_url,
        badge=badge,
    )


def parse_promos(soup: BeautifulSoup) -> list[RawPromo]:
    promos = []
    for li in soup.select("li.promoCarousel__slide"):
        a = li.find("a", href=True)
        title_el = li.find("h5")
        if not a or not title_el:
            continue
        p = li.find("p")
        img = li.find("img", src=True)
        promos.append(
            RawPromo(
                chain=CHAIN,
                title=title_el.get_text(strip=True),
                url=a["href"],
                description=p.get_text(" ", strip=True) if p else None,
                image_url=img["src"] if img else None,
            )
        )
    return promos


def parse_home(html: str, now: datetime, tz_name: str = DEFAULT_TZ) -> FetchResult:
    tz = tz_of(tz_name)
    today = now.astimezone(tz).date()
    soup = BeautifulSoup(html, "lxml")
    posters = _poster_map(soup)
    result = FetchResult(cinemas=[CINEMA], promos=parse_promos(soup))

    for li in soup.select("li.Schedule-date__item"):
        sid = li.get("data-id")
        date_el = li.select_one(".date")
        dm = _DATE.search(date_el.get_text(" ", strip=True)) if date_el else None
        if not sid or not dm:
            continue
        day = _resolve_date(int(dm.group(1)), int(dm.group(2)), today)
        badge_el = li.select_one(".schedule-badge")
        badge = badge_el.get_text(strip=True) if badge_el else ""
        wrapper = soup.find(id=sid)
        if wrapper is None or wrapper.find("table") is None:
            # Расписание на этот день ещё не опубликовано: не считаем день покрытым.
            continue
        for tr in wrapper.select("table tbody tr"):
            s = _parse_row(tr, day, badge or None, tz, posters)
            if s:
                result.sessions.append(s)
        result.covered.add((CINEMA_KEY, day))
    return result


class KinoMoscowAdapter(Adapter):
    name = "kinomoscow"

    def __init__(self, tz_name: str = DEFAULT_TZ, http=None):
        self.tz_name = tz_name
        self.http = http or make_http_session()

    def fetch(self, now: datetime) -> FetchResult:
        try:
            r = self.http.get(HOME_URL, timeout=30)
            r.raise_for_status()
        except Exception as e:  # сеть, 5xx и т.п.
            return FetchResult(cinemas=[CINEMA], errors=[f"kinomoscow: не удалось загрузить страницу: {e}"])
        res = parse_home(r.text, now, self.tz_name)
        if not res.sessions:
            res.errors.append("kinomoscow: на странице не найдено ни одного сеанса (возможно, изменилась вёрстка)")
        return res
