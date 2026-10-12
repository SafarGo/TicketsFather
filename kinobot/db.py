"""SQLite-хранилище. Все даты хранятся строками ISO в UTC."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .models import CinemaInfo, RawPromo, RawSession
from .timeutil import to_iso_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cities (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cinemas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    chain TEXT NOT NULL,
    city_id TEXT NOT NULL REFERENCES cities(id),
    address TEXT
);
CREATE TABLE IF NOT EXISTS films (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER UNIQUE,
    title TEXT NOT NULL,
    original_title TEXT,
    year INTEGER,
    overview TEXT,
    poster_url TEXT,
    site_poster_url TEXT,
    genres TEXT NOT NULL DEFAULT '[]',
    directors TEXT NOT NULL DEFAULT '[]',
    runtime INTEGER,
    release_date TEXT,
    popularity REAL,
    confidence TEXT NOT NULL DEFAULT 'local',  -- tmdb | low | local
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS film_aliases (
    norm_title TEXT PRIMARY KEY,
    film_id INTEGER NOT NULL REFERENCES films(id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cinema_id INTEGER NOT NULL REFERENCES cinemas(id),
    external_id TEXT NOT NULL,
    film_id INTEGER NOT NULL REFERENCES films(id),
    title_raw TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    hall TEXT,
    hall_class TEXT NOT NULL DEFAULT 'standard',
    fmt TEXT,
    language_label TEXT,
    age TEXT,
    price INTEGER,
    url TEXT NOT NULL,
    badge TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (cinema_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_film ON sessions(film_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(active, starts_at);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    price INTEGER NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    url TEXT NOT NULL,
    image_url TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (chain, url)
);
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    city_id TEXT REFERENCES cities(id),
    created_at TEXT NOT NULL,
    max_posts_per_day INTEGER NOT NULL DEFAULT 3,
    price_cap INTEGER,
    paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(tg_id),
    film_id INTEGER NOT NULL REFERENCES films(id),
    action TEXT NOT NULL,   -- wishlist | not_interested | watched | like | rating
    value REAL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, film_id, action)
);
CREATE TABLE IF NOT EXISTS feed_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(tg_id),
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    film_id INTEGER,
    session_id INTEGER,
    promo_id INTEGER,
    sent_at TEXT NOT NULL,
    UNIQUE (user_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS collector_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    sessions_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""

DEFAULT_CITIES = [("yerevan", "Ереван")]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.init()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def init(self) -> None:
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            for cid, name in DEFAULT_CITIES:
                c.execute("INSERT OR IGNORE INTO cities(id, name) VALUES (?, ?)", (cid, name))


# ------------------------------------------------------------------ meta

def meta_get(c, key: str) -> str | None:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(c, key: str, value: str) -> None:
    c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------- cinemas

def upsert_cinema(c, info: CinemaInfo) -> int:
    c.execute("INSERT OR IGNORE INTO cities(id, name) VALUES (?, ?)", (info.city, info.city.title()))
    row = c.execute("SELECT id FROM cinemas WHERE key=?", (info.key,)).fetchone()
    if row:
        c.execute("UPDATE cinemas SET name=?, chain=?, city_id=?, address=COALESCE(?, address) WHERE id=?",
                  (info.name, info.chain, info.city, info.address, row["id"]))
        return row["id"]
    cur = c.execute("INSERT INTO cinemas(key, name, chain, city_id, address) VALUES (?,?,?,?,?)",
                    (info.key, info.name, info.chain, info.city, info.address))
    return cur.lastrowid


# ------------------------------------------------------------------ films

FILM_FIELDS = {
    "tmdb_id", "title", "original_title", "year", "overview", "poster_url", "site_poster_url",
    "genres", "directors", "runtime", "release_date", "popularity", "confidence",
}


def insert_film(c, first_seen_at: str, **fields) -> int:
    unknown = set(fields) - FILM_FIELDS
    if unknown:
        raise ValueError(f"unknown film fields: {unknown}")
    for k in ("genres", "directors"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    cols = list(fields) + ["first_seen_at"]
    vals = list(fields.values()) + [first_seen_at]
    cur = c.execute(f"INSERT INTO films({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    return cur.lastrowid


def update_film(c, film_id: int, **fields) -> None:
    unknown = set(fields) - FILM_FIELDS
    if unknown:
        raise ValueError(f"unknown film fields: {unknown}")
    for k in ("genres", "directors"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE films SET {sets} WHERE id=?", [*fields.values(), film_id])


def get_film(c, film_id: int):
    return c.execute("SELECT * FROM films WHERE id=?", (film_id,)).fetchone()


def get_film_by_tmdb(c, tmdb_id: int):
    return c.execute("SELECT * FROM films WHERE tmdb_id=?", (tmdb_id,)).fetchone()


def find_alias(c, norm_title: str):
    return c.execute("SELECT film_id FROM film_aliases WHERE norm_title=?", (norm_title,)).fetchone()


def add_alias(c, norm_title: str, film_id: int) -> None:
    c.execute("INSERT OR IGNORE INTO film_aliases(norm_title, film_id) VALUES (?, ?)", (norm_title, film_id))


def merge_films(c, from_id: int, to_id: int) -> None:
    """Переносит сеансы, алиасы и действия пользователей с одного фильма на другой."""
    if from_id == to_id:
        return
    c.execute("UPDATE sessions SET film_id=? WHERE film_id=?", (to_id, from_id))
    c.execute("UPDATE film_aliases SET film_id=? WHERE film_id=?", (to_id, from_id))
    c.execute("UPDATE feed_events SET film_id=? WHERE film_id=?", (to_id, from_id))
    c.execute("UPDATE OR IGNORE user_actions SET film_id=? WHERE film_id=?", (to_id, from_id))
    c.execute("DELETE FROM user_actions WHERE film_id=?", (from_id,))
    c.execute("DELETE FROM films WHERE id=?", (from_id,))


# --------------------------------------------------------------- sessions

def upsert_session(c, cinema_id: int, film_id: int, s: RawSession, now_iso: str) -> tuple[int, bool, bool]:
    """-> (session_id, is_new, price_changed)"""
    starts = to_iso_utc(s.starts_at)
    row = c.execute("SELECT id, price FROM sessions WHERE cinema_id=? AND external_id=?",
                    (cinema_id, s.external_id)).fetchone()
    if row is None:
        cur = c.execute(
            """INSERT INTO sessions(cinema_id, external_id, film_id, title_raw, starts_at, hall, hall_class, fmt,
               language_label, age, price, url, badge, active, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (cinema_id, s.external_id, film_id, s.title_raw, starts, s.hall, s.hall_class, s.fmt,
             s.language_label, s.age, s.price, s.url, s.badge, now_iso, now_iso),
        )
        sid = cur.lastrowid
        if s.price is not None:
            c.execute("INSERT INTO price_history(session_id, price, seen_at) VALUES (?,?,?)", (sid, s.price, now_iso))
        return sid, True, False
    c.execute(
        """UPDATE sessions SET film_id=?, title_raw=?, starts_at=?, hall=?, hall_class=?, fmt=?, language_label=?,
           age=?, price=?, url=?, badge=?, active=1, last_seen_at=? WHERE id=?""",
        (film_id, s.title_raw, starts, s.hall, s.hall_class, s.fmt, s.language_label, s.age, s.price, s.url,
         s.badge, now_iso, row["id"]),
    )
    changed = s.price is not None and s.price != row["price"]
    if changed:
        c.execute("INSERT INTO price_history(session_id, price, seen_at) VALUES (?,?,?)", (row["id"], s.price, now_iso))
    return row["id"], False, changed


