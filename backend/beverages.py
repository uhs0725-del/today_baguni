"""Beverage prices via the NAVER 쇼핑 검색 API (graceful degradation).

KAMIS has NO beverage prices (우유 is handled elsewhere), so drinks are
priced from the NAVER Shopping Search API — the SAME NAVER app credentials
already used for recipe blog search (env NAVER_CLIENT_ID /
NAVER_CLIENT_SECRET). For each curated beverage we take the first
sim-ranked listing and expose its lowest listed price.

IMPORTANT product note: `lprice` is an online-listing lowest price and is
OFTEN a multipack price (e.g. "코카콜라 1.5L 12개" = 11500). There is NO
reliable per-unit normalization and NO historical baseline, so beverages
carry NO "평소 대비 ▼NN%" trend — the UI must label them clearly as
"네이버 쇼핑 최저가" so they're never confused with the KAMIS ▼% signal.
The raw listing title is exposed so a multipack is visible to the user.

A module-level TTL cache (same spirit as recipes.py) keeps the NAVER quota
safe — the upstream is hit at most once per query per ~12h. Missing API
keys OR any per-item failure degrades that item to status "fallback"
(price None, more_url still set). The public entry point
`gather_beverages()` NEVER raises.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

# Curated 1인가구 drinks KAMIS can't price. (display_name, naver_query).
# Specific queries (brand + size) keep the first sim-ranked result stable.
# Editable in code.
BEVERAGES: list[tuple[str, str]] = [
    ("생수", "삼다수 2L"),
    ("두유", "매일두유 99.9 190ml"),
    ("콜라", "코카콜라 1.5L"),
    ("사이다", "칠성사이다 1.5L"),
    ("탄산수", "트레비 탄산수 500ml"),
    ("오렌지주스", "델몬트 오렌지주스 1.5L"),
    ("포도주스", "웰치스 포도주스 1.5L"),
    ("이온음료", "포카리스웨트 1.5L"),
    ("캔커피", "맥심 티오피 200ml"),
    ("인스턴트커피", "맥심 카누 아메리카노"),
    ("보리차", "하늘보리 500ml"),
    ("녹차", "녹차 티백 100T"),
    ("에너지드링크", "핫식스 250ml"),
]

# External call is best-effort; keep the timeout short so a slow/blocked
# upstream can't stall the API response.
_HTTP_TIMEOUT = 6.0

# How many listings to pull per query (we keep the FIRST = sim-ranked).
_DISPLAY = 5

# ---------------------------------------------------------------------------
# Server-side cache: module-level dict keyed by the naver query. TTL ~12h so
# the NAVER quota is hit at most once per query per half-day. A soft cap
# bounds memory; on overflow the oldest entries are dropped. Same spirit as
# recipes.py.
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 12 * 60 * 60  # ~12h
_CACHE_MAX_ENTRIES = 256

# key: naver_query -> (stored_at_epoch, item_dict)
_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(query: str) -> Optional[dict]:
    entry = _cache.get(query)
    if entry is None:
        return None
    stored_at, payload = entry
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _cache.pop(query, None)
        return None
    return payload


def _cache_set(query: str, payload: dict) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        # Drop the oldest ~1/8 so we don't evict one-at-a-time once full.
        for key in sorted(_cache, key=lambda k: _cache[k][0])[
            : max(1, _CACHE_MAX_ENTRIES // 8)
        ]:
            _cache.pop(key, None)
    _cache[query] = (time.time(), payload)


def _strip_tags(text: str) -> str:
    """Remove HTML tags and unescape entities (NAVER returns <b>…</b>)."""
    no_tags = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(no_tags).strip()


def _more_url(display_name: str) -> str:
    """Always-set deep-link into NAVER 쇼핑 search for this beverage, so the
    UI can always link out even when the per-item fetch falls back."""
    return (
        "https://search.shopping.naver.com/search/all?query="
        + quote(display_name)
    )


def _fallback_item(display_name: str) -> dict:
    """Graceful per-item shape: no price, but a working deep-link."""
    return {
        "name": display_name,
        "price": None,
        "listing": "",
        "url": "",
        "mall": "",
        "status": "fallback",
        "more_url": _more_url(display_name),
    }


def _fetch_one(display_name: str, query: str) -> dict:
    """Representative listing for one beverage via NAVER 쇼핑. The
    representative item is the FIRST returned (sim-ranked). ANY problem
    (missing keys, network, HTTP, parse, empty) ⇒ fallback item. Cached
    per query (~12h). Never raises.
    """
    cached = _cache_get(query)
    if cached is not None:
        return cached

    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        # Absent locally → graceful fallback (NOT cached: keys may appear
        # in a later process / on prod, and we don't want to pin fallback).
        return _fallback_item(display_name)

    item = _fallback_item(display_name)
    try:
        resp = httpx.get(
            "https://openapi.naver.com/v1/search/shop.json",
            params={
                "query": query,
                "display": _DISPLAY,
                "sort": "sim",
            },
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        listings = data.get("items") or []
        if listings:
            top = listings[0]
            title = _strip_tags(top.get("title", ""))
            link = top.get("link") or ""
            mall = top.get("mallName") or ""
            price: Optional[int] = None
            try:
                lprice = top.get("lprice")
                if lprice is not None and str(lprice) != "":
                    price = int(lprice)
            except (TypeError, ValueError):
                price = None
            if title:
                item = {
                    "name": display_name,
                    "price": price,
                    "listing": title,
                    "url": link,
                    "mall": mall,
                    "status": "ok",
                    "more_url": _more_url(display_name),
                }
    except Exception:
        # Tolerant by design — keep the fallback shape for THIS item.
        item = _fallback_item(display_name)

    _cache_set(query, item)
    return item


def gather_beverages() -> dict:
    """Build the /api/beverages payload for the curated beverage list.

    Each beverage is fetched behind its own try/except and a shared
    module-level TTL cache so the NAVER quota is hit at most once per query
    per ~12h. Missing keys or any per-item failure degrades that item to
    status "fallback" (price None, more_url still set). NEVER raises.
    """
    items: list[dict] = []
    for display_name, query in BEVERAGES:
        try:
            items.append(_fetch_one(display_name, query))
        except Exception:
            # Defensive: _fetch_one is self-contained, but the route must
            # never raise — degrade this one item.
            items.append(_fallback_item(display_name))
    return {"items": items, "source": "naver-shopping"}
