import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from kinobot import db as dbm
from kinobot.adapters.base import Adapter
from kinobot.collector import Collector
from kinobot.config import Settings
from kinobot.dispatcher import FeedDispatcher, SendBlocked, in_quiet_hours
from kinobot.events import build_candidates
from kinobot.letterboxd import LbEntry, import_entries
from kinobot.matcher import FilmMatcher
from kinobot.models import CinemaInfo, FetchResult, RawPromo, RawSession
from kinobot.render import CAPTION_LIMIT, render_candidate
from kinobot.timeutil import UTC, to_iso_utc, tz_of

TZ = tz_of("Asia/Yerevan")
NOW = datetime(2026, 10, 11, 8, 30, tzinfo=UTC)  # 12:30 в Ереване, тихие часы не мешают
SETTINGS = Settings(db_path=":unused:")

CATALOG = [
    {"id": 1, "title": "Диггер", "original_title": "Digger", "release_date": "2026-10-08", "popularity": 50,
     "genres": [{"id": 1, "name": "триллер"}], "credits": {"crew": [{"job": "Director", "name": "А. Режиссёр"}]},
     "overview": "Описание про диггера & <тайны>.", "poster_path": "/d.jpg", "runtime": 100},
    {"id": 2, "title": "Сумерки. Сага. Новолуние", "original_title": "The Twilight Saga: New Moon",
     "release_date": "2009-11-20", "popularity": 80, "genres": [{"id": 2, "name": "фэнтези"}],
     "credits": {"crew": []}, "overview": "Старое кино.", "poster_path": "/t.jpg", "runtime": 130},
]


class FakeTMDb:
    def __init__(self, catalog=CATALOG, fail=False):
        self.catalog, self.fail = catalog, fail
        self.calls = 0

    def search_movie(self, query, year=None, language="ru-RU"):
        self.calls += 1
        if self.fail:
            from kinobot.tmdb import TMDbError
            raise TMDbError("down")
        return [{k: v for k, v in m.items() if k in ("id", "title", "original_title", "popularity")}
                for m in self.catalog]

    def details(self, tmdb_id, language="ru-RU"):
        return next(m for m in self.catalog if m["id"] == tmdb_id)


class FakeAdapter(Adapter):
    name = "fake"

    def __init__(self, sessions, promos=None):
        self.sessions, self.promos = sessions, promos or []

    def fetch(self, now):
        covered = {("fake:1", s.starts_at.astimezone(TZ).date()) for s in self.sessions}
        return FetchResult(sessions=list(self.sessions), covered=covered, promos=list(self.promos),
                           cinemas=[CinemaInfo("fake:1", "Тест-кино", "Тест", "yerevan")])


