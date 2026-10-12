"""Клиент TMDb API v3. Поддерживает и API key, и Read Access Token (v4, начинается с 'eyJ')."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"


class TMDbError(Exception):
    pass


def poster_url(path: str | None, size: str = "w500") -> str | None:
    return f"{IMG}/{size}{path}" if path else None


class TMDb:
    def __init__(self, api_key: str, http=None, delay: float = 0.05):
        self.api_key = api_key
        self.http = http or requests.Session()
        self.delay = delay

    def _get(self, path: str, **params):
        headers = {"Accept": "application/json"}
        if self.api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        last = None
        for attempt in range(3):
            try:
                r = self.http.get(f"{API}{path}", params=params, headers=headers, timeout=20)
            except requests.RequestException as e:
                last = e
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = TMDbError(f"HTTP {r.status_code}")
                time.sleep(float(r.headers.get("Retry-After", 1 + attempt)))
                continue
            if r.status_code != 200:
                raise TMDbError(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(self.delay)
            return r.json()
        raise TMDbError(f"TMDb недоступен: {last}")

    def search_movie(self, query: str, year: int | None = None, language: str = "ru-RU") -> list[dict]:
        params = {"query": query, "language": language, "include_adult": "false"}
        if year:
            params["year"] = year
        return self._get("/search/movie", **params).get("results", [])

    def details(self, tmdb_id: int, language: str = "ru-RU") -> dict:
        return self._get(f"/movie/{tmdb_id}", language=language, append_to_response="credits")


def film_data(details: dict, fallback_overview: str | None = None) -> dict:
    """Приводит ответ TMDb к полям таблицы films."""
    release = details.get("release_date") or None
    directors = [
        p["name"] for p in (details.get("credits") or {}).get("crew", []) if p.get("job") == "Director"
    ][:3]
    return {
        "tmdb_id": details["id"],
        "title": details.get("title") or details.get("original_title") or "",
        "original_title": details.get("original_title"),
        "year": int(release[:4]) if release else None,
        "overview": details.get("overview") or fallback_overview or None,
        "poster_url": poster_url(details.get("poster_path")),
        "genres": [g["name"] for g in details.get("genres", [])],
        "directors": directors,
        "runtime": details.get("runtime") or None,
        "release_date": release,
        "popularity": details.get("popularity"),
    }
