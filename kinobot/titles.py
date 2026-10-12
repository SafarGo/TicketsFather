"""Нормализация названий фильмов с сайтов кинотеатров.

Cinema Star пишет «Диггер (RUS)», КиноМосква «Гордость и предубеждение ENG»,
Kinopark использует английские названия. Здесь мы приводим всё к общему виду.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_LANG_WORDS = r"(?:rus|arm|eng|ru|hy|en|am)"
_PAREN_TAG = re.compile(rf"\(\s*(?:{_LANG_WORDS}|2d|3d|imax|4dx|screenx)\s*\)", re.I)
_TRAIL_LANG = re.compile(r"\s+(?:rus|arm|eng)\s*$", re.I)
_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

_LANG_MAP = {"rus": "ru", "ru": "ru", "arm": "hy", "am": "hy", "hy": "hy", "eng": "en", "en": "en"}


def detect_language(raw: str) -> str | None:
    m = re.search(rf"\(\s*({_LANG_WORDS})\s*\)", raw, re.I)
    if m:
        return _LANG_MAP[m.group(1).lower()]
    m = re.search(r"\s(rus|arm|eng)\s*$", raw, re.I)
    if m:
        return _LANG_MAP[m.group(1).lower()]
    return None


def clean_title(raw: str) -> str:
    """Название для показа: без пометок языка и формата."""
    s = unicodedata.normalize("NFKC", raw)
    s = _PAREN_TAG.sub(" ", s)
    s = _TRAIL_LANG.sub("", s.strip())
    return _SPACES.sub(" ", s).strip()


def normalize_title(raw: str) -> str:
    """Ключ для сравнения: нижний регистр, без знаков препинания и пометок."""
    s = clean_title(raw).lower().replace("ё", "е")
    s = _NON_WORD.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(ratio, jaccard)
