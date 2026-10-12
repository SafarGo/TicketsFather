from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv необязателен
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    tmdb_api_key: str = ""
    db_path: str = "kinobot.db"
    admin_chat_id: int | None = None
    tz: str = "Asia/Yerevan"

    collect_interval_min: int = 30
    dispatch_interval_min: int = 10
    quiet_start_hour: int = 23  # не присылаем посты с 23:00 до 09:00 по местному времени
    quiet_end_hour: int = 9
    max_posts_per_tick: int = 2
    film_cooldown_days: int = 5

    horizon_days: int = 7
    min_lead_minutes: int = 45  # не предлагаем сеансы, которые вот-вот начнутся
    deal_ratio: float = 0.7  # «выгодно» = цена не выше 70% от обычной для этого фильма в этом кинотеатре
    deal_min_saving: int = 500  # и экономия не меньше 500 драм
    deal_min_sessions: int = 3  # сколько сеансов нужно, чтобы оценить «обычную» цену

    @classmethod
    def from_env(cls) -> "Settings":
        admin = os.getenv("ADMIN_CHAT_ID", "").strip()
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            tmdb_api_key=os.getenv("TMDB_API_KEY", ""),
            db_path=os.getenv("DB_PATH", "kinobot.db"),
            admin_chat_id=int(admin) if admin else None,
            tz=os.getenv("TZ_NAME", "Asia/Yerevan"),
            collect_interval_min=int(os.getenv("COLLECT_INTERVAL_MIN", "30")),
            dispatch_interval_min=int(os.getenv("DISPATCH_INTERVAL_MIN", "10")),
        )
