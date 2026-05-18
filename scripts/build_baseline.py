"""Build backend/data/baseline.json — the TRUE "작년 평균" price baseline.

Standalone, offline-testable, idempotent batch script. NEVER imported or
called by the web app at request time. Run it on a schedule (or by hand);
it hits KAMIS monthlySalesList (RETAIL) per curated ingredient, parses the
last complete calendar year's `yearavg`, validates the response is retail,
and writes whatever succeeded. KAMIS connectivity from this env is flaky,
so every call retries and partial output is expected and fine.

Usage (from the project root or anywhere — paths resolve to the repo):
    python scripts/build_baseline.py            # build baseline.json
    python scripts/build_baseline.py --selftest # offline parse asserts only

The two core pure functions — parse_monthly_sales() and
resolve_ingredient_codes() — take no network and are asserted by
--selftest against the captured scripts/fixtures/monthly_sales_sample.json
fixture.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# --- Path resolution: the repo root is this file's parent's parent. The
#     script may be launched from anywhere; never trust cwd. ---------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_DATA_DIR = _REPO_ROOT / "backend" / "data"
_FIXTURES_DIR = _SCRIPT_DIR / "fixtures"

_PRODUCTINFO_PATH = _FIXTURES_DIR / "kamis_productinfo.json"
_ALIAS_PATH = _DATA_DIR / "kamis_alias.json"
_INGREDIENTS_PATH = _DATA_DIR / "ingredients.json"
_BASELINE_OUT_PATH = _DATA_DIR / "baseline.json"
_Q2_FIXTURE_PATH = _FIXTURES_DIR / "monthly_sales_sample.json"

KAMIS_BASE_URL = "https://www.kamis.or.kr/service/price/xml.do"
RETAIL_CLS = "01"  # p_productclscode 01 = 소매(retail); 02 = 도매(wholesale)

# Flaky-network tolerance. Per-request timeout is generous; we retry the
# whole request a few times with a short sleep between attempts.
HTTP_TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 4
RETRY_SLEEP_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Pure parsing helpers (no network) — unit-testable against the fixture.
# ---------------------------------------------------------------------------
def _parse_price(raw: object) -> Optional[int]:
    """Parse a KAMIS price/avg cell. Handles '49,693', '-', '', None, nums.

    Mirrors backend/kamis.py:_parse_price so behaviour stays consistent.
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-", "0"):
        return None
    try:
        return int(round(float(s)))
    except (ValueError, TypeError):
        return None


