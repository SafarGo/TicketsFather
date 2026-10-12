"""Сбор сеансов: адаптеры -> привязка к фильмам -> база."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from . import db as dbm
from .adapters.base import Adapter
from .matcher import FilmMatcher
from .timeutil import UTC, schedule_day_window, to_iso_utc, tz_of
from .titles import normalize_title
from .tmdb import TMDbError

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    source: str
    ok: bool = False
    sessions: int = 0
    new_sessions: int = 0
    unresolved: int = 0
    errors: list[str] = field(default_factory=list)
    alert: str | None = None


class Collector:
    def __init__(self, database: dbm.Database, adapters: list[Adapter], matcher: FilmMatcher, tz_name: str):
        self.db = database
        self.adapters = adapters
        self.matcher = matcher
        self.tz = tz_of(tz_name)
        self._lock = threading.Lock()  # фоновый сбор и админская команда /collect не должны пересекаться

    def run_once(self, now: datetime | None = None) -> list[RunReport]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            reports = [self._run_adapter(a, now) for a in self.adapters if a.enabled]
            if any(r.sessions for r in reports):
                with self.db.conn() as c:
                    # Всё, что мы увидели при самом первом сборе, не считаем «новинками».
                    if dbm.meta_get(c, "bootstrap_at") is None:
                        dbm.meta_set(c, "bootstrap_at", to_iso_utc(now))
            return reports

    def _run_adapter(self, adapter: Adapter, now: datetime) -> RunReport:
        report = RunReport(source=adapter.name)
        started = to_iso_utc(now)
        try:
            res = adapter.fetch(now)
        except Exception as e:  # адаптер не должен ронять весь сбор
            log.exception("adapter %s failed", adapter.name)
            report.errors.append(f"{adapter.name}: {e}")
            return self._finish(report, started, 0)

        report.errors.extend(res.errors)

        with self.db.conn() as c:
            cinema_ids = {info.key: dbm.upsert_cinema(c, info) for info in res.cinemas}

        # 1) Привязка названий к фильмам (сетевые запросы, пока транзакций нет)
        film_by_norm: dict[str, int | None] = {}
        usable = []
        for s in res.sessions:
            if s.starts_at.astimezone(UTC) < now or s.cinema_key not in cinema_ids:
                continue
            norm = normalize_title(s.title_raw)
            if norm not in film_by_norm:
                try:
                    film_by_norm[norm] = self.matcher.resolve(s.title_raw, s.poster_url, now)
                except TMDbError as e:
                    film_by_norm[norm] = None
                    report.errors.append(f"TMDb не ответил для «{s.title_raw}»: {e}")
            if film_by_norm[norm] is None:
                report.unresolved += 1
                continue
            usable.append((s, film_by_norm[norm]))

        # 2) Запись в одной транзакции
        with self.db.conn() as c:
            for s, film_id in usable:
                _, is_new, _ = dbm.upsert_session(c, cinema_ids[s.cinema_key], film_id, s, started)
                report.new_sessions += int(is_new)
            for key, day in res.covered:
                if key not in cinema_ids:
                    continue
                w_start, w_end = schedule_day_window(day, self.tz)
                dbm.deactivate_missing(c, cinema_ids[key], to_iso_utc(w_start), to_iso_utc(w_end), started)
            for chain in {p.chain for p in res.promos}:
                for p in (x for x in res.promos if x.chain == chain):
                    dbm.upsert_promo(c, p, started)
                dbm.deactivate_stale_promos(c, chain, started)

        report.sessions = len(usable)
        return self._finish(report, started, len(res.sessions))

    def _finish(self, report: RunReport, started: str, fetched: int) -> RunReport:
        with self.db.conn() as c:
            prev = dbm.last_ok_run(c, report.source)
            report.ok = report.sessions > 0
            alerts = []
            if report.errors:
                alerts.append("; ".join(report.errors[:3]))
            if prev and prev["sessions_count"] > 0 and report.sessions < 0.3 * prev["sessions_count"]:
                alerts.append(f"сеансов стало {report.sessions} (раньше {prev['sessions_count']})")
            if report.sessions == 0 and not report.errors:
                alerts.append("не найдено ни одного актуального сеанса" + (" (сайт вернул 0)" if fetched == 0 else ""))
            report.alert = f"⚠️ {report.source}: " + " | ".join(alerts) if alerts else None
            dbm.log_run(c, report.source, started, to_iso_utc(datetime.now(UTC)), report.ok, report.sessions,
                        "; ".join(report.errors)[:500] or None)
        log.info("collect %s: ok=%s sessions=%s new=%s unresolved=%s errors=%s",
                 report.source, report.ok, report.sessions, report.new_sessions, report.unresolved, len(report.errors))
        return report
