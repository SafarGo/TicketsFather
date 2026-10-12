"""CLI: python -m kinobot <команда>

  run                       запустить бота (сбор + рассылка)
  collect                   один раз собрать сеансы и показать отчёт (бот не нужен)
  deals                     показать выгодные сеансы из базы
  parse <сайт> <файл.html>  разобрать сохранённую страницу, чтобы проверить парсер
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import db as dbm
from .app import App
from .config import Settings
from .events import find_deals
from .timeutil import UTC, fmt_when, from_iso_utc, to_iso_utc, tz_of


def cmd_collect(app: App) -> int:
    if not app.tmdb:
        print("⚠️ TMDB_API_KEY не задан: фильмы будут создаваться без постеров и жанров (local).")
    reports = app.collector.run_once()
    for r in reports:
        print(f"{r.source}: ok={r.ok} сеансов={r.sessions} новых={r.new_sessions} без привязки={r.unresolved}")
        for e in r.errors:
            print(f"   ! {e}")
    with app.db.conn() as c:
        print("\nАктивные сеансы по кинотеатрам:")
        for row in c.execute("""SELECT ci.name, COUNT(*) n FROM sessions s JOIN cinemas ci ON ci.id=s.cinema_id
                                WHERE s.active=1 GROUP BY ci.id ORDER BY ci.name"""):
            print(f"  {row['name']}: {row['n']}")
        local = c.execute("SELECT COUNT(*) FROM films WHERE confidence != 'tmdb'").fetchone()[0]
        print(f"\nФильмов без уверенной привязки к TMDb: {local}")
    return 0 if all(r.ok for r in reports) else 1


def cmd_deals(app: App) -> int:
    tz = tz_of(app.settings.tz)
    now = datetime.now(UTC)
    with app.db.conn() as c:
        rows = c.execute(
            """SELECT s.id, s.cinema_id, s.film_id, s.starts_at, s.hall_class, s.price, s.url, ci.name AS cinema, f.title
               FROM sessions s JOIN cinemas ci ON ci.id=s.cinema_id JOIN films f ON f.id=s.film_id
               WHERE s.active=1 AND s.starts_at>=? AND s.starts_at<=?""",
            (to_iso_utc(now), to_iso_utc(now.replace(year=now.year + 1))),
        ).fetchall()
    by_film: dict[int, list] = {}
    for r in rows:
        by_film.setdefault(r["film_id"], []).append(r)
    found = []
    for ss in by_film.values():
        found += find_deals(ss, app.settings)
    found.sort(key=lambda d: (-d[2], d[0]["starts_at"]))
    if not found:
        print("Выгодных сеансов сейчас нет.")
    for s, ref, pct in found[:40]:
        print(f"-{pct:>2}%  {s['price']:>5} ֏ (обычно ~{ref})  {s['title']} · {s['cinema']} · "
              f"{fmt_when(from_iso_utc(s['starts_at']), tz)}  {s['url']}")
    return 0


def cmd_parse(site: str, path: str, tz_name: str) -> int:
    from .adapters import cinemastar, kinomoscow

    html = Path(path).read_text(encoding="utf-8")
    now = datetime.now(tz_of(tz_name))
    if site == "kinomoscow":
        res = kinomoscow.parse_home(html, now, tz_name)
    elif site == "cinemastar":
        state = cinemastar.parse_home_state(html)
        res = type("R", (), {})()
        res.sessions = cinemastar.parse_screenings(html, state.selected_date,
                                                   cinemastar.cinema_key(state.selected_cinema), tz_name)
        res.promos, res.errors = [], []
        print("Филиалы:", state.cinemas)
        print("Даты:", [d.isoformat() for d in state.dates])
    else:
        print("Сайты: kinomoscow, cinemastar")
        return 2
    print(f"Сеансов: {len(res.sessions)}")
    print("Сеансов по датам:", dict(sorted(Counter(s.starts_at.date().isoformat() for s in res.sessions).items())))
    for s in res.sessions[:15]:
        print(f"  {s.starts_at:%d.%m %H:%M}  {s.price or '—':>5}  {s.hall or '':<12} {s.title_raw}  {s.url}")
    for p in getattr(res, "promos", []):
        print(f"  Акция: {p.title} — {p.url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kinobot", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("collect")
    sub.add_parser("deals")
    p = sub.add_parser("parse")
    p.add_argument("site")
    p.add_argument("file")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if args.cmd == "parse":
        return cmd_parse(args.site, args.file, settings.tz)
    app = App(settings)
    if args.cmd == "collect":
        return cmd_collect(app)
    if args.cmd == "deals":
        return cmd_deals(app)
    from .bot import run_bot

    asyncio.run(run_bot(app))
    return 0


if __name__ == "__main__":
    sys.exit(main())
