"""Cinema Star (cinemastar.am).

Сайт на Laravel Livewire. В исходной странице есть расписание только для
первого филиала и сегодняшней даты. Остальные даты и филиалы подгружаются
вызовами Livewire (`changeCinema`, `$set selectedDate`) на /ru/livewire/update,
ровно как это делает сама страница при клике на вкладки.

У каждого сеанса прямая ссылка на покупку: pay.cinemastar.am/<id>?cinema=...
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import CinemaInfo, FetchResult, RawSession
from ..timeutil import DEFAULT_TZ, local_session_start, tz_of
from ..titles import detect_language
from .base import Adapter, make_http_session, polite_sleep

log = logging.getLogger(__name__)

BASE = "https://cinemastar.am"
HOME_URL = f"{BASE}/ru"
CHAIN = "Cinema Star"

_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_PRICE = re.compile(r"(\d[\d\s]*)\s*(?:др|֏|AMD)", re.I)
_CHANGE_CINEMA = re.compile(r"changeCinema\('(\d+)'\)")
_SELECT_DATE = re.compile(r"selectedDate',\s*'(\d{4}-\d{2}-\d{2})'")
_LANG_NAMES = {"ru": "на русском", "hy": "на армянском", "en": "на английском"}


def cinema_key(cinema_id: int | str) -> str:
    return f"cinemastar:{cinema_id}"


def _clean_hall(text: str) -> tuple[str | None, str]:
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return None, "standard"
    if "vip" in t.lower():
        return "VIP", "vip"
    digits = re.search(r"\d+", t)
    return (f"Зал {digits.group(0)}" if digits else t), "standard"


def parse_screenings(html: str, day: date, key: str, tz_name: str = DEFAULT_TZ) -> list[RawSession]:
    """Разбирает строки таблицы сеансов (и из полной страницы, и из ответа Livewire)."""
    tz = tz_of(tz_name)
    soup = BeautifulSoup(html, "lxml")
    out: list[RawSession] = []
    for tr in soup.find_all("tr"):
        pay = tr.find("a", href=re.compile(r"pay\.cinemastar\.am/"))
        th = tr.find("th")
        if not pay or not th:
            continue
        h2 = th.find("h2")
        tm = _TIME.search(th.get_text(" ", strip=True))
        if not h2 or not tm:
            continue
        title = h2.get_text(" ", strip=True)
        movie_a = th.find("a", href=re.compile(r"/movie/"))
        icon = th.find("img", alt=re.compile(r"language icon"))
        lang = icon["alt"].split()[0] if icon else detect_language(title)
        tds = tr.find_all("td", recursive=False)
        hall, hall_class = _clean_hall(tds[0].get_text(" ", strip=True)) if len(tds) > 0 else (None, "standard")
        fmt_el = tds[1].find("span") if len(tds) > 1 else None
        fmt = fmt_el.get_text(strip=True).upper() if fmt_el else None
        price = None
        if len(tds) > 2:
            pm = _PRICE.search(tds[2].get_text(" ", strip=True).replace("\xa0", " "))
            if pm:
                price = int(re.sub(r"\D", "", pm.group(1)))
        posters = [i["src"] for i in th.find_all("img", src=True) if "language icon" not in i.get("alt", "")]
        portrait = [i["src"] for i in th.find_all("img", src=True) if "lg:hidden" in " ".join(i.get("class", []))]
        url = pay["href"]
        ext_id = urlparse(url).path.strip("/").rsplit("/", 1)[-1]
        out.append(
            RawSession(
                cinema_key=key,
                external_id=ext_id,
                title_raw=title,
                starts_at=local_session_start(day, int(tm.group(1)), int(tm.group(2)), tz),
                url=url,
                price=price,
                hall=hall,
                hall_class=hall_class,
                fmt=fmt,
                language_label=_LANG_NAMES.get(lang or ""),
                poster_url=(portrait or posters or [None])[0],
                movie_url=movie_a["href"] if movie_a else None,
            )
        )
    return out


# ---------------------------------------------------------------- Livewire


@dataclass
class HomeState:
    csrf: str
    update_url: str
    snapshot: str
    cinemas: list[tuple[int, str]]
    dates: list[date]
    selected_cinema: int
    selected_date: date
    html: str  # HTML, из которого можно разобрать сеансы для (selected_cinema, selected_date)


def _selected_from_snapshot(snapshot: str) -> tuple[int, date]:
    data = json.loads(snapshot)["data"]
    return int(data["selectedCinema"]), date.fromisoformat(str(data["selectedDate"])[:10])


def parse_home_state(html: str) -> HomeState:
    soup = BeautifulSoup(html, "lxml")
    csrf_el = soup.find(attrs={"data-csrf": True})
    upd_el = soup.find(attrs={"data-update-uri": True})
    comp = soup.find(attrs={"wire:name": "home.screenings"})
    if not (csrf_el and upd_el and comp and comp.get("wire:snapshot")):
        raise ValueError("cinemastar: на странице нет компонента home.screenings (вёрстка изменилась?)")
    snapshot = comp["wire:snapshot"]
    cinemas: list[tuple[int, str]] = []
    for a in soup.find_all("a"):
        m = _CHANGE_CINEMA.search(a.get("wire:click.prevent", "") or a.get("wire:click", ""))
        if not m:
            continue
        cid = int(m.group(1))
        img = a.find("img", alt=True)
        name = (a.find("p").get_text(strip=True) if a.find("p") else "") or (
            re.sub(r"\s*logo\s*$", "", img["alt"]) if img else f"Cinema Star {cid}"
        )
        if cid not in [c[0] for c in cinemas]:
            cinemas.append((cid, name))
    dates: list[date] = []
    for d in _SELECT_DATE.findall(html):
        dd = date.fromisoformat(d)
        if dd not in dates:
            dates.append(dd)
    sel_cinema, sel_date = _selected_from_snapshot(snapshot)
    return HomeState(
        csrf=csrf_el["data-csrf"],
        update_url=upd_el["data-update-uri"],
        snapshot=snapshot,
        cinemas=cinemas,
        dates=dates,
        selected_cinema=sel_cinema,
        selected_date=sel_date,
        html=html,
    )


class CinemaStarClient:
    """Минимальный клиент Livewire 3: отправляет вызовы методов компонента."""

    def __init__(self, http=None, delay: float = 0.7):
        self.http = http or make_http_session()
        self.delay = delay

    def load_home(self) -> HomeState:
        r = self.http.get(HOME_URL, timeout=30)
        r.raise_for_status()
        return parse_home_state(r.text)

    def call(self, state: HomeState, calls: list[dict] | None = None, updates: dict | None = None) -> HomeState:
        """Один запрос Livewire. `updates` меняют свойства компонента (так страница делает $set),
        `calls` вызывают методы (changeCinema)."""
        payload = {
            "_token": state.csrf,
            "components": [{"snapshot": state.snapshot, "updates": updates or {}, "calls": calls or []}],
        }
        headers = {
            "X-Livewire": "true",
            "X-CSRF-TOKEN": state.csrf,
            "Accept": "application/json",
            "Referer": HOME_URL,
        }
        r = self.http.post(state.update_url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        comp = r.json()["components"][0]
        html = (comp.get("effects") or {}).get("html")
        if not html:
            raise ValueError("cinemastar: Livewire не вернул HTML компонента")
        snapshot = comp["snapshot"]
        cinema_id, day = _selected_from_snapshot(snapshot)
        return replace(state, snapshot=snapshot, selected_cinema=cinema_id, selected_date=day, html=html)

    def switch(self, state: HomeState, cinema_id: int, day: date) -> HomeState:
        # Филиал меняем вызовом метода, дату отдельным запросом через updates: на сайте $set
        # отправляется именно так, а после смены филиала дата могла сброситься.
        if state.selected_cinema != cinema_id:
            state = self.call(state, calls=[{"path": "", "method": "changeCinema", "params": [str(cinema_id)]}])
            polite_sleep(self.delay)
        if state.selected_date != day:
            state = self.call(state, updates={"selectedDate": day.isoformat()})
            polite_sleep(self.delay)
        return state


class CinemaStarAdapter(Adapter):
    name = "cinemastar"

    def __init__(self, tz_name: str = DEFAULT_TZ, days_ahead: int = 7, client: CinemaStarClient | None = None):
        self.tz_name = tz_name
        self.days_ahead = days_ahead
        self.client = client or CinemaStarClient()

    def fetch(self, now: datetime) -> FetchResult:
        result = FetchResult()
        try:
            state = self.client.load_home()
        except Exception as e:
            result.errors.append(f"cinemastar: не удалось загрузить главную: {e}")
            return result

        result.cinemas = [
            CinemaInfo(key=cinema_key(cid), name=name, chain=CHAIN, city="yerevan") for cid, name in state.cinemas
        ]
        # 1) то, что уже есть в исходной странице
        first_key = cinema_key(state.selected_cinema)
        result.sessions += parse_screenings(state.html, state.selected_date, first_key, self.tz_name)
        result.covered.add((first_key, state.selected_date))
        initial = (state.selected_cinema, state.selected_date)

        # 2) остальные филиалы и даты через Livewire
        days = state.dates[: self.days_ahead] or [state.selected_date]
        for cid, _name in state.cinemas:
            key = cinema_key(cid)
            try:
                for day in days:
                    if (cid, day) == initial:
                        continue
                    state = self.client.switch(state, cid, day)
                    if state.selected_cinema != cid or state.selected_date != day:
                        raise ValueError(
                            f"Livewire вернул {state.selected_cinema}/{state.selected_date}, ожидали {cid}/{day}"
                        )
                    result.sessions += parse_screenings(state.html, day, key, self.tz_name)
                    result.covered.add((key, day))
            except Exception as e:
                result.errors.append(f"cinemastar[{cid}]: {e}")
                try:  # сбросим состояние компонента и продолжим со следующего филиала
                    state = self.client.load_home()
                except Exception:
                    break
        return result