def deactivate_missing(c, cinema_id: int, window_start_iso: str, window_end_iso: str, run_start_iso: str) -> int:
    cur = c.execute(
        """UPDATE sessions SET active=0
           WHERE cinema_id=? AND active=1 AND starts_at>=? AND starts_at<? AND last_seen_at<?""",
        (cinema_id, window_start_iso, window_end_iso, run_start_iso),
    )
    return cur.rowcount


def upsert_promo(c, p: RawPromo, now_iso: str) -> int:
    row = c.execute("SELECT id FROM promos WHERE chain=? AND url=?", (p.chain, p.url)).fetchone()
    if row:
        c.execute("UPDATE promos SET title=?, description=?, image_url=?, active=1, last_seen_at=? WHERE id=?",
                  (p.title, p.description, p.image_url, now_iso, row["id"]))
        return row["id"]
    cur = c.execute(
        "INSERT INTO promos(chain, title, description, url, image_url, active, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,1,?,?)",
        (p.chain, p.title, p.description, p.url, p.image_url, now_iso, now_iso),
    )
    return cur.lastrowid


def deactivate_stale_promos(c, chain: str, run_start_iso: str) -> None:
    c.execute("UPDATE promos SET active=0 WHERE chain=? AND last_seen_at<?", (chain, run_start_iso))


# ------------------------------------------------------------------ users

def get_user(c, tg_id: int):
    return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def get_or_create_user(c, tg_id: int, now_iso: str):
    c.execute("INSERT OR IGNORE INTO users(tg_id, created_at) VALUES (?, ?)", (tg_id, now_iso))
    return get_user(c, tg_id)


def set_user_fields(c, tg_id: int, **fields) -> None:
    allowed = {"city_id", "max_posts_per_day", "price_cap", "paused"}
    if set(fields) - allowed:
        raise ValueError("unknown user fields")
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE users SET {sets} WHERE tg_id=?", [*fields.values(), tg_id])


def list_dispatchable_users(c) -> list:
    return c.execute("SELECT * FROM users WHERE city_id IS NOT NULL AND paused=0").fetchall()


# ---------------------------------------------------------------- actions

_EXCLUSIVE = {"wishlist": ["not_interested"], "not_interested": ["wishlist", "like"], "like": ["not_interested"]}


def set_action(c, user_id: int, film_id: int, action: str, now_iso: str, value: float | None = None) -> None:
    for other in _EXCLUSIVE.get(action, []):
        c.execute("DELETE FROM user_actions WHERE user_id=? AND film_id=? AND action=?", (user_id, film_id, other))
    c.execute(
        """INSERT INTO user_actions(user_id, film_id, action, value, created_at) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, film_id, action) DO UPDATE SET value=excluded.value""",
        (user_id, film_id, action, value, now_iso),
    )


def remove_action(c, user_id: int, film_id: int, action: str) -> None:
    c.execute("DELETE FROM user_actions WHERE user_id=? AND film_id=? AND action=?", (user_id, film_id, action))


def user_actions(c, user_id: int) -> list:
    return c.execute(
        """SELECT a.action, a.value, a.film_id, f.title, f.genres, f.directors
           FROM user_actions a JOIN films f ON f.id=a.film_id WHERE a.user_id=?""",
        (user_id,),
    ).fetchall()


# ------------------------------------------------------------------- feed

def count_events_since(c, user_id: int, since_iso: str) -> int:
    return c.execute("SELECT COUNT(*) AS n FROM feed_events WHERE user_id=? AND sent_at>=?",
                     (user_id, since_iso)).fetchone()["n"]


def sent_keys(c, user_id: int) -> set[str]:
    return {r["dedupe_key"] for r in c.execute("SELECT dedupe_key FROM feed_events WHERE user_id=?", (user_id,))}


def recent_event_films(c, user_id: int, since_iso: str) -> set[int]:
    return {
        r["film_id"]
        for r in c.execute(
            "SELECT DISTINCT film_id FROM feed_events WHERE user_id=? AND sent_at>=? AND film_id IS NOT NULL",
            (user_id, since_iso),
        )
    }


def record_event(c, user_id: int, kind: str, key: str, film_id, session_id, promo_id, sent_at: str) -> bool:
    cur = c.execute(
        "INSERT OR IGNORE INTO feed_events(user_id, kind, dedupe_key, film_id, session_id, promo_id, sent_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, kind, key, film_id, session_id, promo_id, sent_at),
    )
    return cur.rowcount == 1


def delete_event(c, user_id: int, key: str) -> None:
    c.execute("DELETE FROM feed_events WHERE user_id=? AND dedupe_key=?", (user_id, key))


# ------------------------------------------------------------- run log

def log_run(c, source: str, started_at: str, finished_at: str, ok: bool, sessions_count: int, error: str | None) -> None:
    c.execute(
        "INSERT INTO collector_runs(source, started_at, finished_at, ok, sessions_count, error) VALUES (?,?,?,?,?,?)",
        (source, started_at, finished_at, int(ok), sessions_count, error),
    )


def last_ok_run(c, source: str):
    return c.execute(
        "SELECT * FROM collector_runs WHERE source=? AND ok=1 ORDER BY id DESC LIMIT 1", (source,)
    ).fetchone()
