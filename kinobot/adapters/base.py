from __future__ import annotations

import time
from datetime import datetime

import requests

from ..models import FetchResult

USER_AGENT = "Mozilla/5.0 (compatible; KinobotMVP/0.1; schedule aggregator for personal use)"


def make_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"})
    return s


def polite_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


class Adapter:
    """Источник сеансов. Один адаптер = один сайт (сеть кинотеатров)."""

    name: str = "base"
    enabled: bool = True

    def fetch(self, now: datetime) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError
