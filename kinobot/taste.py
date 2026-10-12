"""Простой профиль вкуса: веса жанров и режиссёров по действиям пользователя."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field

ACTION_WEIGHT = {"wishlist": 1.0, "like": 1.5, "watched": 0.3, "not_interested": -1.5}


@dataclass
class Taste:
    genres: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    directors: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    signals: int = 0


def _load(raw) -> list[str]:
    if isinstance(raw, list):
        return raw
    try:
        return list(json.loads(raw or "[]"))
    except (TypeError, ValueError):
        return []


def action_weight(action: str, value: float | None) -> float:
    if action == "rating":
        # рейтинги Letterboxd 0.5–5: 3 нейтрально, 5 сильный плюс, 1 сильный минус
        return ((value if value is not None else 3.0) - 3.0) / 2.0 * 1.5
    return ACTION_WEIGHT.get(action, 0.0)


def build_taste(actions) -> Taste:
    t = Taste()
    for a in actions:
        w = action_weight(a["action"], a["value"])
        if w == 0:
            continue
        t.signals += 1
        for g in _load(a["genres"]):
            t.genres[g] += w
        for d in _load(a["directors"]):
            t.directors[d] += w * 0.7
    return t


def taste_score(t: Taste, genres_raw, directors_raw) -> float:
    """Оценка фильма для пользователя в диапазоне [-1, 1]; 0 = нет данных."""
    genres, directors = _load(genres_raw), _load(directors_raw)
    if not t.signals or not (genres or directors):
        return 0.0
    g = sum(math.tanh(t.genres.get(x, 0.0) / 3.0) for x in genres) / len(genres) if genres else 0.0
    d = max((math.tanh(t.directors.get(x, 0.0) / 2.0) for x in directors), default=0.0)
    return max(-1.0, min(1.0, 0.75 * g + 0.25 * d))
