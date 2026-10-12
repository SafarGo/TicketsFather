"""Превращает кандидата в готовый пост: картинка + подпись (HTML) + кнопки."""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime

from . import db as dbm
from .events import Candidate
from .timeutil import fmt_when, from_iso_utc

CAPTION_LIMIT = 1024
TAGS = {
    "wishlist": "🔖 ВЫ ХОТЕЛИ ПОСМОТРЕТЬ",
    "deal": "💸 ВЫГОДНО!",
    "new": "🆕 НОВИНКА!",
    "promo": "🎁 АКЦИЯ!",
}


@dataclass
class Button:
    text: str
    url: str | None = None
    callback_data: str | None = None


@dataclass
class RenderedPost:
    text: str
    photo_url: str | None = None
    buttons: list[list[Button]] = field(default_factory=list)


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _list(raw) -> list[str]:
    try:
        return list(json.loads(raw or "[]"))
    except (TypeError, ValueError):
        return []


def _trim(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def render_candidate(database: dbm.Database, cand: Candidate, tz, now: datetime | None = None) -> RenderedPost:
    with database.conn() as c:
        if cand.kind == "promo":
            p = c.execute("SELECT * FROM promos WHERE id=?", (cand.promo_id,)).fetchone()
            text = f"{TAGS['promo']}\n\n<b>{esc(p['title'])}</b> · {esc(p['chain'])}"
            if p["description"]:
                text += "\n" + esc(_trim(p["description"], 600))
            return RenderedPost(text=text, photo_url=p["image_url"], buttons=[[Button("Подробнее", url=p["url"])]])

        film = dbm.get_film(c, cand.film_id)
        s = c.execute(
            """SELECT s.*, ci.name AS cinema_name FROM sessions s JOIN cinemas ci ON ci.id=s.cinema_id WHERE s.id=?""",
            (cand.session_id,),
        ).fetchone()

    title = f"<b>{esc(film['title'])}</b>" + (f" ({film['year']})" if film["year"] else "")
    meta_bits = []
    genres = _list(film["genres"])[:3]
    if genres:
        meta_bits.append(", ".join(genres))
    directors = _list(film["directors"])[:1]
    if directors:
        meta_bits.append("реж. " + ", ".join(directors))
    if film["runtime"]:
        meta_bits.append(f"{film['runtime']} мин")
    where = " · ".join(x for x in [s["cinema_name"], s["hall"], s["fmt"], s["language_label"]] if x)
    when = fmt_when(from_iso_utc(s["starts_at"]), tz, now)
    price_line = ""
    if s["price"]:
        price_line = f"💰 {s['price']} ֏"
        if cand.deal_pct:
            price_line += f" · на {cand.deal_pct}% дешевле обычного (≈{cand.deal_ref} ֏)"
    elif cand.deal_pct:
        price_line = f"💰 на {cand.deal_pct}% дешевле обычного"
    if s["badge"] == "Специальная цена":
        price_line = (price_line + "\n" if price_line else "") + "🏷 день специальных цен"

    head = [TAGS[cand.kind], "", title]
    if meta_bits:
        head.append(esc(" · ".join(meta_bits)))
    tail = ["", f"📍 {esc(where)}", f"🕒 {esc(when)}"]
    if price_line:
        tail.append(price_line)
    fixed = "\n".join(head + tail)
    overview = _trim(film["overview"], min(300, CAPTION_LIMIT - len(fixed) - 4))
    text = "\n".join(head + ([""] + [esc(overview)] if overview else []) + tail)

    buy = "🎟 Купить билет" + (f" · {s['price']} ֏" if s["price"] else "")
    buttons = [
        [Button(buy, url=s["url"])],
        [Button("🔖 В вишлист", callback_data=f"wl:{film['id']}"),
         Button("🚫 Не интересно", callback_data=f"ni:{film['id']}")],
    ]
    return RenderedPost(text=text, photo_url=film["poster_url"] or film["site_poster_url"], buttons=buttons)
