"""Привязка названий с сайтов кинотеатров к фильмам TMDb."""
from __future__ import annotations

import logging
from datetime import datetime

from . import db as dbm
from .timeutil import UTC, to_iso_utc
from .titles import clean_title, normalize_title, similarity
from .tmdb import TMDb, TMDbError, film_data

log = logging.getLogger(__name__)


class FilmMatcher:
    def __init__(self, database: dbm.Database, tmdb: TMDb | None, accept: float = 0.9, low: float = 0.72):
        self.db = database
        self.tmdb = tmdb
        self.accept = accept
        self.low = low

    # ---------------------------------------------------------- site titles

    def resolve(self, title_raw: str, site_poster_url: str | None = None, now: datetime | None = None) -> int:
        """Возвращает id фильма для названия с сайта. Бросает TMDbError, если TMDb недоступен."""
        norm = normalize_title(title_raw)
        with self.db.conn() as c:
            row = dbm.find_alias(c, norm)
            if row:
                if site_poster_url:
                    c.execute("UPDATE films SET site_poster_url=COALESCE(site_poster_url, ?) WHERE id=?",
                              (site_poster_url, row["film_id"]))
                return row["film_id"]

        found = self._search(clean_title(title_raw), None) if self.tmdb else None
        now = to_iso_utc(now or datetime.now(UTC))
        with self.db.conn() as c:
            row = dbm.find_alias(c, norm)  # мог появиться за время сетевого запроса
            if row:
                return row["film_id"]
            if found:
                data, confidence = found
                film = dbm.get_film_by_tmdb(c, data["tmdb_id"])
                film_id = film["id"] if film else dbm.insert_film(
                    c, now, confidence=confidence, site_poster_url=site_poster_url, **data)
            else:
                film_id = dbm.insert_film(c, now, title=clean_title(title_raw), confidence="local",
                                          site_poster_url=site_poster_url)
            dbm.add_alias(c, norm, film_id)
            return film_id

    # ---------------------------------------------------------- Letterboxd

    def resolve_for_import(self, title: str, year: int | None) -> int | None:
        """Для импорта профиля: ищем фильм по названию и году, без алиасов."""
        if not self.tmdb:
            return None
        found = self._search(title, year, languages=("en-US", "ru-RU"))
        if not found:
            return None
        data, confidence = found
        now = to_iso_utc(datetime.now(UTC))
        with self.db.conn() as c:
            film = dbm.get_film_by_tmdb(c, data["tmdb_id"])
            return film["id"] if film else dbm.insert_film(c, now, confidence=confidence, **data)

    def resolve_tmdb_id(self, tmdb_id: int) -> int:
        """Фильм по известному TMDb ID (кнопки в результатах поиска)."""
        assert self.tmdb
        with self.db.conn() as c:
            film = dbm.get_film_by_tmdb(c, tmdb_id)
            if film:
                return film["id"]
        details = self.tmdb.details(tmdb_id)
        data = film_data(details, self._fallback_overview(details))
        now = to_iso_utc(datetime.now(UTC))
        with self.db.conn() as c:
            film = dbm.get_film_by_tmdb(c, tmdb_id)
            return film["id"] if film else dbm.insert_film(c, now, confidence="tmdb", **data)

    # ---------------------------------------------------------------- admin

    def link(self, film_id: int, tmdb_id: int) -> int:
        """Ручная привязка локального/сомнительного фильма к TMDb. Возвращает итоговый id."""
        assert self.tmdb
        details = self.tmdb.details(tmdb_id)
        data = film_data(details, self._fallback_overview(details))
        with self.db.conn() as c:
            existing = dbm.get_film_by_tmdb(c, tmdb_id)
            if existing and existing["id"] != film_id:
                dbm.merge_films(c, film_id, existing["id"])
                return existing["id"]
            dbm.update_film(c, film_id, confidence="tmdb", **data)
            return film_id

    # -------------------------------------------------------------- helpers

    def _fallback_overview(self, details: dict) -> str | None:
        if details.get("overview"):
            return None
        try:
            return self.tmdb.details(details["id"], language="en-US").get("overview") or None
        except TMDbError:
            return None

    def _best(self, query: str, results: list[dict]):
        best = None
        for r in results[:8]:
            score = max(similarity(query, r.get("title") or ""), similarity(query, r.get("original_title") or ""))
            key = (score, r.get("popularity") or 0)
            if best is None or key > best[0]:
                best = (key, r)
        return best

    def _search(self, query: str, year: int | None, languages=("ru-RU", "en-US")):
        best = None
        for lang in languages:
            res = self._best(query, self.tmdb.search_movie(query, year, lang))
            if res and (best is None or res[0] > best[0]):
                best = res
            if best and best[0][0] >= self.accept:
                break
        if not best or best[0][0] < self.low:
            return None
        score, cand = best[0][0], best[1]
        details = self.tmdb.details(cand["id"])
        data = film_data(details, self._fallback_overview(details))
        return data, ("tmdb" if score >= self.accept else "low")
