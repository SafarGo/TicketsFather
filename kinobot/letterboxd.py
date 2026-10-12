"""Импорт экспорта Letterboxd (zip целиком или отдельные CSV)."""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import db as dbm
from .matcher import FilmMatcher
from .timeutil import UTC, to_iso_utc
from .tmdb import TMDbError

KINDS = ("watchlist", "ratings", "watched", "liked")


@dataclass
class LbEntry:
    kind: str  # watchlist | ratings | watched | liked
    name: str
    year: int | None = None
    rating: float | None = None


@dataclass
class ImportSummary:
    wishlist: int = 0
    rated: int = 0
    liked: int = 0
    watched: int = 0
    unresolved: int = 0
    skipped_by_limit: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.wishlist + self.rated + self.liked + self.watched


def _kind_from_name(name: str) -> str | None:
    n = name.lower().replace("\\", "/")
    base = n.rsplit("/", 1)[-1]
    if base == "watchlist.csv":
        return "watchlist"
    if base == "ratings.csv":
        return "ratings"
    if base == "watched.csv":
        return "watched"
    if n.endswith("likes/films.csv") or base in ("liked.csv", "likes.csv", "films.csv"):
        return "liked"
    return None


def parse_csv(kind: str, data: bytes) -> list[LbEntry]:
    text = data.decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("Name") or "").strip()
        if not name:
            continue
        year = None
        if (row.get("Year") or "").strip().isdigit():
            year = int(row["Year"])
        rating = None
        if kind == "ratings":
            try:
                rating = float(row.get("Rating") or "")
            except ValueError:
                continue
        out.append(LbEntry(kind=kind, name=name, year=year, rating=rating))
    return out


def parse_export(filename: str, data: bytes) -> list[LbEntry]:
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        entries: list[LbEntry] = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > 20 * 1024 * 1024:
                    continue
                kind = _kind_from_name(info.filename)
                if kind:
                    entries += parse_csv(kind, z.read(info))
        return entries
    kind = _kind_from_name(filename)
    if kind is None:
        header = data.decode("utf-8-sig", errors="replace").splitlines()[:1]
        kind = "ratings" if header and "Rating" in header[0] else "watched"
    return parse_csv(kind, data)


def select_entries(entries: list[LbEntry], max_items: int = 250) -> tuple[list[LbEntry], int]:
    """Выбирает самое информативное: весь вишлист, лучшие и худшие оценки, лайки, часть просмотренного."""
    watchlist = [e for e in entries if e.kind == "watchlist"][:120]
    ratings = sorted((e for e in entries if e.kind == "ratings"), key=lambda e: e.rating or 0, reverse=True)
    top = ratings[:60]
    bottom = [e for e in ratings[60:] if (e.rating or 3) <= 2.0][-20:]
    liked = [e for e in entries if e.kind == "liked"][:50]
    watched = [e for e in entries if e.kind == "watched"][:40]
    ordered = watchlist + top + bottom + liked + watched
    chosen = ordered[:max_items]
    return chosen, len(ordered) - len(chosen)


def import_entries(
    database: dbm.Database,
    matcher: FilmMatcher,
    user_id: int,
    entries: list[LbEntry],
    max_items: int = 250,
    progress: Callable[[int, int], None] | None = None,
) -> ImportSummary:
    """Блокирующая функция (сетевые запросы к TMDb): вызывать из потока."""
    summary = ImportSummary()
    chosen, summary.skipped_by_limit = select_entries(entries, max_items)
    cache: dict[tuple[str, int | None], int | None] = {}
    now = to_iso_utc(datetime.now(UTC))
    for i, e in enumerate(chosen, 1):
        key = (e.name.lower(), e.year)
        if key not in cache:
            try:
                cache[key] = matcher.resolve_for_import(e.name, e.year)
            except TMDbError as ex:
                cache[key] = None
                if len(summary.errors) < 3:
                    summary.errors.append(str(ex))
        film_id = cache[key]
        if film_id is None:
            summary.unresolved += 1
        else:
            with database.conn() as c:
                if e.kind == "watchlist":
                    dbm.set_action(c, user_id, film_id, "wishlist", now)
                    summary.wishlist += 1
                elif e.kind == "ratings":
                    dbm.set_action(c, user_id, film_id, "rating", now, value=e.rating)
                    summary.rated += 1
                elif e.kind == "liked":
                    dbm.set_action(c, user_id, film_id, "like", now)
                    summary.liked += 1
                else:
                    dbm.set_action(c, user_id, film_id, "watched", now)
                    summary.watched += 1
        if progress and i % 25 == 0:
            progress(i, len(chosen))
    return summary
