"""Inline recipe search results via official APIs (graceful degradation).

Three independent sources — 만개의레시피 (best-effort HTML scrape, no key),
네이버 블로그 (NAVER 검색 API, needs key), 유튜브 (YouTube Data API v3,
needs key). Each source is fetched behind its own try/except and a shared
module-level TTL cache so external APIs are NOT hit per user request
(quota + abuse protection). ANY failure / missing key / timeout / empty
result degrades to status "fallback" with the existing deep-link as
`more_url` — i.e. the previous behaviour is the graceful fallback.

On top of fetching, results are RELEVANCE-filtered so they genuinely use
the selected MAIN ingredient(s): every source filters by title (+ snippet),
and 만개의레시피 additionally fetches each candidate's detail page and
checks its `recipeIngredient` list (JSON-LD, HTML fallback). This kills the
"양파 오이무침" / "부추 양파무침" noise that 만개's loose `q=` returns for
양파+돼지삼겹살. No `main_terms` (all-staple selection) ⇒ no filtering.

The public entry point `gather_recipe_results(names, main_terms)` NEVER
raises.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

# External calls are best-effort; keep the timeout short so a slow/blocked
# upstream can't stall the API response.
_HTTP_TIMEOUT = 6.0

# Per-detail-page fetch (만개의레시피 relevance check) — shorter still, since
# we may do several sequentially and must bound total added latency.
_DETAIL_TIMEOUT = 5.0

# Per-source result cap (matches the spec: "up to 6").
_MAX_RESULTS = 6

# 만개의레시피 relevance scan bounds: pull more list candidates than we need
# (loose q= is noisy), then detail-check until enough pass or the hard
# fetch cap is hit (bounds worst-case latency on an uncached query).
_SCRAPE_CANDIDATES = 15
_DETAIL_FETCH_CAP = 12

# ---------------------------------------------------------------------------
# Server-side cache: module-level dict keyed by (source, query). TTL ~12h so
# external APIs are called at most once per (source, query) per half-day. A
# soft cap (~256 entries) bounds memory; on overflow the oldest entries are
# dropped. Each source is cached independently.
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 12 * 60 * 60  # ~12h
_CACHE_MAX_ENTRIES = 256

# key: (source_key, query) -> (stored_at_epoch, source_dict)
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _cache_get(source_key: str, query: str) -> Optional[dict]:
    entry = _cache.get((source_key, query))
    if entry is None:
        return None
    stored_at, payload = entry
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _cache.pop((source_key, query), None)
        return None
    return payload


def _cache_set(source_key: str, query: str, payload: dict) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        # Drop the oldest ~1/8 of entries so we don't evict one-at-a-time
        # on every call once full.
        for key in sorted(_cache, key=lambda k: _cache[k][0])[
            : max(1, _CACHE_MAX_ENTRIES // 8)
        ]:
            _cache.pop(key, None)
    _cache[(source_key, query)] = (time.time(), payload)


# ---------------------------------------------------------------------------
# 만개의레시피 detail-page ingredient cache: recipe-id -> (stored_at, text).
# Popular recipes recur across queries, and the detail fetch is the slow
# part of the relevance check, so cache the parsed ingredient text for ~7d
# (capped). Independent of the per-source result cache above.
# ---------------------------------------------------------------------------
_DETAIL_TTL_SECONDS = 7 * 24 * 60 * 60  # ~7d
_DETAIL_CACHE_MAX = 512
_detail_cache: dict[str, tuple[float, str]] = {}


def _detail_cache_get(rid: str) -> Optional[str]:
    entry = _detail_cache.get(rid)
    if entry is None:
        return None
    stored_at, text = entry
    if time.time() - stored_at > _DETAIL_TTL_SECONDS:
        _detail_cache.pop(rid, None)
        return None
    return text


def _detail_cache_set(rid: str, text: str) -> None:
    if len(_detail_cache) >= _DETAIL_CACHE_MAX:
        for key in sorted(_detail_cache, key=lambda k: _detail_cache[k][0])[
            : max(1, _DETAIL_CACHE_MAX // 8)
        ]:
            _detail_cache.pop(key, None)
    _detail_cache[rid] = (time.time(), text)


# ---------------------------------------------------------------------------
# Relevance filter — does `text` genuinely reference EVERY selected MAIN
# ingredient? `main_terms` is one term-set per MAIN name (from
# ranking.content_match_terms). Empty/None ⇒ no filtering (all-staple
# selection keeps the previous behaviour). Match is substring on the
# lowercased, whitespace-stripped text so spacing variants still hit.
# ---------------------------------------------------------------------------
def _despace_lower(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _passes(text: str, main_terms: Optional[list]) -> bool:
    if not main_terms:
        return True
    hay = _despace_lower(text)
    if not hay:
        return False
    for term_set in main_terms:
        if not term_set:
            # An empty term-set can't be satisfied; treat as no constraint
            # rather than rejecting everything.
            continue
        if not any(_despace_lower(t) in hay for t in term_set):
            return False
    return True


def _main_terms_sig(main_terms: Optional[list]) -> str:
    """Deterministic short signature of main_terms for the cache key, so a
    changed selection never serves a stale filtered set. Tolerates a falsy
    (e.g. None) term-set entry the same way _passes does — it just doesn't
    contribute terms — so the route can never raise on odd input."""
    if not main_terms:
        return ""
    return repr(
        [
            sorted(_despace_lower(t) for t in s) if s else []
            for s in main_terms
        ]
    )


# ---------------------------------------------------------------------------
# Deep-links (the graceful fallback — identical to ranking.build_combo_recipe_links)
# ---------------------------------------------------------------------------
def _more_url_10000(joined: str) -> str:
    return f"https://www.10000recipe.com/recipe/list.html?q={quote(joined)}"


def _more_url_naver(joined: str) -> str:
    q = quote(f"{joined} 레시피")
    return f"https://search.naver.com/search.naver?where=blog&query={q}"


def _more_url_youtube(joined: str) -> str:
    q = quote(f"{joined} 레시피")
    return f"https://www.youtube.com/results?search_query={q}"


def _strip_tags(text: str) -> str:
    """Remove HTML tags and unescape entities (NAVER returns <b>…</b>)."""
    no_tags = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(no_tags).strip()


# ---------------------------------------------------------------------------
# 만개의레시피 — best-effort HTML scrape (NO new deps; tolerant regex).
# ---------------------------------------------------------------------------
# Tolerant regex over the 만개의레시피 list items. The list page renders each
# recipe as `<li class="common_sp_list_li"> … <a href="/recipe/<id>"> <img
# src="<thumb>"> … <div class="common_sp_caption_tit …">제목</div>`. We split
# on the list-item class and, per item, pull the recipe id, thumbnail, and
# caption title with forgiving patterns. A markup change just yields fewer/no
# matches (-> fallback) — it never raises.
_LI_SPLIT_RE = re.compile(r'common_sp_list_li')
_RECIPE_ID_RE = re.compile(r'/recipe/(\d+)')
_THUMB_RE = re.compile(r'common_sp_thumb.*?<img[^>]+src="([^"]+)"', re.DOTALL)
_TITLE_RE = re.compile(
    r'common_sp_caption_tit[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)


def _parse_10000recipe_html(text: str, limit: int = _MAX_RESULTS) -> list[dict]:
    """Best-effort: extract up to `limit` recipe cards. Never raises."""
    results: list[dict] = []
    seen: set[str] = set()
    # First chunk is the pre-list page head; recipe items follow.
    for chunk in _LI_SPLIT_RE.split(text)[1:]:
        if len(results) >= limit:
            break
        id_m = _RECIPE_ID_RE.search(chunk)
        if not id_m:
            continue
        rid = id_m.group(1)
        if rid in seen:
            continue
        title_m = _TITLE_RE.search(chunk)
        if not title_m:
            continue
        title = _strip_tags(title_m.group(1))
        if not title:
            continue
        thumb_m = _THUMB_RE.search(chunk)
        seen.add(rid)
        results.append(
            {
                "title": title,
                "url": f"https://www.10000recipe.com/recipe/{rid}",
                "thumbnail": thumb_m.group(1) if thumb_m else None,
            }
        )
    return results


# Detail-page ingredient extraction. Prefer JSON-LD (robust, structured);
# fall back to the HTML ingredient area. Both are best-effort — a markup
# change just yields "" (the item then relies on title-only matching).
_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# HTML fallback: the confirmed-ingredient area lists each item; grab the
# visible label text. 만개 renders them as
# `<div class="ingre_list_name"><a ...>설탕</a></div>` inside
# `#divConfirmedMaterialArea` / `.ready_ingre3`.
_INGRE_AREA_RE = re.compile(
    r'(divConfirmedMaterialArea|ready_ingre3).*?(?=<script|</body>)',
    re.DOTALL | re.IGNORECASE,
)
_INGRE_NAME_RE = re.compile(
    r'ingre_list_name[^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</', re.DOTALL
)


def _ld_recipe_ingredients(blob: str) -> list[str]:
    """Pull recipeIngredient from one JSON-LD blob. Handles a bare object,
    a list of objects, or an @graph wrapper. Never raises."""
    try:
        data = json.loads(blob.strip())
    except (ValueError, TypeError):
        return []
    candidates: list = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            candidates = data["@graph"]
        else:
            candidates = [data]
    for node in candidates:
        if not isinstance(node, dict):
            continue
        ing = node.get("recipeIngredient") or node.get("ingredients")
        if isinstance(ing, list) and ing:
            return [str(x) for x in ing if x]
        if isinstance(ing, str) and ing.strip():
            return [ing]
    return []


def _extract_detail_ingredient_text(html_text: str) -> str:
    """Joined ingredient text for a 만개 recipe detail page. Tries JSON-LD
    `recipeIngredient` first, then the HTML ingredient area. "" if neither
    yields anything (caller then falls back to title-only matching)."""
    for m in _LD_JSON_RE.finditer(html_text or ""):
        ings = _ld_recipe_ingredients(m.group(1))
        if ings:
            return " ".join(ings)
    area_m = _INGRE_AREA_RE.search(html_text or "")
    if area_m:
        names = [
            _strip_tags(x) for x in _INGRE_NAME_RE.findall(area_m.group(0))
        ]
        names = [n for n in names if n]
        if names:
            return " ".join(names)
    return ""


def _fetch_detail_ingredient_text(client: httpx.Client, rid: str) -> str:
    """Cached detail-page ingredient text for a recipe id. Network/parse
    failure ⇒ "" (item falls back to title-only relevance). Never raises."""
    cached = _detail_cache_get(rid)
    if cached is not None:
        return cached
    try:
        resp = client.get(
            f"https://www.10000recipe.com/recipe/{rid}",
            timeout=_DETAIL_TIMEOUT,
        )
        resp.raise_for_status()
        text = _extract_detail_ingredient_text(resp.text)
    except Exception:
        return ""
    _detail_cache_set(rid, text)
    return text


_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def _fetch_10000recipe(joined: str, main_terms: Optional[list]) -> dict:
    """Best-effort scrape of 만개의레시피 + per-candidate detail-page
    relevance check. ANY problem -> fallback (deep-link `more_url`).

    Loose `q=` is noisy, so we pull up to _SCRAPE_CANDIDATES list items then,
    for each, fetch its detail page and keep it iff title + recipeIngredient
    references every selected MAIN ingredient. Stops once _MAX_RESULTS pass
    OR _DETAIL_FETCH_CAP detail fetches are done (bounds latency). A failed
    detail fetch for an item falls back to title-only matching for THAT item
    (we don't lose everything). No main_terms ⇒ title-list as before.
    """
    more_url = _more_url_10000(joined)
    source = {
        "key": "10000recipe",
        "label": "만개의레시피",
        "status": "fallback",
        "results": [],
        "more_url": more_url,
    }
    try:
        url = (
            "https://www.10000recipe.com/recipe/list.html?q="
            + quote(joined)
        )
        with httpx.Client(
            follow_redirects=True, headers=_UA_HEADERS
        ) as client:
            resp = client.get(url, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()

            if not main_terms:
                # No MAIN ingredient (all-staple) — previous behaviour:
                # just the title list, no detail fetches.
                results = _parse_10000recipe_html(resp.text)
                if results:
                    source["results"] = results
                    source["status"] = "ok"
                return source

            candidates = _parse_10000recipe_html(
                resp.text, limit=_SCRAPE_CANDIDATES
            )
            passed: list[dict] = []
            fetches = 0
            for cand in candidates:
                if len(passed) >= _MAX_RESULTS:
                    break
                if fetches >= _DETAIL_FETCH_CAP:
                    break
                rid_m = _RECIPE_ID_RE.search(cand["url"])
                if not rid_m:
                    continue
                rid = rid_m.group(1)
                cached_text = _detail_cache_get(rid)
                if cached_text is None:
                    fetches += 1
                    ingredient_text = _fetch_detail_ingredient_text(
                        client, rid
                    )
                else:
                    ingredient_text = cached_text
                if ingredient_text:
                    check_text = cand["title"] + " " + ingredient_text
                else:
                    # Detail fetch failed/empty — fall back to title-only
                    # for this item so we don't drop everything.
                    check_text = cand["title"]
                if _passes(check_text, main_terms):
                    passed.append(cand)

            if passed:
                source["results"] = passed[:_MAX_RESULTS]
                source["status"] = "ok"
    except Exception:
        # Tolerant by design — keep the fallback shape.
        source["results"] = []
        source["status"] = "fallback"
    return source


# ---------------------------------------------------------------------------
# 네이버 블로그 — NAVER 검색 API (needs NAVER_CLIENT_ID + NAVER_CLIENT_SECRET).
# ---------------------------------------------------------------------------
def _fetch_naver(joined: str, main_terms: Optional[list]) -> dict:
    more_url = _more_url_naver(joined)
    source = {
        "key": "naver",
        "label": "네이버 블로그",
        "status": "fallback",
        "results": [],
        "more_url": more_url,
    }
    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return source
    try:
        resp = httpx.get(
            "https://openapi.naver.com/v1/search/blog.json",
            params={
                "query": f"{joined} 레시피",
                # Over-fetch then relevance-filter so a noisy top result
                # set can still yield up to _MAX_RESULTS real matches.
                "display": min(30, _MAX_RESULTS * 4),
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
        results = []
        for item in data.get("items") or []:
            if len(results) >= _MAX_RESULTS:
                break
            title = _strip_tags(item.get("title", ""))
            link = item.get("link") or ""
            if not title or not link:
                continue
            desc = _strip_tags(item.get("description", ""))
            if not _passes(title + " " + desc, main_terms):
                continue
            results.append(
                {
                    "title": title,
                    "url": link,
                    "desc": desc,
                }
            )
        if results:
            source["results"] = results
            source["status"] = "ok"
    except Exception:
        source["results"] = []
        source["status"] = "fallback"
    return source


# ---------------------------------------------------------------------------
# 유튜브 — YouTube Data API v3 (needs YOUTUBE_API_KEY).
# ---------------------------------------------------------------------------
def _fetch_youtube(joined: str, main_terms: Optional[list]) -> dict:
    more_url = _more_url_youtube(joined)
    source = {
        "key": "youtube",
        "label": "유튜브",
        "status": "fallback",
        "results": [],
        "more_url": more_url,
    }
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        return source
    try:
        resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "type": "video",
                # Over-fetch then relevance-filter (API max page = 50).
                "maxResults": min(50, _MAX_RESULTS * 4),
                "q": f"{joined} 레시피",
                "key": api_key,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("items") or []:
            if len(results) >= _MAX_RESULTS:
                break
            vid = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not vid:
                continue
            title = html.unescape(snippet.get("title", ""))
            desc = html.unescape(snippet.get("description", "") or "")
            if not _passes(title + " " + desc, main_terms):
                continue
            thumbs = snippet.get("thumbnails") or {}
            medium = thumbs.get("medium") or {}
            results.append(
                {
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "thumbnail": medium.get("url"),
                    "channel": snippet.get("channelTitle", ""),
                }
            )
        if results:
            source["results"] = results
            source["status"] = "ok"
    except Exception:
        source["results"] = []
        source["status"] = "fallback"
    return source


# ---------------------------------------------------------------------------
# Public entry point — cached per source, NEVER raises.
# ---------------------------------------------------------------------------
_FETCHERS = (
    ("10000recipe", _fetch_10000recipe),
    ("naver", _fetch_naver),
    ("youtube", _fetch_youtube),
)


def gather_recipe_results(
    names: list[str], main_terms: Optional[list] = None
) -> dict:
    """Build the /api/recipe-results payload for the resolved names.

    `query` is the human space-joined resolved names. Results are
    relevance-filtered to the selected MAIN ingredient(s) via `main_terms`
    (one term-set per MAIN name; empty/None ⇒ no filtering, the previous
    behaviour). Each source is independently cached (TTL ~12h) and fetched
    behind its own try/except, so a single failing/slow source can never
    break the response or hit quota per call. The cache key folds in a
    deterministic signature of `main_terms` so changing the selection never
    serves a stale filtered set.
    """
    joined = " ".join(names)
    # Cache key (NOT the user-facing query): query text + main_terms sig.
    sig = _main_terms_sig(main_terms)
    cache_query = joined + "|" + sig
    sources: list[dict] = []
    for source_key, fetcher in _FETCHERS:
        try:
            cached = _cache_get(source_key, cache_query)
            if cached is not None:
                sources.append(cached)
                continue
            result = fetcher(joined, main_terms)
        except Exception:
            # Defensive: a fetcher should already be self-contained, but the
            # route must never raise — degrade to a minimal fallback source.
            result = {
                "key": source_key,
                "label": {
                    "10000recipe": "만개의레시피",
                    "naver": "네이버 블로그",
                    "youtube": "유튜브",
                }.get(source_key, source_key),
                "status": "fallback",
                "results": [],
                "more_url": {
                    "10000recipe": _more_url_10000,
                    "naver": _more_url_naver,
                    "youtube": _more_url_youtube,
                }[source_key](joined),
            }
        _cache_set(source_key, cache_query, result)
        sources.append(result)

    return {"query": joined, "sources": sources}