def sess(ext, title, hours, price, hall_class="standard", badge=None):
    return RawSession(cinema_key="fake:1", external_id=str(ext), title_raw=title,
                      starts_at=(NOW + timedelta(hours=hours)).astimezone(TZ), url=f"https://x.test/{ext}",
                      price=price, hall="Зал 1", hall_class=hall_class, fmt="2D", language_label="на русском",
                      badge=badge)


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = dbm.Database(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def user(self, tg=1, **fields):
        with self.db.conn() as c:
            dbm.get_or_create_user(c, tg, to_iso_utc(NOW))
            dbm.set_user_fields(c, tg, city_id="yerevan", **fields)
        return tg

    def collect(self, sessions, tmdb=None, promos=None, now=NOW):
        matcher = FilmMatcher(self.db, tmdb)
        col = Collector(self.db, [FakeAdapter(sessions, promos)], matcher, "Asia/Yerevan")
        return col.run_once(now)[0]


class MatcherTest(Base):
    def test_ru_and_en_titles_resolve_to_one_film(self):
        m = FilmMatcher(self.db, FakeTMDb())
        a = m.resolve("Диггер (RUS)")
        b = m.resolve("Digger")
        c = m.resolve("Диггер")
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        with self.db.conn() as conn:
            f = dbm.get_film(conn, a)
        self.assertEqual((f["tmdb_id"], f["confidence"], f["year"]), (1, "tmdb", 2026))
        self.assertEqual(f["poster_url"], "https://image.tmdb.org/t/p/w500/d.jpg")

    def test_alias_cache_avoids_repeat_tmdb_calls(self):
        t = FakeTMDb()
        m = FilmMatcher(self.db, t)
        m.resolve("Диггер (RUS)")
        calls = t.calls
        m.resolve("Диггер (ARM)")  # те же нормализованные слова
        self.assertEqual(t.calls, calls)

    def test_unknown_film_becomes_local(self):
        m = FilmMatcher(self.db, FakeTMDb())
        fid = m.resolve("Белая рубашка (ARM)")
        with self.db.conn() as conn:
            f = dbm.get_film(conn, fid)
        self.assertEqual((f["tmdb_id"], f["confidence"], f["title"]), (None, "local", "Белая рубашка"))

    def test_no_tmdb_key_gives_local_films(self):
        fid = FilmMatcher(self.db, None).resolve("Диггер (RUS)")
        with self.db.conn() as conn:
            self.assertEqual(dbm.get_film(conn, fid)["confidence"], "local")

    def test_tmdb_outage_raises_and_pollutes_nothing(self):
        from kinobot.tmdb import TMDbError
        m = FilmMatcher(self.db, FakeTMDb(fail=True))
        with self.assertRaises(TMDbError):
            m.resolve("Диггер")
        with self.db.conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM films").fetchone()["n"], 0)

    def test_link_merges_into_existing_tmdb_film(self):
        m = FilmMatcher(self.db, FakeTMDb())
        good = m.resolve("Диггер")
        local = FilmMatcher(self.db, None).resolve("Digger Extra Cut")
        self.assertNotEqual(good, local)
        self.assertEqual(m.link(local, 1), good)
        with self.db.conn() as conn:
            self.assertIsNone(dbm.get_film(conn, local))
            self.assertEqual(dbm.find_alias(conn, "digger extra cut")["film_id"], good)


class CollectorTest(Base):
    def test_collect_upsert_and_deactivate(self):
        s1, s2 = sess(1, "Диггер", 5, 2500), sess(2, "Диггер", 6, 2500)
        r = self.collect([s1, s2], FakeTMDb())
        self.assertEqual((r.sessions, r.new_sessions, r.ok), (2, 2, True))
        later = NOW + timedelta(minutes=30)
        r = self.collect([s1], FakeTMDb(), now=later)  # сеанс 2 пропал с сайта
        with self.db.conn() as c:
            rows = {x["external_id"]: x["active"] for x in c.execute("SELECT external_id, active FROM sessions")}
        self.assertEqual(rows, {"1": 1, "2": 0})
        self.assertIsNone(r.alert)

    def test_price_history_only_on_change(self):
        self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb())
        self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb(), now=NOW + timedelta(minutes=30))
        self.collect([sess(1, "Диггер", 5, 1500)], FakeTMDb(), now=NOW + timedelta(minutes=60))
        with self.db.conn() as c:
            prices = [r["price"] for r in c.execute("SELECT price FROM price_history ORDER BY id")]
        self.assertEqual(prices, [2500, 1500])

    def test_past_sessions_skipped_and_zero_alert(self):
        r = self.collect([sess(1, "Диггер", -3, 2500)], FakeTMDb())
        self.assertEqual(r.sessions, 0)
        self.assertIsNotNone(r.alert)

    def test_tmdb_outage_skips_sessions_without_crash(self):
        r = self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb(fail=True))
        self.assertEqual((r.sessions, r.unresolved), (0, 1))
        self.assertTrue(r.errors)

    def test_bootstrap_marker_set_once(self):
        self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb())
        with self.db.conn() as c:
            first = dbm.meta_get(c, "bootstrap_at")
        self.assertIsNotNone(first)
        self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb(), now=NOW + timedelta(hours=1))
        with self.db.conn() as c:
            self.assertEqual(dbm.meta_get(c, "bootstrap_at"), first)


