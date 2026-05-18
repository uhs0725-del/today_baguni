"""FastAPI app: serves the static frontend at / and JSON APIs under /api."""

from __future__ import annotations

from pathlib import Path as _Path

from dotenv import load_dotenv

# Load .env (KAMIS_CERT_KEY / KAMIS_CERT_ID) no matter how the app is
# launched — run.py, `uvicorn` directly, or the preview runner. Use an
# absolute path so it works regardless of the process working directory.
load_dotenv(_Path(__file__).resolve().parent.parent / ".env")

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .kamis import get_today_prices
from .recipes import gather_recipe_results
from .ranking import (
    Recommendation,
    RecipeLink,
    RecipeSuggestion,
    build_combo_recipe_links,
    build_recipe_links,
    find_ingredient,
    is_all_staple,
    rank,
    staple_only_suggestions,
)

# KST — date shown in the UI and used for seasonality.
KST = timezone(timedelta(hours=9))

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="자취 식탁", description="오늘 뭐 사 먹지?")


@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    """Don't let browsers cache the HTML/CSS/JS — otherwise an updated
    style.css/app.js can render stale (e.g. the sample badge lingering on
    live data). API responses are unaffected."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe for PaaS health checks — no deps, no external calls."""
    return {"ok": True}


class RecommendationsResponse(BaseModel):
    source: Literal["live", "sample"]
    date: str
    items: list[Recommendation]


class IngredientDetail(BaseModel):
    source: Literal["live", "sample"]
    name: str
    category: str
    price: Optional[int] = None
    unit: Optional[str] = None
    change_pct: Optional[float] = None
    reasons: list[str]
    solo_fit: int
    storage_tip: str
    season: bool
    recipe_links: list


class ComboRecipesResponse(BaseModel):
    items: list[str]
    recipe_links: list[RecipeLink]
    all_staple: bool = False
    suggestions: list[RecipeSuggestion] = []


class RecipeResultsSource(BaseModel):
    key: str
    label: str
    status: Literal["ok", "fallback"]
    results: list[dict] = []
    more_url: str


class RecipeResultsResponse(BaseModel):
    query: str
    sources: list[RecipeResultsSource]


@app.get("/api/recommendations", response_model=RecommendationsResponse)
def get_recommendations(limit: int = 12) -> RecommendationsResponse:
    price_result = get_today_prices()
    now_kst = datetime.now(KST)
    items = rank(price_result.rows, now_kst.month)
    if limit > 0:
        items = items[:limit]
    return RecommendationsResponse(
        source=price_result.source,
        date=now_kst.strftime("%Y-%m-%d"),
        items=items,
    )


@app.get("/api/ingredient/{name}", response_model=IngredientDetail)
def get_ingredient(name: str) -> IngredientDetail:
    meta = find_ingredient(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 재료: {name}")

    price_result = get_today_prices()
    now_kst = datetime.now(KST)
    season = now_kst.month in meta.season_months

    # Reuse the ranking join so price/reasons stay consistent with the feed.
    matched = next(
        (r for r in rank(price_result.rows, now_kst.month) if r.name == meta.name),
        None,
    )

    if matched is not None:
        return IngredientDetail(
            source=price_result.source,
            name=meta.name,
            category=meta.category,
            price=matched.price,
            unit=matched.unit,
            change_pct=matched.change_pct,
            reasons=matched.reasons,
            solo_fit=meta.solo_fit,
            storage_tip=meta.storage_tip,
            season=season,
            recipe_links=matched.recipe_links,
        )

    # Known ingredient but no price row today — still return meta + links.
    reasons = ["오늘 시세 정보는 없지만 자취 상비 추천"]
    if meta.solo_fit >= 4:
        reasons.append("소분·보관 쉬워 안 버림")
    if season:
        reasons.append("지금 제철")
    return IngredientDetail(
        source=price_result.source,
        name=meta.name,
        category=meta.category,
        price=None,
        unit=None,
        change_pct=None,
        reasons=reasons,
        solo_fit=meta.solo_fit,
        storage_tip=meta.storage_tip,
        season=season,
        recipe_links=build_recipe_links(meta.search_keyword),
    )


@app.get("/api/combo-recipes", response_model=ComboRecipesResponse)
def get_combo_recipes(items: str = "") -> ComboRecipesResponse:
    names = [n.strip() for n in items.split(",") if n.strip()]

    resolved_names: list[str] = []
    keywords: list[str] = []
    for name in names:
        meta = find_ingredient(name)
        if meta is None:
            continue
        if meta.name not in resolved_names:
            resolved_names.append(meta.name)
            keywords.append(meta.search_keyword)

    if not resolved_names:
        raise HTTPException(status_code=400, detail="선택한 재료를 찾을 수 없어요")

    all_staple = is_all_staple(resolved_names)

    return ComboRecipesResponse(
        items=resolved_names,
        recipe_links=build_combo_recipe_links(keywords),
        all_staple=all_staple,
        suggestions=staple_only_suggestions() if all_staple else [],
    )


@app.get("/api/recipe-results", response_model=RecipeResultsResponse)
def get_recipe_results(items: str = "") -> RecipeResultsResponse:
    """Inline REAL search results (3 sources) with graceful per-source
    fallback. NEVER raises / always 200: an unresolved item list, missing
    API keys, or any upstream failure just yields fallback sources whose
    `more_url` is the existing deep-link (= the previous behaviour)."""
    names = [n.strip() for n in items.split(",") if n.strip()]

    resolved_names: list[str] = []
    for name in names:
        meta = find_ingredient(name)
        if meta is None:
            continue
        if meta.name not in resolved_names:
            resolved_names.append(meta.name)

    data = gather_recipe_results(resolved_names)
    return RecipeResultsResponse(**data)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_FRONTEND_DIR / "index.html")


# Static assets (style.css, app.js) served under /static.
app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")
