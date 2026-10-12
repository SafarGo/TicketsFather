"""Kinopark (kinopark.am): ЗАГОТОВКА.

Сайт на Next.js: сеансы в исходной странице отсутствуют и подгружаются скриптом
после выбора даты. Чтобы написать адаптер, нужен реальный запрос, который
возвращает сеансы и цены (вкладка Network в браузере: URL и пример ответа).
Пока адаптер отключён и коллектором пропускается.
"""
from __future__ import annotations

from datetime import datetime

from ..models import FetchResult
from .base import Adapter


class KinoParkAdapter(Adapter):
    name = "kinopark"
    enabled = False

    def fetch(self, now: datetime) -> FetchResult:
        raise NotImplementedError("Kinopark: нужен endpoint с сеансами (см. docstring)")