class EventsTest(Base):
    def setup_schedule(self):
        sessions = [
            sess(1, "Диггер (RUS)", 5, 1000), sess(2, "Диггер (RUS)", 7, 2500), sess(3, "Диггер (RUS)", 9, 2500),
            sess(4, "Диггер (RUS)", 26, 3000), sess(5, "Диггер (RUS)", 8, 6000, hall_class="vip"),
            sess(6, "Сумерки. Сага. Новолуние (RUS)", 10, 3000), sess(7, "Сумерки. Сага. Новолуние (RUS)", 12, 3000),
            sess(8, "Белая рубашка (ARM)", 11, 2000),
        ]
        self.collect(sessions, FakeTMDb())

    def film_id(self, title):
        with self.db.conn() as c:
            return c.execute("SELECT id FROM films WHERE title=?", (title,)).fetchone()["id"]

    def test_deal_detected_and_vip_ignored(self):
        self.setup_schedule()
        u = self.user()
        cands = build_candidates(self.db, u, NOW, SETTINGS)
        deal = [c for c in cands if c.kind == "deal"]
        self.assertEqual(len(deal), 1)
        d = deal[0]
        self.assertEqual((d.deal_ref, d.deal_pct), (2500, 60))
        with self.db.conn() as c:
            self.assertEqual(c.execute("SELECT price FROM sessions WHERE id=?", (d.session_id,)).fetchone()["price"], 1000)

    def test_new_release_and_old_rerelease(self):
        self.setup_schedule()
        u = self.user()
        kinds = {self.title_of(c): c.kind for c in build_candidates(self.db, u, NOW, SETTINGS) if c.film_id}
        # Диггер вышел 08.10: сеанс выгодный, поэтому «выгодно» приоритетнее «новинки»
        self.assertEqual(kinds["Диггер"], "deal")
        # «Сумерки» — ре-релиз 2009 года, не новинка; «Белая рубашка» была в афише при запуске, тоже не новинка
        self.assertNotIn("Сумерки. Сага. Новолуние", kinds)
        self.assertNotIn("Белая рубашка", kinds)

    def title_of(self, cand):
        with self.db.conn() as c:
            return dbm.get_film(c, cand.film_id)["title"]

    def test_wishlist_has_top_priority_and_cheapest_session(self):
        self.setup_schedule()
        u = self.user()
        with self.db.conn() as c:
            dbm.set_action(c, u, self.film_id("Сумерки. Сага. Новолуние"), "wishlist", to_iso_utc(NOW))
        cands = build_candidates(self.db, u, NOW, SETTINGS)
        self.assertEqual(cands[0].kind, "wishlist")
        self.assertEqual(self.title_of(cands[0]), "Сумерки. Сага. Новолуние")

    def test_not_interested_and_watched_are_excluded(self):
        self.setup_schedule()
        u = self.user()
        fid = self.film_id("Диггер")
        with self.db.conn() as c:
            dbm.set_action(c, u, fid, "not_interested", to_iso_utc(NOW))
        self.assertFalse(any(c.film_id == fid for c in build_candidates(self.db, u, NOW, SETTINGS)))
        with self.db.conn() as c:
            dbm.remove_action(c, u, fid, "not_interested")
            dbm.set_action(c, u, fid, "rating", to_iso_utc(NOW), value=4.5)
        self.assertFalse(any(c.film_id == fid for c in build_candidates(self.db, u, NOW, SETTINGS)))

    def test_wishlist_and_not_interested_are_exclusive(self):
        self.setup_schedule()
        u = self.user()
        fid = self.film_id("Диггер")
        with self.db.conn() as c:
            dbm.set_action(c, u, fid, "wishlist", to_iso_utc(NOW))
            dbm.set_action(c, u, fid, "not_interested", to_iso_utc(NOW))
            acts = [a["action"] for a in dbm.user_actions(c, u)]
        self.assertEqual(acts, ["not_interested"])

    def test_sessions_too_soon_or_inactive_are_not_offered(self):
        self.collect([sess(1, "Диггер", 0.3, 1000), sess(2, "Диггер", 5, 2500), sess(3, "Диггер", 6, 2500),
                      sess(4, "Диггер", 7, 2500)], FakeTMDb())
        u = self.user()
        for c in build_candidates(self.db, u, NOW, SETTINGS):
            with self.db.conn() as conn:
                row = conn.execute("SELECT external_id FROM sessions WHERE id=?", (c.session_id,)).fetchone()
            self.assertNotEqual(row["external_id"], "1")

    def test_taste_prefers_liked_genre(self):
        self.setup_schedule()
        u = self.user()
        with self.db.conn() as c:
            thriller = dbm.insert_film(c, to_iso_utc(NOW), title="Другой триллер", genres=["триллер"])
            fantasy = dbm.insert_film(c, to_iso_utc(NOW), title="Другое фэнтези", genres=["фэнтези"])
            dbm.set_action(c, u, thriller, "like", to_iso_utc(NOW))
            dbm.set_action(c, u, fantasy, "not_interested", to_iso_utc(NOW))
        cands = build_candidates(self.db, u, NOW, SETTINGS)
        self.assertEqual(self.title_of(cands[0]), "Диггер")

    def test_new_promo_only_after_bootstrap(self):
        promo = RawPromo("Тест", "Новая акция", "https://x.test/promo", "Скидка 50%")
        self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb(), promos=[promo])  # промо при запуске: не новость
        u = self.user()
        self.assertFalse(any(c.kind == "promo" for c in build_candidates(self.db, u, NOW, SETTINGS)))
        later = NOW + timedelta(hours=2)
        new_promo = RawPromo("Тест", "Свежая акция", "https://x.test/promo2", "Скидка 30%")
        self.collect([sess(1, "Диггер", 5, 2500)], FakeTMDb(), promos=[promo, new_promo], now=later)
        promos = [c for c in build_candidates(self.db, u, later, SETTINGS) if c.kind == "promo"]
        self.assertEqual(len(promos), 1)


