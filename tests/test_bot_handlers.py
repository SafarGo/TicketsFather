import asyncio
import io
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta

from tests import aiogram_stub
from tests.test_pipeline import NOW, FakeSender, FakeTMDb, sess

from kinobot import db as dbm
from kinobot.app import App
from kinobot.config import Settings
from kinobot.dispatcher import FeedDispatcher
from kinobot.matcher import FilmMatcher
from kinobot.timeutil import UTC, to_iso_utc


class FakeMessage:
    def __init__(self, user_id=1, text=None, document=None, reply_markup=None):
        self.from_user = type("U", (), {"id": user_id})()
        self.text, self.document, self.reply_markup = text, document, reply_markup
        self.answers, self.edits, self.deleted = [], [], False

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=1, message=None):
        self.data, self.from_user = data, type("U", (), {"id": user_id})()
        self.message = message or FakeMessage(user_id)
        self.toasts = []

    async def answer(self, text=None, show_alert=False):
        self.toasts.append(text)


class FakeBot:
    def __init__(self, payload=b""):
        self.payload = payload

    async def download(self, doc, destination):
        destination.write(self.payload)


class BotHandlersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        aiogram_stub.install()
        import importlib
        cls.botmod = importlib.import_module("kinobot.bot")

    @classmethod
    def tearDownClass(cls):
        aiogram_stub.uninstall()

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = App(Settings(db_path=self.path, admin_chat_id=777))
        self.app.tmdb = FakeTMDb()
        self.app.matcher = FilmMatcher(self.app.db, self.app.tmdb)
        self.sender = FakeSender()
        self.feed = FeedDispatcher(self.app.db, self.app.settings, self.sender)
        self.h = self.botmod.build_router(self.app, self.feed).handlers
        self.db = self.app.db

    def tearDown(self):
        os.unlink(self.path)

    def run_(self, coro):
        return asyncio.run(coro)

    def set_city(self, uid=1):
        self.run_(self.h["on_start"](FakeMessage(uid)))
        self.run_(self.h["on_city_chosen"](FakeCallback("city:yerevan", uid)))

    def test_start_asks_city_then_onboards(self):
        m = FakeMessage()
        self.run_(self.h["on_start"](m))
        self.assertIn("Выберите город", m.answers[0][0])
        self.assertEqual(m.answers[0][1].inline_keyboard[0][0].callback_data, "city:yerevan")
        cb = FakeCallback("city:yerevan")
        self.run_(self.h["on_city_chosen"](cb))
        with self.db.conn() as c:
            self.assertEqual(dbm.get_user(c, 1)["city_id"], "yerevan")
        self.assertIn("Letterboxd", cb.message.answers[0][0])

    def test_text_search_and_add_to_wishlist(self):
        self.set_city()
        m = FakeMessage(text="диггер")
        self.run_(self.h["on_text"](m))
        kb = m.answers[0][1].inline_keyboard
        self.assertEqual(len(kb), 2)  # оба фильма из каталога
        self.assertEqual(kb[0][0].callback_data, "add:wl:1")
        cb = FakeCallback("add:wl:1")
        self.run_(self.h["on_add_from_search"](cb))
        with self.db.conn() as c:
            acts = dbm.user_actions(c, 1)
        self.assertEqual([(a["action"], a["title"]) for a in acts], [("wishlist", "Диггер")])

    def test_post_buttons(self):
        self.set_city()
        with self.db.conn() as c:
            f1 = dbm.insert_film(c, to_iso_utc(NOW), title="А")
            f2 = dbm.insert_film(c, to_iso_utc(NOW), title="Б")
        msg = FakeMessage(reply_markup=self.botmod.InlineKeyboardMarkup(
            inline_keyboard=[[self.botmod.InlineKeyboardButton(text="buy", url="https://x")], []]))
        cb = FakeCallback(f"wl:{f1}", message=msg)
        self.run_(self.h["on_wishlist_button"](cb))
        self.assertEqual(msg.reply_markup.inline_keyboard[1][0].text, "✅ В вишлисте")
        cb2 = FakeCallback(f"ni:{f2}")
        self.run_(self.h["on_not_interested_button"](cb2))
        self.assertTrue(cb2.message.deleted)
        with self.db.conn() as c:
            acts = {a["film_id"]: a["action"] for a in dbm.user_actions(c, 1)}
        self.assertEqual(acts, {f1: "wishlist", f2: "not_interested"})

    def test_settings_roundtrip(self):
        self.set_city()
        cb = FakeCallback("set:ppd:5")
        self.run_(self.h["on_set"](cb))
        cb = FakeCallback("set:cap:2000")
        self.run_(self.h["on_set"](cb))
        with self.db.conn() as c:
            u = dbm.get_user(c, 1)
        self.assertEqual((u["max_posts_per_day"], u["price_cap"]), (5, 2000))
        self.run_(self.h["on_set"](FakeCallback("set:cap:0")))
        with self.db.conn() as c:
            self.assertIsNone(dbm.get_user(c, 1)["price_cap"])
        self.run_(self.h["on_set"](FakeCallback("set:ppd:999")))  # недопустимое значение игнорируется
        with self.db.conn() as c:
            self.assertEqual(dbm.get_user(c, 1)["max_posts_per_day"], 5)

    def test_pause_resume(self):
        self.set_city()
        self.run_(self.h["on_pause"](FakeMessage()))
        with self.db.conn() as c:
            self.assertEqual(dbm.get_user(c, 1)["paused"], 1)
        self.run_(self.h["on_resume"](FakeMessage()))
        with self.db.conn() as c:
            self.assertEqual(dbm.get_user(c, 1)["paused"], 0)

    def test_document_import(self):
        self.set_city()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("watchlist.csv", "Date,Name,Year,Letterboxd URI\n2024-01-01,Диггер,2026,x\n")
            z.writestr("ratings.csv", "Date,Name,Year,Letterboxd URI,Rating\n2024-01-01,Сумерки. Сага. Новолуние,2009,x,1\n")
        doc = type("D", (), {"file_name": "letterboxd-export.zip", "file_size": 1000})()
        m = FakeMessage(document=doc)
        self.run_(self.h["on_document"](m, FakeBot(buf.getvalue())))
        self.assertIn("в вишлист: 1", m.edits[-1])
        self.assertIn("оценок: 1", m.edits[-1])

    def test_document_validation(self):
        self.set_city()
        doc = type("D", (), {"file_name": "photo.jpg", "file_size": 10})()
        m = FakeMessage(document=doc)
        self.run_(self.h["on_document"](m, FakeBot()))
        self.assertIn("zip", m.answers[0][0])
        bad = type("D", (), {"file_name": "x.zip", "file_size": 10})()
        m = FakeMessage(document=bad)
        self.run_(self.h["on_document"](m, FakeBot(b"PK\x03\x04garbage")))
        self.assertIn("архив", m.answers[0][0])

    def test_feed_command_uses_dispatcher(self):
        self.set_city()
        matcher = self.app.matcher
        from tests.test_pipeline import FakeAdapter
        from kinobot.collector import Collector
        Collector(self.db, [FakeAdapter([sess(1, "Диггер", 5, 1000), sess(2, "Диггер", 6, 2500),
                                         sess(3, "Диггер", 7, 2500)])], matcher, "Asia/Yerevan").run_once(NOW)
        # handler использует datetime.now(), поэтому вызываем диспетчер с фиксированным временем напрямую
        sent = self.run_(self.feed.send_for_user(1, NOW, respect_limits=False, limit=3))
        self.assertEqual(sent, 1)
        self.assertIn("ВЫГОДНО", self.sender.posts[0][1].text)
        m = FakeMessage()
        self.run_(self.h["on_feed"](m))  # реальное «сейчас» далеко от тестового расписания: просто подсказка
        self.assertTrue(m.answers)

    def test_admin_commands_are_gated(self):
        self.set_city()
        m = FakeMessage(user_id=5)
        self.run_(self.h["on_stats"](m))
        self.run_(self.h["on_unmatched"](m))
        self.assertEqual(m.answers, [])
        admin = FakeMessage(user_id=777)
        self.run_(self.h["on_stats"](admin))
        self.assertIn("Пользователей: 1", admin.answers[0][0])
        self.run_(self.h["on_unmatched"](admin))
        self.assertIn("привязаны", admin.answers[0 + 1][0])

    def test_sender_translates_forbidden_to_blocked(self):
        from kinobot.dispatcher import SendBlocked
        from kinobot.render import RenderedPost

        class B:
            async def send_message(self, *a, **k):
                raise aiogram_stub.TelegramForbiddenError()

        sender = self.botmod.TelegramSender(B())
        with self.assertRaises(SendBlocked):
            self.run_(sender.send(1, RenderedPost(text="hi")))


if __name__ == "__main__":
    unittest.main()
