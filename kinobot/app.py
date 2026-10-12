"""Сборка всех компонентов в одном месте (используют и бот, и CLI)."""
from __future__ import annotations

from . import db as dbm
from .adapters import default_adapters
from .collector import Collector
from .config import Settings
from .matcher import FilmMatcher
from .tmdb import TMDb


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = dbm.Database(settings.db_path)
        self.tmdb = TMDb(settings.tmdb_api_key) if settings.tmdb_api_key else None
        self.matcher = FilmMatcher(self.db, self.tmdb)
        self.collector = Collector(self.db, default_adapters(settings.tz), self.matcher, settings.tz)

    def is_admin(self, user_id: int) -> bool:
        return self.settings.admin_chat_id is not None and user_id == self.settings.admin_chat_id
