"""KAMIS 농산물유통정보 Open API adapter.

All KAMIS-specific HTTP + JSON parsing is isolated in this module behind the
PriceRow / PriceResult contract. ranking.py and main.py never see raw KAMIS
shapes, so the parsing here can be tuned later against a real cert key without
touching the rest of the app.

KAMIS auth: two query params `p_cert_key` (cert_key) and `p_cert_id`
(cert_id), read from env KAMIS_CERT_KEY / KAMIS_CERT_ID. JSON requested via
`p_returntype=json`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Literal, Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger("jachwi.kamis")

KAMIS_BASE_URL = "https://www.kamis.or.kr/service/price/xml.do"
HTTP_TIMEOUT_SECONDS = 6.0
CACHE_TTL_SECONDS = 30 * 60  # 30 min — don't hammer KAMIS

_DATA_DIR = Path(__file__).parent / "data"
_SAMPLE_PRICES_PATH = _DATA_DIR / "sample_prices.json"

# Module-level in-memory cache for the live result only.
_cache_result: Optional["PriceResult"] = None
_cache_ts: float = 0.0


class PriceRow(BaseModel):
    item_name: str
    unit: str
    price: int
    prev_price: Optional[int] = None
    change_pct: Optional[float] = None  # negative = cheaper than before
    category: str


class PriceResult(BaseModel):
    source: Literal["live", "sample"]
    rows: list[PriceRow]


# ---------------------------------------------------------------------------
# KAMIS dailySalesList JSON — VERIFIED against a real cert key on 2026-05-18.
# ---------------------------------------------------------------------------
# Real top-level shape:
#   { "condition": [["YYYYMMDD"]],
#     "error_code": "000",                 # "000" = success; else auth/quota/no-data
#     "price": [ {row}, {row}, ... ] }     # rows live under "price" (NOT "data")
# Each row (relevant keys):
#   "product_cls_code" "01"=소매 / "02"=도매   -> keep RETAIL ("01") only
#   "item_name"  "쌀/20kg"  -> name + spec; we match on the part before "/"
#   "unit"       "20kg"
#   "dpr1"       당일 (today, comma'd: "62,242", may be "-"/"")
#   "dpr2"       1일전   "dpr3" 1개월전   "dpr4" 1년전
#   "category_name" "식량작물"
# "평소 대비" baseline = MEDIAN of the available values among {dpr2 (1일전),
# dpr3 (1개월전), dpr4 (1년전)}. See _usual_baseline() for the exact rule.
# This is an INTERIM approximation (a few sparse KAMIS reference points), all
# isolated in that one helper so it's a single-spot swap once a real cert_id
# unlocks a cached ~1-year average (monthlySalesList/periodProductList).
# ---------------------------------------------------------------------------
_FIELD_ITEM_NAME = "item_name"
_FIELD_UNIT = "unit"
_FIELD_TODAY_PRICE = "dpr1"
# The three reference points the interim "평소" baseline is the median of.
_FIELD_PREV_DAY = "dpr2"     # 1일전
_FIELD_PREV_MONTH = "dpr3"   # 1개월전
_FIELD_PREV_YEAR = "dpr4"    # 1년전
_FIELD_CATEGORY = "category_name"
_FIELD_PRODUCT_CLS = "product_cls_code"
_RETAIL_CLS = "01"


def _parse_price(raw: object) -> Optional[int]:
    """Parse a KAMIS price cell. Handles '3,500', '-', '', None, numbers."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-", "0"):
        return None
    try:
        return int(round(float(s)))
    except (ValueError, TypeError):
        return None


def _usual_baseline(raw: dict) -> Optional[int]:
    """Compute the interim "평소" baseline for one KAMIS row.

    INTERIM APPROXIMATION — the median of whatever is available among the
    three KAMIS reference points {1일전 (dpr2), 1개월전 (dpr3), 1년전 (dpr4)}:
      * 0 parseable values  -> None (no "평소 대비" signal)
      * 1 value             -> that value
      * 2 values            -> their mean
      * 3 values            -> the middle value (true median)

    This is the SINGLE place that decides the baseline. Once a real cert_id
    is available, swap the body here for a cached ~1-year average (KAMIS
    monthlySalesList / periodProductList) — nothing else needs to change.
    """
    vals = [
        v
        for v in (
            _parse_price(raw.get(_FIELD_PREV_DAY)),
            _parse_price(raw.get(_FIELD_PREV_MONTH)),
            _parse_price(raw.get(_FIELD_PREV_YEAR)),
        )
        if v is not None
    ]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    if n == 1:
        return vals[0]
    if n == 2:
        return int(round((vals[0] + vals[1]) / 2))
    return vals[1]


