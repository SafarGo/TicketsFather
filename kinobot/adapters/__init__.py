from __future__ import annotations

from .base import Adapter
from .cinemastar import CinemaStarAdapter
from .kinomoscow import KinoMoscowAdapter
from .kinopark import KinoParkAdapter


def default_adapters(tz_name: str) -> list[Adapter]:
    return [KinoMoscowAdapter(tz_name), CinemaStarAdapter(tz_name), KinoParkAdapter()]


__all__ = ["Adapter", "default_adapters", "KinoMoscowAdapter", "CinemaStarAdapter", "KinoParkAdapter"]