def parse_monthly_sales(
    payload: object,
    target_year: int,
) -> Optional[tuple[int, str, str]]:
    """Parse a monthlySalesList JSON payload.

    Returns (yearavg:int, year:str, productclscode:str) for the best row, or
    None if the payload is unusable (error, empty, no parseable yearavg).

    Year selection: prefer the row whose ``yyyy == str(target_year)``; else
    the row with the max ``yyyy <= target_year``. ``price.item`` may be a
    list (multi-year) OR a single dict — both are handled.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("error_code") not in (None, "", "000"):
        return None

    price = payload.get("price")
    if not isinstance(price, dict):
        return None

    productclscode = str(price.get("productclscode", "")).strip()

    item = price.get("item")
    if isinstance(item, dict):
        rows = [item]
    elif isinstance(item, list):
        rows = [r for r in item if isinstance(r, dict)]
    else:
        return None
    if not rows:
        return None

    # Build {year:int -> (year:str, yearavg:int)} for rows with a usable avg.
    by_year: dict[int, tuple[str, int]] = {}
    for r in rows:
        yyyy_raw = r.get("yyyy")
        if yyyy_raw is None:
            continue
        yyyy_str = str(yyyy_raw).strip()
        try:
            yyyy_int = int(yyyy_str)
        except (ValueError, TypeError):
            continue
        avg = _parse_price(r.get("yearavg"))
        if avg is None:
            continue
        by_year[yyyy_int] = (yyyy_str, avg)

    if not by_year:
        return None

    if target_year in by_year:
        chosen_year = target_year
    else:
        candidates = [y for y in by_year if y <= target_year]
        if not candidates:
            return None
        chosen_year = max(candidates)

    year_str, yearavg = by_year[chosen_year]
    return yearavg, year_str, productclscode


# ---------------------------------------------------------------------------
# Curated ingredient -> KAMIS code resolution (no network).
# ---------------------------------------------------------------------------
def _non_empty(val: object) -> bool:
    """KAMIS productinfo uses [] (empty list) or '' for 'no value'."""
    if val is None:
        return False
    if isinstance(val, (list, dict)):
        return len(val) > 0
    return str(val).strip() != ""


def _pick_kind_row(rows: list[dict]) -> Optional[dict]:
    """From productinfo rows sharing an itemname, pick a representative kind.

    Prefer a kind that actually has a retail unit (retail_unitsize or
    retail_unit non-empty). Among those (or, if none, among all) prefer
    kindcode '00', otherwise take the first.
    """
    if not rows:
        return None

    def has_retail(r: dict) -> bool:
        return _non_empty(r.get("retail_unitsize")) or _non_empty(
            r.get("retail_unit")
        )

    retail_rows = [r for r in rows if has_retail(r)]
    pool = retail_rows or rows

    for r in pool:
        if str(r.get("kindcode", "")).strip() == "00":
            return r
    return pool[0]


def resolve_ingredient_codes(
    curated_name: str,
    alias_map: dict[str, list[str]],
    productinfo: list[dict],
) -> Optional[tuple[str, str, str]]:
    """Resolve a curated ingredient name to KAMIS (cat, item, kind) codes.

    Looks up the curated name's KAMIS itemname aliases, finds matching
    productinfo rows by EXACT itemname, and picks a representative kind.
    Returns (itemcategorycode, itemcode, kindcode) or None if unresolvable
    (no aliases, or no productinfo match — e.g. livestock / 김치 / 두부).
    """
    aliases = alias_map.get(curated_name) or []
    for alias in aliases:
        alias_s = str(alias).strip()
        if not alias_s:
            continue
        rows = [
            r
            for r in productinfo
            if str(r.get("itemname", "")).strip() == alias_s
        ]
        chosen = _pick_kind_row(rows)
        if chosen is None:
            continue
        cat = str(chosen.get("itemcategorycode", "")).strip()
        item = str(chosen.get("itemcode", "")).strip()
        kind = str(chosen.get("kindcode", "")).strip()
        if cat and item and kind:
            return cat, item, kind
    return None


# ---------------------------------------------------------------------------
# I/O helpers.
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_alias_map() -> dict[str, list[str]]:
    raw = _load_json(_ALIAS_PATH)
    return {
        k: [str(a).strip() for a in v]
        for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, list)
    }


def _load_productinfo() -> list[dict]:
    raw = _load_json(_PRODUCTINFO_PATH)
    if isinstance(raw, dict):
        info = raw.get("info", [])
    else:
        info = raw
    return [r for r in info if isinstance(r, dict)]


def _load_curated_names() -> list[str]:
    raw = _load_json(_INGREDIENTS_PATH)
    return [str(r["name"]).strip() for r in raw if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Network: one ingredient -> validated retail yearavg (with retry).
# ---------------------------------------------------------------------------
def _request_monthly(
    httpx_mod,
    cat: str,
    item: str,
    kind: str,
    year: int,
    cert_key: str,
    cert_id: str,
    *,
    with_graderank: bool,
) -> Optional[dict]:
    """One monthlySalesList HTTP attempt. Returns parsed JSON or None.

    None on any failure (non-200, empty/blank body, non-JSON). The caller
    decides retry/fallback. error_code is checked by parse_monthly_sales().
    """
    params = {
        "action": "monthlySalesList",
        "p_yyyy": str(year),
        "p_period": "1",
        "p_productclscode": RETAIL_CLS,
        "p_itemcategorycode": cat,
        "p_itemcode": item,
        "p_kindcode": kind,
        "p_convert_kg_yn": "N",
        "p_cert_key": cert_key,
        "p_cert_id": cert_id,
        "p_returntype": "json",
    }
    if with_graderank:
        params["p_graderank"] = "2"  # 상품

    try:
        with httpx_mod.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(KAMIS_BASE_URL, params=params)
        if resp.status_code != 200:
            return None
        body = (resp.text or "").strip()
        if not body:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001 — every failure is just "retry/skip"
        return None


def fetch_retail_yearavg(
    httpx_mod,
    cat: str,
    item: str,
    kind: str,
    year: int,
    cert_key: str,
    cert_id: str,
) -> Optional[tuple[int, str]]:
    """Fetch + validate the last-complete-year RETAIL yearavg for one item.

    Retries up to MAX_ATTEMPTS on flaky failures. If the call returns OK but
    zero usable rows, retries once WITHOUT p_graderank. Validates the parsed
    productclscode == '01' (retail) — a wholesale response is rejected so we
    never silently use 도매. Returns (yearavg, year_str) or None.
    """
    for graderank in (True, False):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            payload = _request_monthly(
                httpx_mod,
                cat,
                item,
                kind,
                year,
                cert_key,
                cert_id,
                with_graderank=graderank,
            )
            if payload is not None:
                parsed = parse_monthly_sales(payload, year)
                if parsed is not None:
                    yearavg, year_str, cls = parsed
                    if cls == RETAIL_CLS:
                        return yearavg, year_str
                    # Wholesale or unexpected cls -> not usable as retail.
                    # No point retrying the same params; try w/o graderank.
                    break
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_SLEEP_SECONDS)
        # graderank=True exhausted with no usable rows -> retry w/o it once.
    return None


# ---------------------------------------------------------------------------
# Selftest (offline, deterministic — MUST pass regardless of network).
# ---------------------------------------------------------------------------
def _selftest() -> int:
    payload = _load_json(_Q2_FIXTURE_PATH)

    parsed = parse_monthly_sales(payload, 2025)
    assert parsed is not None, "parse_monthly_sales(fixture, 2025) -> None"
    yearavg, year, cls = parsed
    assert yearavg == 49693, f"yearavg expected 49693, got {yearavg}"
    assert year == "2025", f"year expected '2025', got {year!r}"
    assert cls == "02", f"productclscode expected '02', got {cls!r}"

    # Fallback year selection: ask for 2099 -> picks max yyyy <= 2099 (2025).
    p2 = parse_monthly_sales(payload, 2099)
    assert p2 is not None and p2[1] == "2025", f"fallback-year picked {p2}"
    # Exact older year still selectable.
    p3 = parse_monthly_sales(payload, 2023)
    assert p3 is not None and p3[0] == 45215 and p3[1] == "2023", (
        f"2023 row expected (45215,'2023'), got {p3}"
    )

    # Single-dict price.item is tolerated.
    single = {
        "error_code": "000",
        "price": {
            "productclscode": "01",
            "item": {"yyyy": "2025", "yearavg": "1,234"},
        },
    }
    ps = parse_monthly_sales(single, 2025)
    assert ps == (1234, "2025", "01"), f"single-dict item parse: {ps}"

    # error_code != 000 -> None.
    assert parse_monthly_sales({"error_code": "900"}, 2025) is None
    # Empty / wrong shapes -> None (never raise).
    assert parse_monthly_sales({}, 2025) is None
    assert parse_monthly_sales([], 2025) is None
    assert parse_monthly_sales({"price": {"item": []}}, 2025) is None

    # Resolution against the real code table.
    alias_map = _load_alias_map()
    productinfo = _load_productinfo()

    rice = resolve_ingredient_codes("쌀", alias_map, productinfo)
    assert rice == ("100", "111", "01"), f"쌀 resolve expected 100/111/01, got {rice}"

    # 양배추 itemname '양배추' kind '00' has retail unit.
    cabbage = resolve_ingredient_codes("양배추", alias_map, productinfo)
    assert cabbage == ("200", "212", "00"), f"양배추 resolve got {cabbage}"

    # 당근 -> itemname '당근', kind '00' (retail 1kg) preferred.
    carrot = resolve_ingredient_codes("당근", alias_map, productinfo)
    assert carrot == ("200", "232", "00"), f"당근 resolve got {carrot}"

    # Empty-alias ingredients never resolve (김치/두부/떡국떡/라면사리).
    for empty in ("김치", "두부", "떡국떡", "라면사리"):
        assert (
            resolve_ingredient_codes(empty, alias_map, productinfo) is None
        ), f"{empty} should NOT resolve (empty alias)"

    # Livestock not in productinfo -> no resolution (best-effort, no block).
    assert resolve_ingredient_codes("계란", alias_map, productinfo) is None

    print("selftest: OK (parse fixture -> 49693 / 2025 / 02; resolution OK)")
    return 0


# ---------------------------------------------------------------------------
# Main builder.
# ---------------------------------------------------------------------------
def _build() -> int:
    # Lazy imports so --selftest needs no network deps installed.
    import httpx
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
    import os

    cert_key = (os.getenv("KAMIS_CERT_KEY") or "").strip()
    cert_id = (os.getenv("KAMIS_CERT_ID") or "").strip()
    if not cert_key or not cert_id:
        print("baseline: KAMIS_CERT_KEY/ID missing in .env — nothing to do.")
        # Still emit an (empty) file so the app's loader has a stable shape.
        _BASELINE_OUT_PATH.write_text("{}\n", encoding="utf-8")
        return 1

    target_year = datetime.now().year - 1  # last complete calendar year

    alias_map = _load_alias_map()
    productinfo = _load_productinfo()
    curated_names = _load_curated_names()

    baseline: dict[str, dict] = {}
    attempted = 0
    skipped_no_codes: list[str] = []
    failed_net_or_validate: list[str] = []

    for name in curated_names:
        codes = resolve_ingredient_codes(name, alias_map, productinfo)
        if codes is None:
            skipped_no_codes.append(name)
            continue
        cat, item, kind = codes
        attempted += 1
        result = fetch_retail_yearavg(
            httpx, cat, item, kind, target_year, cert_key, cert_id
        )
        if result is None:
            failed_net_or_validate.append(name)
            print(f"  - {name}: no valid retail yearavg (net/validate) - fallback")
            continue
        yearavg, year_str = result
        baseline[name] = {
            "yearly_avg": yearavg,
            "year": year_str,
            "src": "monthly-retail",
        }
        print(f"  + {name}: {yearavg:,} ({year_str}) retail")

    # Always write whatever succeeded (partial is fine & expected).
    _BASELINE_OUT_PATH.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    filled = len(baseline)
    skipped = len(skipped_no_codes)
    print(
        f"baseline: filled {filled} / attempted {attempted} / "
        f"skipped {skipped} (no codes) -> {_BASELINE_OUT_PATH}"
    )
    if skipped_no_codes:
        print(f"  skipped (no KAMIS codes / livestock): {skipped_no_codes}")
    if failed_net_or_validate:
        print(
            f"  fell back to median (net flaky / not retail): "
            f"{failed_net_or_validate}"
        )
    return 0


def _make_stdout_robust() -> None:
    """Windows consoles default to cp949 here, which can't encode em-dash,
    arrows, or some Hangul — a bare print() would then crash the run. Force
    UTF-8 with errors='replace' so output is best-effort and NEVER fatal."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except Exception:  # noqa: BLE001 — non-fatal; fall back to as-is
            pass


def main(argv: list[str]) -> int:
    _make_stdout_robust()
    if "--selftest" in argv:
        return _selftest()
    return _build()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