def _extract_rows(payload: object) -> list[dict]:
    """Pull the row dicts out of KAMIS JSON (verified shape: rows under "price").

    Non-"000" error_code (auth / quota / no-data) -> [] so the caller falls
    back to sample. Defensive against the older "data"/"item" shapes too.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        code = payload.get("error_code")
        if code not in (None, "", "000"):
            logger.warning("KAMIS error_code=%s (falling back to sample)", code)
            return []
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("price", "item", "items", "row", "list"):
                maybe = data.get(key)
                if isinstance(maybe, list):
                    return [r for r in maybe if isinstance(r, dict)]
                if isinstance(maybe, dict):
                    return [maybe]
    return []


def _row_from_kamis(raw: dict) -> Optional[PriceRow]:
    """Convert one KAMIS row into a PriceRow, or None if unusable.

    Keeps RETAIL (소매, "01") only; reduces "쌀/20kg" -> "쌀" for matching;
    "평소" baseline = median of available {dpr2,dpr3,dpr4} (_usual_baseline).
    """
    cls = str(raw.get(_FIELD_PRODUCT_CLS, "")).strip()
    if cls and cls != _RETAIL_CLS:
        return None

    category = str(raw.get(_FIELD_CATEGORY, "")).strip() or "기타"
    cat_code = str(raw.get("category_code", "")).strip()
    raw_name = str(raw.get(_FIELD_ITEM_NAME, "")).strip()
    # Livestock (축산물): the cut is AFTER "/" (e.g. "돼지/삼겹살",
    # "소/등심(1+등급)", "계란/특란10구(일반란)") — keep the FULL name so
    # cuts/grades stay distinct. Everything else reduces "쌀/20kg" -> "쌀".
    if cat_code == "500" or "축산" in category:
        name = raw_name
    else:
        name = raw_name.split("/")[0].strip()
    if not name:
        return None

    price = _parse_price(raw.get(_FIELD_TODAY_PRICE))
    if price is None:
        return None

    prev = _usual_baseline(raw)

    change_pct: Optional[float] = None
    if prev is not None and prev > 0:
        change_pct = round((price - prev) / prev * 100, 1)

    unit = str(raw.get(_FIELD_UNIT, "")).strip() or "단위 미상"

    return PriceRow(
        item_name=name,
        unit=unit,
        price=price,
        prev_price=prev,
        change_pct=change_pct,
        category=category,
    )


def _load_sample() -> PriceResult:
    with _SAMPLE_PRICES_PATH.open(encoding="utf-8") as f:
        raw_rows = json.load(f)
    rows = [PriceRow(**r) for r in raw_rows]
    return PriceResult(source="sample", rows=rows)


def _fetch_live(cert_key: str, cert_id: str) -> PriceResult:
    """Call KAMIS dailySalesList. Raises on any failure (caller handles)."""
    params = {
        "action": "dailySalesList",
        "p_cert_key": cert_key,
        "p_cert_id": cert_id,
        "p_returntype": "json",
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = client.get(KAMIS_BASE_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()

    raw_rows = _extract_rows(payload)
    rows: list[PriceRow] = []
    for raw in raw_rows:
        row = _row_from_kamis(raw)
        if row is not None:
            rows.append(row)

    if not rows:
        raise ValueError("KAMIS returned no usable rows")

    return PriceResult(source="live", rows=rows)


def get_today_prices() -> PriceResult:
    """Return today's prices: live KAMIS if keys present & working, else sample.

    Never raises — any KAMIS/HTTP/parse failure logs a warning and falls back
    to the bundled sample data so the request always succeeds.
    """
    global _cache_result, _cache_ts

    cert_key = (os.getenv("KAMIS_CERT_KEY") or "").strip()
    cert_id = (os.getenv("KAMIS_CERT_ID") or "").strip()

    if not cert_key or not cert_id:
        return _load_sample()

    # Serve cached live result if still fresh.
    now = time.time()
    if _cache_result is not None and (now - _cache_ts) < CACHE_TTL_SECONDS:
        return _cache_result

    try:
        result = _fetch_live(cert_key, cert_id)
        _cache_result = result
        _cache_ts = now
        return result
    except Exception as exc:  # noqa: BLE001 — never crash the request
        logger.warning("KAMIS live fetch failed (%s); falling back to sample", exc)
        return _load_sample()
