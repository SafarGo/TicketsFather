"""Отправка постов пользователям с лимитами и тихими часами."""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Protocol

from . import db as dbm
from .config import Settings
from .events import build_candidates
from .render import RenderedPost, render_candidate
from .timeutil import UTC, to_iso_utc, tz_of

log = logging.getLogger(__name__)


class SendBlocked(Exception):
    """Пользователь заблокировал бота или удалил чат."""


class Sender(Protocol):
    async def send(self, user_id: int, post: RenderedPost) -> None: ...


def in_quiet_hours(now: datetime, tz, start_hour: int, end_hour: int) -> bool:
    h = now.astimezone(tz).hour
    return h >= start_hour or h < end_hour if start_hour > end_hour else start_hour <= h < end_hour


class FeedDispatcher:
    def __init__(self, database: dbm.Database, settings: Settings, sender: Sender):
        self.db = database
        self.settings = settings
        self.sender = sender
        self.tz = tz_of(settings.tz)

    def _local_midnight_iso(self, now: datetime) -> str:
        local = now.astimezone(self.tz)
        return to_iso_utc(datetime.combine(local.date(), time(0, 0), tzinfo=self.tz))

    async def tick(self, now: datetime | None = None) -> int:
        """Один проход по всем пользователям. -> сколько постов отправлено."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if in_quiet_hours(now, self.tz, self.settings.quiet_start_hour, self.settings.quiet_end_hour):
            return 0
        with self.db.conn() as c:
            users = dbm.list_dispatchable_users(c)
        sent_total = 0
        for u in users:
            sent_total += await self.send_for_user(u["tg_id"], now, respect_limits=True)
        return sent_total

    async def send_for_user(self, user_id: int, now: datetime, respect_limits: bool, limit: int | None = None) -> int:
        with self.db.conn() as c:
            user = dbm.get_user(c, user_id)
            if not user:
                return 0
            today = dbm.count_events_since(c, user_id, self._local_midnight_iso(now))
        budget = limit if limit is not None else self.settings.max_posts_per_tick
        if respect_limits:
            budget = min(budget, user["max_posts_per_day"] - today)
        if budget <= 0:
            return 0

        sent = 0
        for cand in build_candidates(self.db, user_id, now, self.settings):
            if sent >= budget:
                break
            with self.db.conn() as c:
                fresh = dbm.record_event(c, user_id, cand.kind, cand.key, cand.film_id, cand.session_id,
                                         cand.promo_id, to_iso_utc(now))
            if not fresh:
                continue
            post = render_candidate(self.db, cand, self.tz, now)
            try:
                await self.sender.send(user_id, post)
                sent += 1
            except SendBlocked:
                with self.db.conn() as c:
                    dbm.set_user_fields(c, user_id, paused=1)
                log.info("user %s blocked the bot; paused", user_id)
                break
            except Exception:
                log.exception("send failed for user %s", user_id)
                with self.db.conn() as c:  # дадим шанс повторить позже
                    dbm.delete_event(c, user_id, cand.key)
                break
        return sent
