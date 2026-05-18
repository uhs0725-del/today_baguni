"""Recommendation scoring.

Joins KAMIS price rows to curated ingredient meta and ranks by a blend of
price discount, single-person fit, and seasonality.

score = 0.5*discount + 0.35*solo_fit_norm + 0.15*season_bonus
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from pydantic import BaseModel

from .kamis import PriceRow

_DATA_DIR = Path(__file__).parent / "data"
_INGREDIENTS_PATH = _DATA_DIR / "ingredients.json"
# TRUE "작년 평균" baseline keyed by CURATED ingredient name, built offline
# by scripts/build_baseline.py from KAMIS monthlySalesList (RETAIL). When an
# entry exists for a matched ingredient, change_pct is recomputed as
# (today − yearly_avg)/yearly_avg so reasons/score reflect the year-average
# comparison. Absent/empty/partial file ⇒ that ingredient just keeps the
# kamis.py median-of-3 fallback (_usual_baseline). The web app NEVER builds
# this file at request time — it only reads it (cached, failure-tolerant).
_BASELINE_PATH = _DATA_DIR / "baseline.json"
# Curated ingredient name -> list of EXACT KAMIS item-names it may match.
# Replaces the old loose fuzzy join for the price↔meta path: a price row
# matches an ingredient iff the "/"-split KAMIS item_name equals (after
# .strip()) one of that ingredient's aliases. Empty list ⇒ never live-match
# (kills the false 김치←KAMIS "김"/laver positive). Curated-name lookups
# (/api/ingredient/{name}) still use _matches/find_ingredient — unchanged.
_ALIAS_PATH = _DATA_DIR / "kamis_alias.json"

# discount is mapped from change_pct into [0,1]. A drop of this many percent
# (or more) maps to the max discount score of 1.0; a rise of this much maps
# to 0.0; flat (0%) maps to 0.5; missing change_pct is neutral 0.5.
_DISCOUNT_FULL_DROP_PCT = 25.0

# Maps a KAMIS 부류명 (category) to a coarse, UI-facing filter group.
# Unknown categories fall back to "곡물·기타" (see category_group()).
CATEGORY_GROUP = {
    "채소류": "채소",
    "축산물": "고기·계란",
    "수산물": "수산물",
    "과일류": "과일",
    "식량작물": "곡물·기타",
}

# Known live-KAMIS synonyms that should fold into an existing group.
_CATEGORY_SYNONYM = {
    "특용작물": "곡물·기타",
}


def category_group(category: str) -> str:
    """Map a category name to its filter group, defaulting to '곡물·기타'."""
    if category in CATEGORY_GROUP:
        return CATEGORY_GROUP[category]
    if category in _CATEGORY_SYNONYM:
        return _CATEGORY_SYNONYM[category]
    return "곡물·기타"


class RecipeLink(BaseModel):
    label: str
    url: str


# Base/aromatic ingredients. When a combo is *only* these, a combined recipe
# search ("대파 양파") is low-signal, so we offer a curated staple-only list
# instead. Defined in code (not a JSON field) to stay trivially editable.
STAPLE_INGREDIENTS = {"양파", "대파", "마늘"}


class RecipeSuggestion(BaseModel):
    name: str
    url: str


# Dishes that genuinely need only staples + universal pantry (간장/기름/소금).
# (display name, 만개의레시피 search keyword)
STAPLE_ONLY_RECIPES = [
    ("파기름", "파기름"),
    ("양파볶음", "양파볶음"),
    ("양파장아찌", "양파장아찌"),
    ("대파전", "대파전"),
    ("맑은 양파국", "양파국"),
]


def staple_only_suggestions() -> list[RecipeSuggestion]:
    """Curated staple-only dishes as 만개의레시피 search links."""
    return [
        RecipeSuggestion(
            name=name,
            url=f"https://www.10000recipe.com/recipe/list.html?q={quote(keyword)}",
        )
        for name, keyword in STAPLE_ONLY_RECIPES
    ]


def is_all_staple(resolved_names: list[str]) -> bool:
    """True iff the list is non-empty and every name is a staple ingredient."""
    return bool(resolved_names) and all(
        name in STAPLE_INGREDIENTS for name in resolved_names
    )


def order_for_query(names: list[str]) -> list[str]:
    """Lead the recipe query with the distinctive 'main' ingredient and put
    the ubiquitous aromatics (양파/대파/마늘) LAST. External recipe search —
    especially 만개의레시피's loose `q=` — over-weights the most common
    token, so "삼겹살 양파" returns real 삼겹살+양파 dishes while
    "양파 삼겹살" floods with onion-only recipes (verified against
    만개의레시피). Stable otherwise; pure reordering, drops nothing."""
    mains = [n for n in names if n not in STAPLE_INGREDIENTS]
    staples = [n for n in names if n in STAPLE_INGREDIENTS]
    return mains + staples


# Dish-name synonym expansion for the content-match relevance filter. A
# selected MAIN ingredient (display name) rarely appears verbatim in a recipe
# title/ingredient line ("돼지 삼겹살" → titled "삼겹살양파볶음" / "제육볶음"),
# so each entry maps a SUBSTRING of the display name to extra words that
# strongly imply that ingredient. Substring match keeps it trivially editable
# and covers variants ("수입 소갈비"/"한우 양지" both hit the 소 family).
#
# IMPORTANT: terms are SUBSTRING-matched against despaced Korean text, so a
# bare single Hangul char would over-match catastrophically — e.g. "소"
# appears inside 소금(salt)/소스(sauce)/채소, and "돈" inside many words, so
# almost every recipe's ingredient line would falsely pass. We therefore use
# only ≥2-char, ingredient-specific tokens here (the search keyword + display
# tokens still cover the rest). Verified: dropping bare "소"/"돈" removes the
# 양파오이무침/양파오징어덮밥 false-positives on a 한우 query while real beef
# dishes (소고기미역국, 소불고기, 갈비찜) still match.
_CONTENT_SYNONYMS: list[tuple[str, set[str]]] = [
    ("돼지", {"돼지", "삼겹", "목살", "앞다리", "뒷다리", "제육", "두루치기", "수육", "돈가스", "돈까스", "돈부리", "돈볶음"}),
    ("한우", {"쇠고기", "소고기", "한우", "불고기", "갈비"}),
    ("소갈비", {"쇠고기", "소고기", "한우", "불고기", "갈비"}),
    ("소 ", {"쇠고기", "소고기", "한우", "불고기", "갈비"}),
    ("수입 소", {"쇠고기", "소고기", "한우", "불고기", "갈비"}),
    ("닭", {"닭", "치킨"}),
    ("계란", {"계란", "달걀", "에그"}),
]

# Fish/seafood display words that should also be searched as-is in titles.
_FISH_WORDS = {
    "고등어", "갈치", "오징어", "명태", "코다리", "동태", "새우",
    "멸치", "김", "미역", "다시마",
}


def _despace_lower(text: str) -> str:
    """Lowercase and drop ALL whitespace so substring checks are robust to
    spacing differences ("소 고기" vs "소고기", "삼겹 살" vs "삼겹살")."""
    return re.sub(r"\s+", "", (text or "").lower())


def content_match_terms(name: str, keyword: str) -> set[str]:
    """Term set used to decide a recipe genuinely uses this MAIN ingredient.

    Returns lowercased, whitespace-stripped Korean strings; a recipe passes
    for this ingredient if ANY term is a substring of its (despaced, lowered)
    title/ingredient text. Always includes the search keyword and each
    whitespace token of the display name; dict-driven synonyms broaden meat
    so dish-named recipes still match, and fish words are added verbatim."""
    terms: set[str] = set()
    if keyword:
        terms.add(keyword)
    for tok in (name or "").split():
        if tok:
            terms.add(tok)
    for needle, extra in _CONTENT_SYNONYMS:
        if needle in name:
            terms.update(extra)
    for tok in (name or "").split():
        if tok in _FISH_WORDS:
            terms.add(tok)
    return {t for t in (_despace_lower(x) for x in terms) if t}


class Recommendation(BaseModel):
    name: str
    category: str
    price: int
    unit: str
    change_pct: Optional[float] = None
    reasons: list[str]
    solo_fit: int
    storage_tip: str
    season: bool
    score: float
    recipe_links: list[RecipeLink]
    group: str


class Ingredient(BaseModel):
    name: str
    category: str
    solo_fit: int
    storage_tip: str
    season_months: list[int]
    search_keyword: str


def load_ingredients() -> list[Ingredient]:
    with _INGREDIENTS_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    return [Ingredient(**r) for r in raw]


_alias_cache: Optional[dict[str, list[str]]] = None


def _load_alias_map() -> dict[str, list[str]]:
    """Load (once) the curated-name → exact-KAMIS-item-names alias map.

    Keys starting with '_' (e.g. the embedded _comment) are ignored. The map
    is read a single time and cached for the process lifetime.
    """
    global _alias_cache
    if _alias_cache is None:
        with _ALIAS_PATH.open(encoding="utf-8") as f:
            raw = json.load(f)
        _alias_cache = {
            k: [str(a).strip() for a in v]
            for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, list)
        }
    return _alias_cache


_baseline_cache: Optional[dict[str, dict]] = None


def _load_baseline_map() -> dict[str, dict]:
    """Load (once) the curated-name → yearly-average baseline map.

    Built offline by scripts/build_baseline.py. Missing file, bad JSON, or a
    non-dict payload ⇒ {} (the universal median fallback stays in effect).
    NEVER raises — a baseline problem must not break recommendations.
    """
    global _baseline_cache
    if _baseline_cache is None:
        try:
            with _BASELINE_PATH.open(encoding="utf-8") as f:
                raw = json.load(f)
            _baseline_cache = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _baseline_cache = {}
    return _baseline_cache


def _yearly_avg_for(meta_name: str) -> Optional[int]:
    """Return a positive int yearly_avg for this curated name, else None.

    Tolerates partial/garbled entries (missing or non-numeric yearly_avg,
    non-dict value) by returning None ⇒ caller keeps the median fallback.
    """
    entry = _load_baseline_map().get(meta_name)
    if not isinstance(entry, dict):
        return None
    try:
        avg = int(entry.get("yearly_avg"))
    except (TypeError, ValueError):
        return None
    return avg if avg > 0 else None


def _alias_matches(price_name: str, meta_name: str) -> bool:
    """Exact alias join: True iff the ("/"-split) KAMIS item_name equals,
    after .strip(), one of meta_name's curated aliases. An empty (or
    unknown) alias list never matches a live price row."""
    aliases = _load_alias_map().get(meta_name)
    if not aliases:
        return False
    return price_name.strip() in aliases


def _normalize(name: str) -> str:
    """Strip parentheticals and whitespace for fuzzy name matching."""
    no_paren = re.sub(r"[(\[].*?[)\]]", "", name)
    return re.sub(r"\s+", "", no_paren).strip()


def _matches(price_name: str, meta_name: str) -> bool:
    p = _normalize(price_name)
    m = _normalize(meta_name)
    if not p or not m:
        return False
    return p == m or p in m or m in p


def build_recipe_links(keyword: str) -> list[RecipeLink]:
    """Server-side recipe search links (no extra API key — the v1 wedge)."""
    k = quote(keyword)
    k_recipe = quote(f"{keyword} 자취 레시피")
    return [
        RecipeLink(
            label="만개의레시피",
            url=f"https://www.10000recipe.com/recipe/list.html?q={k}",
        ),
        RecipeLink(
            label="유튜브",
            url=f"https://www.youtube.com/results?search_query={k_recipe}",
        ),
        RecipeLink(
            label="네이버 블로그",
            url=f"https://search.naver.com/search.naver?where=blog&query={k_recipe}",
        ),
    ]


def build_combo_recipe_links(keywords: list[str]) -> list[RecipeLink]:
    """Combined multi-ingredient search links (same no-API-key approach)."""
    joined = " ".join(keywords)
    k = quote(joined)
    q = quote(f"{joined} 레시피")
    return [
        RecipeLink(
            label="만개의레시피",
            url=f"https://www.10000recipe.com/recipe/list.html?q={k}",
        ),
        RecipeLink(
            label="유튜브",
            url=f"https://www.youtube.com/results?search_query={q}",
        ),
        RecipeLink(
            label="네이버 블로그",
            url=f"https://search.naver.com/search.naver?where=blog&query={q}",
        ),
    ]


def _discount_score(change_pct: Optional[float]) -> float:
    """Map change_pct → [0,1]. More negative (cheaper) → higher. None → 0.5."""
    if change_pct is None:
        return 0.5
    # change_pct = -25 → 1.0 ; 0 → 0.5 ; +25 → 0.0
    score = 0.5 - (change_pct / (2 * _DISCOUNT_FULL_DROP_PCT))
    return max(0.0, min(1.0, score))


def _build_reasons(
    change_pct: Optional[float],
    solo_fit: int,
    season: bool,
) -> list[str]:
    reasons: list[str] = []

    if change_pct is not None:
        if change_pct <= -3:
            reasons.append(f"평소보다 {abs(round(change_pct))}%↓ 쌀 때")
        elif change_pct >= 3:
            reasons.append(f"평소보다 {round(change_pct)}%↑ (참고용)")
        else:
            reasons.append("가격 변동 거의 없음")
    else:
        reasons.append("가격 비교 정보 없음")

    if solo_fit >= 4:
        reasons.append("소분·보관 쉬워 안 버림")
    elif solo_fit <= 2:
        reasons.append("잘 상하니 바로 먹을 만큼만")

    if season:
        reasons.append("지금 제철")

    return reasons


def rank(prices: list[PriceRow], today_month: int) -> list[Recommendation]:
    """Join prices↔meta, score, and return matched items sorted desc by score."""
    ingredients = load_ingredients()
    recs: list[Recommendation] = []
    # KAMIS returns several 품종/시장 rows per item; keep the first (대표)
    # match per ingredient so the feed shows each ingredient once.
    seen: set[str] = set()

    for price in prices:
        meta = next(
            (ing for ing in ingredients if _alias_matches(price.item_name, ing.name)),
            None,
        )
        if meta is None:
            continue
        if meta.name in seen:
            continue
        seen.add(meta.name)

        # TRUE "작년 평균" override: if the offline baseline has a yearly
        # average for THIS curated ingredient, recompute change_pct against
        # it (today vs last year's average) so discount/reasons/score all
        # reflect the year-average comparison. No baseline entry ⇒ keep the
        # kamis.py median-of-3 change_pct exactly as before (unchanged path).
        change_pct = price.change_pct
        yearly_avg = _yearly_avg_for(meta.name)
        if yearly_avg is not None:
            change_pct = round(
                (price.price - yearly_avg) / yearly_avg * 100, 1
            )

        season = today_month in meta.season_months
        discount = _discount_score(change_pct)
        solo_fit_norm = meta.solo_fit / 5.0
        season_bonus = 1.0 if season else 0.0

        score = 0.5 * discount + 0.35 * solo_fit_norm + 0.15 * season_bonus

        recs.append(
            Recommendation(
                name=meta.name,
                category=meta.category,
                price=price.price,
                unit=price.unit,
                change_pct=change_pct,
                reasons=_build_reasons(change_pct, meta.solo_fit, season),
                solo_fit=meta.solo_fit,
                storage_tip=meta.storage_tip,
                season=season,
                score=round(score, 4),
                recipe_links=build_recipe_links(meta.search_keyword),
                group=category_group(meta.category),
            )
        )

    recs.sort(key=lambda r: r.score, reverse=True)
    return recs


def find_ingredient(name: str) -> Optional[Ingredient]:
    """Look up a single ingredient by fuzzy name match (for the detail route)."""
    for ing in load_ingredients():
        if _matches(name, ing.name):
            return ing
    return None