class FakeSender:
    def __init__(self, block=False, fail=False):
        self.posts, self.block, self.fail = [], block, fail

    async def send(self, user_id, post):
        if self.block:
            raise SendBlocked()
        if self.fail:
            raise RuntimeError("telegram down")
        self.posts.append((user_id, post))


class DispatcherTest(EventsTest):
    def run_tick(self, sender, now=NOW):
        return asyncio.run(FeedDispatcher(self.db, SETTINGS, sender).tick(now))

    def test_sends_dedupes_and_respects_daily_cap(self):
        self.setup_schedule()
        u = self.user(max_posts_per_day=2)
        with self.db.conn() as c:
            dbm.set_action(c, u, self.film_id("Сумерки. Сага. Новолуние"), "wishlist", to_iso_utc(NOW))
        sender = FakeSender()
        self.assertEqual(self.run_tick(sender), 2)
        self.assertEqual(self.run_tick(sender), 0)  # дневной лимит исчерпан
        self.assertEqual(self.run_tick(sender, NOW + timedelta(days=1)), 0)  # в новый день уже нечего слать
        self.assertEqual(len(sender.posts), 2)

    def test_quiet_hours(self):
        self.setup_schedule()
        self.user()
        sender = FakeSender()
        night = datetime(2026, 10, 11, 20, 30, tzinfo=UTC)  # 00:30 в Ереване
        self.assertTrue(in_quiet_hours(night, TZ, 23, 9))
        self.assertFalse(in_quiet_hours(NOW, TZ, 23, 9))
        self.assertEqual(self.run_tick(sender, night), 0)

    def test_blocked_user_is_paused(self):
        self.setup_schedule()
        u = self.user()
        self.run_tick(FakeSender(block=True))
        with self.db.conn() as c:
            self.assertEqual(dbm.get_user(c, u)["paused"], 1)

    def test_send_failure_allows_retry(self):
        self.setup_schedule()
        self.user()
        self.assertEqual(self.run_tick(FakeSender(fail=True)), 0)
        sender = FakeSender()
        self.assertGreater(self.run_tick(sender), 0)

    def test_user_without_city_gets_nothing(self):
        self.setup_schedule()
        with self.db.conn() as c:
            dbm.get_or_create_user(c, 5, to_iso_utc(NOW))
        sender = FakeSender()
        self.run_tick(sender)
        self.assertEqual(sender.posts, [])


class RenderTest(EventsTest):
    def test_post_structure_and_limits(self):
        long = dict(CATALOG[0], title="Tom & Jerry <3", original_title="Tom & Jerry", overview="Очень длинное. " * 200)
        self.collect([sess(1, "Tom & Jerry <3", 5, 1000), sess(2, "Tom & Jerry <3", 6, 2500),
                      sess(3, "Tom & Jerry <3", 7, 2500)], FakeTMDb([long]))
        u = self.user()
        cand = build_candidates(self.db, u, NOW, SETTINGS)[0]
        post = render_candidate(self.db, cand, TZ, NOW)
        self.assertLessEqual(len(post.text), CAPTION_LIMIT)
        self.assertIn("ВЫГОДНО", post.text)
        self.assertIn("Tom &amp; Jerry &lt;3", post.text)
        self.assertIn("на 60% дешевле обычного", post.text)
        self.assertIn("сегодня, 17:30", post.text)
        self.assertEqual(post.photo_url, "https://image.tmdb.org/t/p/w500/d.jpg")
        buy = post.buttons[0][0]
        self.assertEqual(buy.url, "https://x.test/1")
        self.assertIn("1000", buy.text)
        data = [b.callback_data for b in post.buttons[1]]
        self.assertTrue(data[0].startswith("wl:") and data[1].startswith("ni:"))
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))


class ImportTest(Base):
    def test_import_creates_actions_and_taste(self):
        u = self.user()
        m = FilmMatcher(self.db, FakeTMDb())
        entries = [LbEntry("watchlist", "Диггер", 2026), LbEntry("ratings", "Сумерки. Сага. Новолуние", 2009, 1.0),
                   LbEntry("watched", "Совсем неизвестный фильм", 1999)]
        s = import_entries(self.db, m, u, entries)
        self.assertEqual((s.wishlist, s.rated), (1, 1))
        with self.db.conn() as c:
            acts = {a["action"]: a["value"] for a in dbm.user_actions(c, u)}
        self.assertEqual(acts["rating"], 1.0)
        self.assertIn("wishlist", acts)

    def test_import_without_tmdb_counts_unresolved(self):
        u = self.user()
        s = import_entries(self.db, FilmMatcher(self.db, None), u, [LbEntry("watchlist", "Диггер", 2026)])
        self.assertEqual((s.total, s.unresolved), (0, 1))


if __name__ == "__main__":
    unittest.main()
