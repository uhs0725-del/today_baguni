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


# Processed / prepared / derived products that merely CONTAIN the ingredient
# name (e.g. 바지락 → "샘표 잔치국수 바지락칼국수 즉석식품"). For a raw-
# ingredient price reference these are misleading, so they're rejected. The
# NAVER category path is the strong signal; the title tokens are a backup
# for when category fields are missing. HIGH-PRECISION only — and the
# category list deliberately EXCLUDES 건어물/수산가공 so dried staples that
# are legitimately sold that way (마른김·멸치·다시마·미역·북어) are NOT
# over-rejected.
_PROCESSED_TOKENS = (
    "칼국수", "국수", "라면", "우동", "파스타", "수제비",
    "밀키트", "즉석", "간편조리", "간편식", "레토르트",
    "통조림", "액젓", "젓갈", "어묵", "맛살",
    "분말", "가루", "즙", "진액", "농축", "엑기스",
    "소스", "양념", "다시다", "다시팩", "육수", "조미료",
    "잼", "버터", "스낵", "과자", "케이크", "쿠키", "초콜릿", "사탕",
    "음료", "주스", "티백", "건강식품", "영양제", "보충제",
    "선식", "시리얼", "지단", "훈제",
)

# NAVER category1~4 substrings that mean prepared/snack/derived — NOT a raw
# grocery item. TIGHT on purpose (no broad "가공식품"/"건어물" — those would
# wrongly kill dried seafood staples).
_BAD_CATEGORY_TOKENS = (
    "면류", "라면", "즉석", "간편조리", "레토르트", "통조림",
    "과자", "스낵", "베이커리", "제과", "음료", "커피",
    "주류", "건강식품", "영양제", "다이어트", "분유", "이유식",
    "조미료", "소스",
)


def _looks_processed(title: str, categories: tuple) -> bool:
    """True if the listing is a processed/prepared/derived product rather
    than the raw ingredient — judged by NAVER category (strong) or, as a
    backup when category fields are absent, a title token. Conservative:
    when in doubt we'd rather show NO price (link-only) than a misleading
    one — the user's explicit 2026-05-19 choice (trust > coverage)."""
    cat = " ".join(c for c in categories if c)
    if cat:
        for tok in _BAD_CATEGORY_TOKENS:
            if tok in cat:
                return True
    for tok in _PROCESSED_TOKENS:
        if tok in (title or ""):
            return True
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


# ---------------------------------------------------------------------------
# Shopping-query builder. ROOT-CAUSE FIX (2026-05-19): the per-ingredient
# `search_keyword` in ingredients.json is RECIPE-oriented (바지락 →
# "바지락칼국수", 계란 → "계란요리", 상추 → "상추겉절이") — great for the
# recipe feature, but as a NAVER 쇼핑 query it literally searches the DISH,
# which is why 바지락 returned 바지락칼국수. For a price lookup we use the
# raw ingredient NAME, with a tiny override for the few names whose raw
# form is still processed-dominated on NAVER. This is the input-side fix;
# the junk/processed reject filters stay exactly as-is.
#
# NOTE: a category-enrichment word ("수산물"/"채소"…) was tried and REMOVED
# (2026-05-19): live data showed it pushed 상추/다시마/멸치 to bigger/
# pricier listings while the bare name already returns the right product.
# `category` is kept in the signature only for call-site stability.
# ❗ DO NOT route search_keyword here again, and DO NOT re-add category
# enrichment (it regressed 1인-size relevance — user's explicit choice).
# ---------------------------------------------------------------------------

# Highest priority: names whose raw form is STILL processed-dominated on
# NAVER. Keep this list tiny and observation-driven (extend only after a
# live check). The value entirely replaces the query.
_QUERY_OVERRIDE = {
    "계란": "달걀 30구",
    "바지락": "생바지락",
}


def build_naver_query(name: str, category: str = "") -> str:
    """NAVER 쇼핑 query for a price lookup: override → else the raw
    ingredient NAME. NEVER the recipe `search_keyword`; NO category
    enrichment (removed — regressed 1인 size). `category` is accepted but
    unused (call-site stability). Pure/total — empty name yields "" (the
    caller already handles the empty case)."""
    nm = (name or "").strip()
    if not nm:
        return ""
    return _QUERY_OVERRIDE.get(nm, nm)


def fetch_online_price(display_name: str, query: str) -> dict:
    """Online lowest-listing price for ONE ingredient via NAVER 쇼핑.

    Two-pass pick: (1) the highest sim-ranked listing that is non-junk AND
    non-processed; failing that (2) a RIGHT product even if bulky/large-
    unit (the UI caption already says 묶음/대용량). A WRONG product
    (processed/wrong-category, e.g. 바지락칼국수) is hard-rejected in both
    passes; if only wrong products exist — or any problem occurs (missing
    keys, network, HTTP, parse, empty) — we return the link-only fallback
    rather than fabricate a guess (user's explicit 2026-05-19 decision:
    wrong product is worse than no price, but right-product-bulky is fine
    with the caption). Cached per query string (~12h) so a lazy expand
    never re-hits the NAVER quota for the same ingredient. Never raises.

    Returns dict keys: name, price, listing, url, mall, status, more_url.
    `status` is "ok" on a CONFIDENT match else "fallback" (the UI shows the
    NAVER 가격비교 deep-link only — no fabricated price). `more_url` is
    ALWAYS set. NO ▼% / NO baseline is derived here — `listing` carries the
    raw title so the unit is transparent.
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
            # Two-pass pick (user decision 2026-05-19, refined):
            #   Pass 1 (ideal): non-junk AND non-processed — a RIGHT
            #     product at a reasonable 1인 size.
            #   Pass 2 (acceptable): a RIGHT product that is merely bulky/
            #     large-unit (junk) — shown WITH the existing 묶음/대용량
            #     caption (restores 양파/계란-type coverage; the raw title
            #     makes the size transparent).
            #   Neither ⇒ link-only fallback (`item` stays _fallback_item):
            #     happens only when EVERY candidate is a WRONG product
            #     (processed/wrong-category, e.g. 바지락 → 바지락칼국수).
            # ❗ "Wrong product" (_looks_processed) is HARD-rejected in
            # BOTH passes and must NEVER be relaxed; and we never fabricate
            # a sim#1 guess. DO NOT collapse this back to a single pass.
            def _cats(c):
                return (
                    c.get("category1") or "",
                    c.get("category2") or "",
                    c.get("category3") or "",
                    c.get("category4") or "",
                )

            top = None
            title = ""
            # Pass 1 — right product, not bulky.
            for cand in listings:
                cand_title = _strip_tags(cand.get("title", ""))
                if not cand_title or _looks_junk(cand_title):
                    continue
                if _looks_processed(cand_title, _cats(cand)):
                    continue
                top = cand
                title = cand_title
                break
            # Pass 2 — right product even if bulky (caption covers size);
            # wrong product (processed/category) still hard-rejected.
            if top is None:
                for cand in listings:
                    cand_title = _strip_tags(cand.get("title", ""))
                    if not cand_title:
                        continue
                    if _looks_processed(cand_title, _cats(cand)):
                        continue
                    top = cand
                    title = cand_title
                    break
            if top is not None:
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
