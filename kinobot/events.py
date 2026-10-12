"""Генерация кандидатов в ленту: ВЫ ХОТЕЛИ ПОСМОТРЕТЬ / ВЫГОДНО / НОВИНКА / АКЦИЯ."""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import db as dbm
from .config import Settings
from .taste import build_taste, taste_score
from .timeutil import UTC, from_iso_utc, to_iso_utc, tz_of

KIND_BASE = {"wishlist": 100.0, "deal": 60.0, "new": 40.0, "promo": 30.0}
WATCHED_ACTIONS = {"watched", "rating"}


@dataclass
class Candidate:
    kind: str  # wishlist | deal | new | promo
    key: str  # ключ дедупликации в рамках пользователя
    score: float
    film_id: int | None = None
    session_id: int | None = None
    promo_id: int | None = None
    deal_ref: int | None = None  # «обычная» цена, если сеанс выгодный
    deal_pct: int | None = None


def _upcoming(c, city_id: str, start: datetime, end: datetime):
    return c.execute(
        """SELECT s.id, s.cinema_id, s.film_id, s.starts_at, s.hall_class, s.price, s.badge,
                  f.genres, f.directors, f.release_date, f.first_seen_at
           FROM sessions s
           JOIN cinemas ci ON ci.id = s.cinema_id
           JOIN films f ON f.id = s.film_id
           WHERE s.active = 1 AND ci.city_id = ? AND s.starts_at >= ? AND s.starts_at <= ?
           ORDER BY s.starts_at""",
        (city_id, to_iso_utc(start), to_iso_utc(end)),
    ).fetchall()


def find_deals(sessions, settings: Settings) -> list[tuple]:
    """Сеансы заметно дешевле обычного для этого фильма в этом кинотеатре. -> [(session, ref_price, pct)]"""
    by_cinema = defaultdict(list)
    for s in sessions:
        if s["hall_class"] != "vip" and s["price"]:
            by_cinema[s["cinema_id"]].append(s)
    deals = []
    for lst in by_cinema.values():
        if len(lst) < settings.deal_min_sessions:
            continue
        ref = statistics.median(s["price"] for s in lst)
        for s in lst:
            if s["price"] <= settings.deal_ratio * ref and ref - s["price"] >= settings.deal_min_saving:
                deals.append((s, int(ref), round((1 - s["price"] / ref) * 100)))
    return deals


def pick_session(sessions, now: datetime, price_cap: int | None):
    near = [s for s in sessions if from_iso_utc(s["starts_at"]) <= now + timedelta(days=3)] or list(sessions)
    pool = [s for s in near if s["hall_class"] != "vip"] or near
    if price_cap:
        capped = [s for s in pool if s["price"] is not None and s["price"] <= price_cap]
        pool = capped or pool
    return min(pool, key=lambda s: (s["price"] is None, s["price"] or 0, s["starts_at"]))


def is_new_film(film, bootstrap_at: str | None, now: datetime, tz) -> bool:
    today = now.astimezone(tz).date()
    if film["release_date"]:
        d = date.fromisoformat(film["release_date"])
        return today - timedelta(days=14) <= d <= today + timedelta(days=7)
    # У фильма нет даты релиза (например, местное кино): смотрим, когда он появился в афише,
    # но игнорируем всё, что было в афише на момент запуска бота.
    if bootstrap_at and film["first_seen_at"] > bootstrap_at:
        return from_iso_utc(film["first_seen_at"]) >= now - timedelta(days=5)
    return False


def build_candidates(database: dbm.Database, user_id: int, now: datetime, settings: Settings) -> list[Candidate]:
    now = now.astimezone(UTC)
    tz = tz_of(settings.tz)
    with database.conn() as c:
        user = dbm.get_user(c, user_id)
        if not user or not user["city_id"]:
            return []
        actions = dbm.user_actions(c, user_id)
        sessions = _upcoming(
            c, user["city_id"],
            now + timedelta(minutes=settings.min_lead_minutes),
            now + timedelta(days=settings.horizon_days),
        )
        bootstrap_at = dbm.meta_get(c, "bootstrap_at")
        sent = dbm.sent_keys(c, user_id)
        cooldown_films = dbm.recent_event_films(c, user_id, to_iso_utc(now - timedelta(days=settings.film_cooldown_days)))
        promos = c.execute(
            """SELECT p.* FROM promos p WHERE p.active = 1
               AND EXISTS (SELECT 1 FROM cinemas ci WHERE ci.chain = p.chain AND ci.city_id = ?)""",
            (user["city_id"],),
        ).fetchall()

    taste = build_taste(actions)
    wishlist = {a["film_id"] for a in actions if a["action"] == "wishlist"}
    blocked = {a["film_id"] for a in actions if a["action"] == "not_interested"}
    watched = {a["film_id"] for a in actions if a["action"] in WATCHED_ACTIONS}
    cap = user["price_cap"]

    by_film = defaultdict(list)
    for s in sessions:
        by_film[s["film_id"]].append(s)

    out: list[Candidate] = []
    for film_id, ss in by_film.items():
        if film_id in blocked or film_id in cooldown_films:
            continue
        film = ss[0]
        t = taste_score(taste, film["genres"], film["directors"])
        deals = find_deals(ss, settings)
        best_deal = max(deals, key=lambda d: (d[2], -d[0]["id"])) if deals else None
        in_wl = film_id in wishlist
        seen = film_id in watched

        if in_wl:
            kind, key = "wishlist", f"wl:{film_id}"
            session = pick_session(ss, now, cap)
            if best_deal and (session["price"] is None or best_deal[0]["price"] <= session["price"]):
                session = best_deal[0]
        elif best_deal and not seen and t >= -0.2:
            kind = "deal"
            session = best_deal[0]
            local_day = from_iso_utc(session["starts_at"]).astimezone(tz).date()
            key = f"deal:{film_id}:{local_day.isoformat()}"
        elif not seen and t >= -0.2 and is_new_film(film, bootstrap_at, now, tz):
            kind, key = "new", f"new:{film_id}"
            session = pick_session(ss, now, cap)
        else:
            continue
        if key in sent:
            continue

        deal_for_session = next((d for d in deals if d[0]["id"] == session["id"]), None)
        hours = (from_iso_utc(session["starts_at"]) - now).total_seconds() / 3600
        urgency = 10.0 if hours <= 24 else 5.0 if hours <= 72 else 0.0
        score = KIND_BASE[kind] + 12.0 * t + urgency + (0.3 * deal_for_session[2] if deal_for_session else 0.0)
        out.append(Candidate(
            kind=kind, key=key, score=score, film_id=film_id, session_id=session["id"],
            deal_ref=deal_for_session[1] if deal_for_session else None,
            deal_pct=deal_for_session[2] if deal_for_session else None,
        ))

    if bootstrap_at:
        for p in promos:
            key = f"promo:{p['id']}"
            if key in sent or p["first_seen_at"] <= bootstrap_at:
                continue
            if from_iso_utc(p["first_seen_at"]) < now - timedelta(days=7):
                continue
            out.append(Candidate(kind="promo", key=key, score=KIND_BASE["promo"], promo_id=p["id"]))

    out.sort(key=lambda x: x.score, reverse=True)
    return out
