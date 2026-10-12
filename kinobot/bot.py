"""Telegram-бот на aiogram 3. Вся бизнес-логика лежит в других модулях, здесь только интерфейс."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import time
import zipfile
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from . import db as dbm
from .app import App
from .dispatcher import FeedDispatcher, SendBlocked
from .letterboxd import import_entries, parse_export
from .render import Button, RenderedPost
from .timeutil import UTC, to_iso_utc
from .tmdb import TMDbError

log = logging.getLogger(__name__)

MAX_UPLOAD = 20 * 1024 * 1024  # лимит Telegram на скачивание файлов ботом
ALERT_REPEAT_SECONDS = 6 * 3600
UNMATCHED_SQL = "SELECT COUNT(*) FROM films WHERE confidence != 'tmdb'"

WELCOME = (
    "Привет! Я слежу за афишей кинотеатров и присылаю сеансы, которые стоит не пропустить: "
    "выгодные билеты, новинки и акции, с прямой ссылкой на покупку.\n\n"
    "Выберите город:"
)
ONBOARDING = (
    "Город сохранён ✅\n\n"
    "Чтобы подборки были по вкусу, расскажите, что вы любите:\n"
    "• пришлите файл экспорта Letterboxd (zip-архив или watchlist.csv / ratings.csv) — "
    "в Letterboxd: Settings → Data → Export your data;\n"
    "• или просто напишите название фильма, и я предложу добавить его в вишлист.\n\n"
    "Команды: /feed — показать подборку сейчас, /wishlist — ваш вишлист, /settings — настройки, "
    "/pause — выключить посты."
)


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def to_markup(buttons: list[list[Button]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=b.text, url=b.url) if b.url
            else InlineKeyboardButton(text=b.text, callback_data=b.callback_data or "noop")
            for b in row
        ]
        for row in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TelegramSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, user_id: int, post: RenderedPost) -> None:
        markup = to_markup(post.buttons)
        try:
            if post.photo_url:
                try:
                    await self.bot.send_photo(user_id, post.photo_url, caption=post.text, reply_markup=markup)
                    return
                except TelegramBadRequest:
                    log.info("photo rejected for %s, sending text only", user_id)
            await self.bot.send_message(
                user_id, post.text, reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramForbiddenError as e:
            raise SendBlocked() from e
        finally:
            await asyncio.sleep(0.05)  # мягко держимся в пределах лимитов Telegram


def build_router(app: App, feed: FeedDispatcher) -> Router:
    r = Router()
    db = app.db

    def now_iso() -> str:
        return to_iso_utc(datetime.now(UTC))

    def ensure_user(tg_id: int):
        with db.conn() as c:
            return dbm.get_or_create_user(c, tg_id, now_iso())

    def city_keyboard() -> InlineKeyboardMarkup:
        with db.conn() as c:
            cities = c.execute("SELECT id, name FROM cities ORDER BY name").fetchall()
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=x["name"], callback_data=f"city:{x['id']}")] for x in cities]
        )

    # --------------------------------------------------------------- start

    @r.message(CommandStart())
    async def on_start(m: Message):
        user = ensure_user(m.from_user.id)
        if user["city_id"]:
            await m.answer("С возвращением! Пришлите экспорт Letterboxd или название фильма, "
                           "или нажмите /feed, чтобы увидеть подборку.")
        else:
            await m.answer(WELCOME, reply_markup=city_keyboard())

    @r.message(Command("city"))
    async def on_city(m: Message):
        ensure_user(m.from_user.id)
        await m.answer("Выберите город:", reply_markup=city_keyboard())

    @r.callback_query(F.data.startswith("city:"))
    async def on_city_chosen(cb: CallbackQuery):
        ensure_user(cb.from_user.id)
        with db.conn() as c:
            dbm.set_user_fields(c, cb.from_user.id, city_id=cb.data.split(":", 1)[1])
        await cb.answer("Готово")
        await cb.message.answer(ONBOARDING)

    # ------------------------------------------------------- feed & lists

    @r.message(Command("feed"))
    async def on_feed(m: Message):
        user = ensure_user(m.from_user.id)
        if not user["city_id"]:
            await m.answer("Сначала выберите город:", reply_markup=city_keyboard())
            return
        sent = await feed.send_for_user(m.from_user.id, datetime.now(UTC), respect_limits=False, limit=3)
        if not sent:
            await m.answer("Пока нет новых подходящих предложений. Добавьте фильмы в вишлист или пришлите "
                           "экспорт Letterboxd, и подборка станет точнее.")

    @r.message(Command("wishlist"))
    async def on_wishlist(m: Message):
        user = ensure_user(m.from_user.id)
        with db.conn() as c:
            rows = c.execute(
                """SELECT f.title, f.year,
                          (SELECT COUNT(*) FROM sessions s JOIN cinemas ci ON ci.id = s.cinema_id
                           WHERE s.film_id = f.id AND s.active = 1 AND s.starts_at >= ? AND ci.city_id = ?) AS n
                   FROM user_actions a JOIN films f ON f.id = a.film_id
                   WHERE a.user_id = ? AND a.action = 'wishlist' ORDER BY n DESC, f.title LIMIT 40""",
                (now_iso(), user["city_id"], m.from_user.id),
            ).fetchall()
        if not rows:
            await m.answer("Вишлист пуст. Напишите название фильма или пришлите экспорт Letterboxd.")
            return
        lines = [
            f"• {esc(x['title'])}" + (f" ({x['year']})" if x["year"] else "")
            + (" — сейчас в афише 🎟" if x["n"] else "")
            for x in rows
        ]
        await m.answer("<b>Ваш вишлист</b>\n" + "\n".join(lines))

    # ------------------------------------------------------------ settings

    def settings_view(user) -> tuple[str, InlineKeyboardMarkup]:
        cap = f"{user['price_cap']} ֏" if user["price_cap"] else "без ограничения"
        text = (f"<b>Настройки</b>\nПостов в день: {user['max_posts_per_day']}\nЛимит цены билета: {cap}\n"
                f"Посты: {'выключены' if user['paused'] else 'включены'}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{'• ' if user['max_posts_per_day'] == n else ''}{n} в день",
                                  callback_data=f"set:ppd:{n}") for n in (1, 3, 5)],
            [InlineKeyboardButton(text=("• " if (user["price_cap"] or 0) == n else "") + ("любая цена" if n == 0 else f"до {n}"),
                                  callback_data=f"set:cap:{n}") for n in (0, 2000, 3000)],
        ])
        return text, kb

    @r.message(Command("settings"))
    async def on_settings(m: Message):
        text, kb = settings_view(ensure_user(m.from_user.id))
        await m.answer(text, reply_markup=kb)

    @r.callback_query(F.data.startswith("set:"))
    async def on_set(cb: CallbackQuery):
        _, key, value = cb.data.split(":")
        with db.conn() as c:
            if key == "ppd" and value in ("1", "3", "5"):
                dbm.set_user_fields(c, cb.from_user.id, max_posts_per_day=int(value))
            elif key == "cap" and value.isdigit():
                dbm.set_user_fields(c, cb.from_user.id, price_cap=int(value) or None)
            user = dbm.get_user(c, cb.from_user.id)
        text, kb = settings_view(user)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await cb.answer("Сохранено")

    @r.message(Command("pause"))
    async def on_pause(m: Message):
        ensure_user(m.from_user.id)
        with db.conn() as c:
            dbm.set_user_fields(c, m.from_user.id, paused=1)
        await m.answer("Посты выключены. /resume — включить обратно.")

    @r.message(Command("resume"))
    async def on_resume(m: Message):
        ensure_user(m.from_user.id)
        with db.conn() as c:
            dbm.set_user_fields(c, m.from_user.id, paused=0)
        await m.answer("Посты включены ✅")

    # -------------------------------------------- buttons under the posts

    @r.callback_query(F.data.startswith("wl:"))
    async def on_wishlist_button(cb: CallbackQuery):
        ensure_user(cb.from_user.id)
        film_id = int(cb.data.split(":")[1])
        with db.conn() as c:
            dbm.set_action(c, cb.from_user.id, film_id, "wishlist", now_iso())
        await cb.answer("Добавил в вишлист 🔖")
        await _replace_action_row(cb, "✅ В вишлисте")

    @r.callback_query(F.data.startswith("ni:"))
    async def on_not_interested_button(cb: CallbackQuery):
        ensure_user(cb.from_user.id)
        film_id = int(cb.data.split(":")[1])
        with db.conn() as c:
            dbm.set_action(c, cb.from_user.id, film_id, "not_interested", now_iso())
        await cb.answer("Больше не покажу этот фильм")
        try:
            await cb.message.delete()
        except TelegramBadRequest:  # сообщение старше 48 часов
            await _replace_action_row(cb, "🚫 Скрыто")

    @r.callback_query(F.data == "noop")
    async def on_noop(cb: CallbackQuery):
        await cb.answer()

    async def _replace_action_row(cb: CallbackQuery, label: str):
        markup = cb.message.reply_markup
        if not markup:
            return
        rows = [row for row in markup.inline_keyboard[:1]]
        rows.append([InlineKeyboardButton(text=label, callback_data="noop")])
        try:
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramBadRequest:
            pass

    # ------------------------------------------------- search by title

    @r.callback_query(F.data.startswith("add:"))
    async def on_add_from_search(cb: CallbackQuery):
        _, kind, tmdb_id = cb.data.split(":")
        ensure_user(cb.from_user.id)
        try:
            film_id = await asyncio.to_thread(app.matcher.resolve_tmdb_id, int(tmdb_id))
        except (TMDbError, AssertionError):
            await cb.answer("TMDb сейчас недоступен, попробуйте позже", show_alert=True)
            return
        with db.conn() as c:
            dbm.set_action(c, cb.from_user.id, film_id, "wishlist" if kind == "wl" else "like", now_iso())
        await cb.answer("Добавил в вишлист 🔖" if kind == "wl" else "Запомнил: вам нравится ❤️")

    @r.message(F.text & ~F.text.startswith("/"))
    async def on_text(m: Message):
        ensure_user(m.from_user.id)
        if not app.tmdb:
            await m.answer("Поиск фильмов пока не настроен (нет ключа TMDb).")
            return
        query = m.text.strip()[:100]
        try:
            results = await asyncio.to_thread(app.tmdb.search_movie, query, None, "ru-RU")
        except TMDbError:
            await m.answer("Поиск сейчас недоступен, попробуйте чуть позже.")
            return
        if not results:
            await m.answer("Ничего не нашёл. Попробуйте написать название иначе.")
            return
        rows = []
        for x in results[:5]:
            year = (x.get("release_date") or "")[:4]
            label = f"{x.get('title') or x.get('original_title')}" + (f" ({year})" if year else "")
            rows.append([
                InlineKeyboardButton(text=f"🔖 {label}"[:40], callback_data=f"add:wl:{x['id']}"),
                InlineKeyboardButton(text="❤️", callback_data=f"add:like:{x['id']}"),
            ])
        await m.answer("Нашёл. 🔖 — в вишлист, ❤️ — мне нравится:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # --------------------------------------------- Letterboxd import

    @r.message(F.document)
    async def on_document(m: Message, bot: Bot):
        user = ensure_user(m.from_user.id)
        doc = m.document
        name = doc.file_name or ""
        if not user["city_id"]:
            await m.answer("Сначала выберите город:", reply_markup=city_keyboard())
            return
        if not name.lower().endswith((".zip", ".csv")):
            await m.answer("Нужен экспорт Letterboxd: zip-архив или файл watchlist.csv / ratings.csv.")
            return
        if (doc.file_size or 0) > MAX_UPLOAD:
            await m.answer("Файл слишком большой (лимит 20 МБ).")
            return
        if not app.tmdb:
            await m.answer("Импорт пока не настроен (нет ключа TMDb).")
            return
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        try:
            entries = parse_export(name, buf.getvalue())
        except zipfile.BadZipFile:
            await m.answer("Не получилось открыть архив. Пришлите файл экспорта Letterboxd целиком.")
            return
        if not entries:
            await m.answer("В файле не нашлось фильмов (ищу watchlist.csv, ratings.csv, watched.csv, likes/films.csv).")
            return
        status = await m.answer(f"Нашёл записей: {len(entries)}. Импортирую самое важное, это займёт около минуты…")
        summary = await asyncio.to_thread(import_entries, db, app.matcher, m.from_user.id, entries)
        parts = []
        if summary.wishlist:
            parts.append(f"в вишлист: {summary.wishlist}")
        if summary.rated:
            parts.append(f"оценок: {summary.rated}")
        if summary.liked:
            parts.append(f"лайков: {summary.liked}")
        if summary.watched:
            parts.append(f"просмотренных: {summary.watched}")
        text = "Готово ✅ Добавлено: " + (", ".join(parts) or "ничего")
        if summary.unresolved:
            text += f"\nНе удалось распознать: {summary.unresolved}."
        if summary.skipped_by_limit:
            text += f"\nЕщё {summary.skipped_by_limit} записей пропущено, чтобы не перегружать."
        if summary.errors:
            text += "\n⚠️ Были ошибки TMDb, можно повторить позже."
        await status.edit_text(text + "\n\nПопробуйте /feed.")

    # -------------------------------------------------------------- admin

    @r.message(Command("stats"))
    async def on_stats(m: Message):
        if not app.is_admin(m.from_user.id):
            return
        with db.conn() as c:
            def count(sql: str) -> int:
                return c.execute(sql).fetchone()[0]

            lines = [
                f"Пользователей: {count('SELECT COUNT(*) FROM users')}, "
                f"с городом: {count('SELECT COUNT(*) FROM users WHERE city_id IS NOT NULL')}",
                f"Активных сеансов: {count('SELECT COUNT(*) FROM sessions WHERE active=1')}",
                f"Фильмов: {count('SELECT COUNT(*) FROM films')}, "
                f"без уверенной привязки к TMDb: {count(UNMATCHED_SQL)}",
                f"Отправлено постов: {count('SELECT COUNT(*) FROM feed_events')}",
            ]
            runs = c.execute(
                """SELECT source, finished_at, ok, sessions_count FROM collector_runs
                   WHERE id IN (SELECT MAX(id) FROM collector_runs GROUP BY source)""").fetchall()
        lines += [f"{x['source']}: {'ok' if x['ok'] else 'FAIL'}, сеансов {x['sessions_count']}, {x['finished_at']}"
                  for x in runs]
        await m.answer("\n".join(lines))

    @r.message(Command("unmatched"))
    async def on_unmatched(m: Message):
        if not app.is_admin(m.from_user.id):
            return
        with db.conn() as c:
            rows = c.execute("SELECT id, title, confidence FROM films WHERE confidence != 'tmdb' ORDER BY id DESC LIMIT 30").fetchall()
        if not rows:
            await m.answer("Все фильмы привязаны к TMDb ✅")
            return
        await m.answer("Фильмы без уверенной привязки (local = не найден, low = сомнительно):\n"
                       + "\n".join(f"{x['id']} · {esc(x['title'])} · {x['confidence']}" for x in rows)
                       + "\n\nИсправить: /link &lt;id фильма&gt; &lt;TMDb id&gt;")

    @r.message(Command("link"))
    async def on_link(m: Message, command: CommandObject):
        if not app.is_admin(m.from_user.id):
            return
        parts = (command.args or "").split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await m.answer("Формат: /link <id фильма> <TMDb id>")
            return
        try:
            final = await asyncio.to_thread(app.matcher.link, int(parts[0]), int(parts[1]))
        except Exception as e:  # noqa: BLE001
            await m.answer(f"Не получилось: {esc(str(e))}")
            return
        await m.answer(f"Готово, фильм привязан (id {final}).")

    @r.message(Command("collect"))
    async def on_collect(m: Message):
        if not app.is_admin(m.from_user.id):
            return
        await m.answer("Запускаю сбор…")
        reports = await asyncio.to_thread(app.collector.run_once)
        await m.answer("\n".join(
            f"{x.source}: сеансов {x.sessions} (новых {x.new_sessions}), без привязки {x.unresolved}"
            + (f"\n⚠️ {esc('; '.join(x.errors[:2]))}" if x.errors else "")
            for x in reports) or "Нет включённых адаптеров")

    return r


async def _collector_loop(app: App, bot: Bot, last_alert: dict[str, tuple[str, float]]) -> None:
    interval = app.settings.collect_interval_min * 60
    while True:
        try:
            reports = await asyncio.to_thread(app.collector.run_once)
            admin = app.settings.admin_chat_id
            for rep in reports:
                if not (rep.alert and admin):
                    continue
                prev = last_alert.get(rep.source)
                if prev and prev[0] == rep.alert and time.time() - prev[1] < ALERT_REPEAT_SECONDS:
                    continue  # не спамим одним и тем же
                last_alert[rep.source] = (rep.alert, time.time())
                await bot.send_message(admin, esc(rep.alert))
        except Exception:  # noqa: BLE001
            log.exception("collector loop failed")
        await asyncio.sleep(interval)


async def _dispatch_loop(feed: FeedDispatcher, interval_min: int) -> None:
    await asyncio.sleep(60)  # дадим первому сбору отработать
    while True:
        try:
            sent = await feed.tick()
            if sent:
                log.info("dispatched %s posts", sent)
        except Exception:  # noqa: BLE001
            log.exception("dispatch loop failed")
        await asyncio.sleep(interval_min * 60)


async def run_bot(app: App) -> None:
    if not app.settings.bot_token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    bot = Bot(app.settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    feed = FeedDispatcher(app.db, app.settings, TelegramSender(bot))
    dp = Dispatcher()
    dp.include_router(build_router(app, feed))
    tasks = [
        asyncio.create_task(_collector_loop(app, bot, {})),
        asyncio.create_task(_dispatch_loop(feed, app.settings.dispatch_interval_min)),
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()
