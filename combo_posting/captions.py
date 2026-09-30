"""Подписи: шаблоны CTA + UTM/deeplink на продукт (instagram_pipeline.md §3.2–3.3).

В Instagram ссылки в подписи не кликабельны -> CTA «ссылка в профиле».
В Threads ссылки кликабельны -> вставляем прямую ссылку с UTM.
"""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

IG_MAX_CHARS = 2200
IG_MAX_HASHTAGS = 30
IG_MAX_MENTIONS = 20
THREADS_MAX_CHARS = 500

PRODUCTS = {
    "dailyq": {
        "url": os.environ.get("DAILYQ_URL", "https://cr-web-production.up.railway.app/"),
        "hashtags": ["#dailyq", "#productivity"],
        "dm_keyword": "DAILY",
    },
    "botify": {
        # TG deep-link = трекинг: t.me/<bot>?start=ig_<reel_id>
        "url": "https://t.me/" + os.environ.get("BOTIFY_BOT", "botify_bot"),
        "hashtags": ["#botify", "#telegrambot"],
        "dm_keyword": "BOT",
        "deeplink": True,
    },
}

HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
MENTION_RE = re.compile(r"(?<![\w.])@[\w.]+")

IG_CTA = "👉 Попробовать: ссылка в профиле{handle}"
DM_CTA = "💬 Напиши «{keyword}» — пришлём ссылку в директ"
LINK_CTA = "👉 Попробовать: {url}"


class CaptionError(ValueError):
    pass


def add_utm(url: str, source: str, medium: str, campaign: str, **extra: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"utm_source": source, "utm_medium": medium, "utm_campaign": campaign}, **extra)
    return urlunsplit(parts._replace(query=urlencode(query)))


def product_link(product: str, platform: str, campaign: str, medium: str = "social") -> str:
    p = _product(product)
    source = {"ig": "instagram", "threads": "threads"}.get(platform, platform)
    if p.get("deeplink"):
        return f"{p['url']}?start={platform}_{campaign}"
    return add_utm(p["url"], source, medium, campaign)


def _product(product: str) -> dict:
    try:
        return PRODUCTS[product]
    except KeyError:
        raise CaptionError(f"неизвестный продукт {product!r} (есть: {', '.join(PRODUCTS)})")


def _merge_hashtags(text: str, tags: list[str]) -> list[str]:
    present = {t.lower() for t in HASHTAG_RE.findall(text)}
    return [t for t in tags if t.lower() not in present]


def render(caption: str, platform: str, product: str | None = None,
           campaign: str = "post", handle: str | None = None,
           dm_keyword: bool = False) -> str:
    """Готовая подпись под платформу: текст + CTA + хэштеги продукта."""
    parts = [caption.strip()]
    tags: list[str] = []
    if product:
        p = _product(product)
        if platform == "ig":
            cta = [IG_CTA.format(handle=f" (@{handle.lstrip('@')})" if handle else "")]
            if dm_keyword:
                cta.append(DM_CTA.format(keyword=p["dm_keyword"]))
        else:
            cta = [LINK_CTA.format(url=product_link(product, platform, campaign))]
        parts.append("\n".join(cta))
        tags = _merge_hashtags(caption, p["hashtags"])
    if tags:
        parts.append(" ".join(tags))
    text = "\n\n".join(x for x in parts if x)
    validate(text, platform)
    return text


def validate(text: str, platform: str = "ig") -> None:
    if platform == "ig":
        if len(text) > IG_MAX_CHARS:
            raise CaptionError(f"подпись {len(text)} символов > {IG_MAX_CHARS}")
        if len(HASHTAG_RE.findall(text)) > IG_MAX_HASHTAGS:
            raise CaptionError(f"хэштегов больше {IG_MAX_HASHTAGS}")
        if len(MENTION_RE.findall(text)) > IG_MAX_MENTIONS:
            raise CaptionError(f"упоминаний больше {IG_MAX_MENTIONS}")
    elif platform == "threads" and len(text) > THREADS_MAX_CHARS:
        raise CaptionError(f"подпись {len(text)} символов > {THREADS_MAX_CHARS} (Threads)")
