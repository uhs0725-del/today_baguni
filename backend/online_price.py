"""Per-INGREDIENT online lowest price via the NAVER 쇼핑 검색 API.

This is the online lowest-listing price for a SINGLE recommended ingredient
(e.g. 양파, 삼겹살), fetched from the NAVER Shopping Search API using the
SAME NAVER app credentials already used for recipe blog / beverage search
(env NAVER_CLIENT_ID / NAVER_CLIENT_SECRET). For each ingredient we take the
first sim-ranked listing and expose its lowest listed price.

IMPORTANT product note: this is DELIBERATELY separate from — and must never
be confused with — the KAMIS "평소 대비 ▼NN%" signal shown in the card head.
`lprice` is an online-listing lowest price and is OFTEN a multipack / bulk
listing (e.g. "양파 10kg" = 12900) whose unit does NOT match the KAMIS 소량
소매 reference. There is therefore NO per-unit normalization, NO historical
baseline, and NO derived "% cheaper vs 기준가" — doing so would be dishonest.
Instead the raw listing title is exposed so the unit/multipack is visible to
the user (same principle beverages.py documents).

Also: NAVER shop.json has NO delivery-speed field, so "당일배송" is NOT
guaranteed and must never be implied.

A module-level TTL cache (same spirit as beverages.py / recipes.py) keeps
the NAVER quota safe — the upstream is hit at most once per query per ~12h.
This matters because /api/recommendations returns many items; the online
price is fetched LAZILY (only when the user expands a card), one ingredient
at a time. Missing API keys OR any per-item failure degrades that item to
status "fallback" (price None, more_url still set). The public entry point
`fetch_online_price()` NEVER raises.

Phase 2 (쿠팡 파트너스) can be added later as an additional price source
alongside this one — keep this module self-contained so that's a sibling
add, not a rewrite.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

# External call is best-effort; keep the timeout short so a slow/blocked
# upstream can't stall the API response.
_HTTP_TIMEOUT = 6.0

# How many listings to pull per query (ONE API call regardless). We pull a
# few so the light relevance filter has candidates; we still PREFER the
# highest sim-ranked one that isn't obvious junk.
_DISPLAY = 10

# Listings that are clearly NOT a 1인가구-relevant retail item — bulk /
# foodservice / gift / seed-or-grow / wholesale. HIGH-PRECISION Korean
# tokens only (substring on the de-tagged title); the goal is to drop the
# worst offenders, NOT perfect matching (impossible — NAVER 쇼핑 has no
# unit/identity normalization; the caption still warns about 묶음/대용량).
_JUNK_TOKENS = (
    "선물세트", "선물 세트", "업소용", "영업용", "외식", "급식",
    "도매", "도매가", "벌크", "박스판매", "한박스", "한 박스",
    "모종", "씨앗", "종자", "모판", "재배", "사료", "비료",
    "중도매", "경매", "단체", "대량",
)

# Absurd-quantity heuristic: a number ≥ 5 right before "kg" (e.g. "10kg",
# "5 kg", "20KG"). 1인가구 consumer packs are almost always < 5kg, so this
# strips the "양파 10kg" / "통삼겹살 4kg 박스" style listings when a smaller
# one exists. Guarded; never raises.
_BULK_KG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.IGNORECASE)


def _looks_junk(title: str) -> bool:
    """True if the title screams bulk/foodservice/gift/seed/wholesale, or
    quotes a ≥5kg quantity. Conservative by design — false negatives are
    fine (caption still warns), false positives lose a usable listing."""
    if not title:
        return True
    low = title.lower()
    for tok in _JUNK_TOKENS:
        if tok.lower() in low:
            return True
    try:
        for m in _BULK_KG_RE.finditer(title):
            if float(m.group(1)) >= 5:
                return True
    except (ValueError, TypeError):
        pass
    return False

# ---------------------------------------------------------------------------
# Server-side cache: module-level dict keyed by the naver query string. TTL
# ~12h so the NAVER quota is hit at most once per query per half-day. A soft
# cap bounds memory; on overflow the oldest entries are dropped. Same spirit
# as beverages.py.
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
    """Always-set deep-link into NAVER 쇼핑 search for this ingredient, so the
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


def fetch_online_price(display_name: str, query: str) -> dict:
    """Online lowest-listing price for ONE ingredient via NAVER 쇼핑.

    The representative item is the FIRST returned (sim-ranked). ANY problem
    (missing keys, network, HTTP, parse, empty) ⇒ fallback item. Cached per
    query string (~12h) so a lazy expand never re-hits the NAVER quota for
    the same ingredient. Never raises.

    Returns dict keys: name, price, listing, url, mall, status, more_url.
    `status` is "ok" on a real listing else "fallback". `more_url` is ALWAYS
    set (even on fallback). NO ▼% / NO baseline is derived here — `listing`
    carries the raw (often multipack) title so the unit is transparent.
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
            # Prefer the highest sim-ranked listing that isn't obvious
            # junk/bulk; if EVERY candidate looks junky, keep sim #1 — never
            # degrade to no-data (the caption still warns 묶음/대용량).
            top = None
            title = ""
            for cand in listings:
                cand_title = _strip_tags(cand.get("title", ""))
                if cand_title and not _looks_junk(cand_title):
                    top = cand
                    title = cand_title
                    break
            if top is None:
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
