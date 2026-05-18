"""Inline recipe search results via official APIs (graceful degradation).

Three independent sources — 만개의레시피 (best-effort HTML scrape, no key),
네이버 블로그 (NAVER 검색 API, needs key), 유튜브 (YouTube Data API v3,
needs key). Each source is fetched behind its own try/except and a shared
module-level TTL cache so external APIs are NOT hit per user request
(quota + abuse protection). ANY failure / missing key / timeout / empty
result degrades to status "fallback" with the existing deep-link as
`more_url` — i.e. the previous behaviour is the graceful fallback.

The public entry point `gather_recipe_results(names)` NEVER raises.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

# External calls are best-effort; keep the timeout short so a slow/blocked
# upstream can't stall the API response.
_HTTP_TIMEOUT = 6.0

# Per-source result cap (matches the spec: "up to 6").
_MAX_RESULTS = 6

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


def _parse_10000recipe_html(text: str) -> list[dict]:
    """Best-effort: extract up to _MAX_RESULTS recipe cards. Never raises."""
    results: list[dict] = []
    seen: set[str] = set()
    # First chunk is the pre-list page head; recipe items follow.
    for chunk in _LI_SPLIT_RE.split(text)[1:]:
        if len(results) >= _MAX_RESULTS:
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


def _fetch_10000recipe(joined: str) -> dict:
    """Best-effort scrape of 만개의레시피 list page. ANY problem -> fallback.

    `more_url` is ALWAYS the deep-link (set regardless of fetch outcome).
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
        resp = httpx.get(
            url,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        results = _parse_10000recipe_html(resp.text)
        if results:
            source["results"] = results
            source["status"] = "ok"
    except Exception:
        # Tolerant by design — keep the fallback shape.
        source["results"] = []
        source["status"] = "fallback"
    return source


# ---------------------------------------------------------------------------
# 네이버 블로그 — NAVER 검색 API (needs NAVER_CLIENT_ID + NAVER_CLIENT_SECRET).
# ---------------------------------------------------------------------------
def _fetch_naver(joined: str) -> dict:
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
                "display": _MAX_RESULTS,
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
        for item in (data.get("items") or [])[:_MAX_RESULTS]:
            title = _strip_tags(item.get("title", ""))
            link = item.get("link") or ""
            if not title or not link:
                continue
            results.append(
                {
                    "title": title,
                    "url": link,
                    "desc": _strip_tags(item.get("description", "")),
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
def _fetch_youtube(joined: str) -> dict:
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
                "maxResults": _MAX_RESULTS,
                "q": f"{joined} 레시피",
                "key": api_key,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in (data.get("items") or [])[:_MAX_RESULTS]:
            vid = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not vid:
                continue
            thumbs = snippet.get("thumbnails") or {}
            medium = thumbs.get("medium") or {}
            results.append(
                {
                    "title": html.unescape(snippet.get("title", "")),
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


def gather_recipe_results(names: list[str]) -> dict:
    """Build the /api/recipe-results payload for the resolved names.

    `query` is the space-joined resolved names. Each source is independently
    cached (TTL ~12h) and fetched behind its own try/except, so a single
    failing/slow source can never break the response or hit quota per call.
    """
    joined = " ".join(names)
    sources: list[dict] = []
    for source_key, fetcher in _FETCHERS:
        try:
            cached = _cache_get(source_key, joined)
            if cached is not None:
                sources.append(cached)
                continue
            result = fetcher(joined)
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
        _cache_set(source_key, joined, result)
        sources.append(result)

    return {"query": joined, "sources": sources}
