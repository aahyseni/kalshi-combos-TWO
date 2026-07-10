"""MVE eligibility drift scan — MLB family baseline guard.

Scans ALL Kalshi multivariate event collections (public endpoint, no auth) and
verifies that the MLB combo-eligibility surface still matches the baseline
established 2026-07-09/10 (docs/calibration/staged_mlb_props.md + the
2026-07-10 baseball scorecard report):

  - exactly two MLB-bearing collections: KXMVESPORTSMULTIGAMEEXTENDED-R and
    KXMVECROSSCATEGORY-R (the KXMVEMLB whitelist prefix matches ZERO live
    collections — dead);
  - member MLB families exactly the 9-family baseline
    {GAME, TOTAL, SPREAD, KS, HIT, HR, HRR, TB, RFI}.

Why it matters: our shipped classification/ρ-table/settlement audit covers
exactly these 9 families. If Kalshi adds a family to a combo collection
(TEAMTOTAL, F5*, RBI, SB, OUTS, EXTRAS, ...) it becomes quotable-in-combos
while our side has no keywords/ρ/settlement audit for it — UNKNOWN fail-safe
widens it, but the eligibility surface changed and must be re-audited.

Exit codes (fail-safe: ANY drift or ANY error is nonzero):
  0 — surface matches baseline exactly
  1 — collection-set drift only (new/renamed/missing MLB-bearing collection)
  2 — FAMILY drift (family added beyond baseline and/or baseline family gone)
  3 — scan failed (network/API error) — treat as "unknown", re-run

Membership counts roll daily (e.g. 373 MLB events on 2026-07-09, 173 on
2026-07-10 as games settle out) — counts are reported for context but are
NEVER a drift criterion; only the family set and the collection set are.

Run (repo root):  .venv/Scripts/python.exe tools/mvec_eligibility_scan.py
Cadence: monthly + before playoffs (rule changes cluster at season breaks).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
PAGE_LIMIT = 200
PACE_SECONDS = 0.8  # pagination pacing per repo convention
MAX_PAGES = 50  # hard stop against cursor loops (universe is ~7 pages today)
RETRIES = 3

MLB_SERIES_PREFIX = "KXMLB"

# The 9-family baseline (series ticker = KXMLB + family). Verified vs all
# 1,387 collections 2026-07-09 and re-verified live 2026-07-10.
BASELINE_FAMILIES = frozenset(
    {"GAME", "TOTAL", "SPREAD", "KS", "HIT", "HR", "HRR", "TB", "RFI"}
)

# The two MLB-bearing collections as of 2026-07-10. A third appearing (e.g. a
# live KXMVEMLB*) or one vanishing/renaming is collection-set drift: the
# whitelist and the eligibility audit were scoped to exactly these two.
EXPECTED_COLLECTIONS = frozenset(
    {"KXMVESPORTSMULTIGAMEEXTENDED-R", "KXMVECROSSCATEGORY-R"}
)

# Families whose appearance is the specific alarm this scan exists for.
KNOWN_ALARM_FAMILIES = ("TEAMTOTAL", "F5", "F3", "F7", "RBI", "SB", "OUTS", "EXTRAS")


def _get(path: str, params: str = "") -> dict:
    """GET a public endpoint with small retry; raises on persistent failure."""
    url = f"{BASE}{path}" + (f"?{params}" if params else "")
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "combomaker-mvec-scan"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            time.sleep(PACE_SECONDS * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {RETRIES} attempts: {last_err}")


def fetch_all_collections() -> list[dict]:
    """Paginate GET /multivariate_event_collections (public), paced."""
    collections: list[dict] = []
    cursor = ""
    for page in range(MAX_PAGES):
        params = f"limit={PAGE_LIMIT}" + (f"&cursor={cursor}" if cursor else "")
        data = _get("/multivariate_event_collections", params)
        batch = data.get("multivariate_contracts") or []
        collections.extend(batch)
        cursor = data.get("cursor") or ""
        if not cursor:
            return collections
        if page < MAX_PAGES - 1:
            time.sleep(PACE_SECONDS)
    raise RuntimeError(f"pagination did not terminate within {MAX_PAGES} pages")


def event_tickers(collection: dict) -> list[str]:
    """Member event tickers; prefers associated_events, falls back to deprecated field."""
    events = collection.get("associated_events") or []
    tickers = [e.get("ticker", "") for e in events if e.get("ticker")]
    if not tickers:
        tickers = [t for t in (collection.get("associated_event_tickers") or []) if t]
    return tickers


def mlb_family_counts(tickers: list[str]) -> dict[str, int]:
    """Map MLB family -> member-event count. Family = series segment minus KXMLB.

    Exact series-segment matching (split on first '-') keeps KXMLBHR and
    KXMLBHRR distinct.
    """
    counts: dict[str, int] = {}
    for ticker in tickers:
        series = ticker.split("-", 1)[0]
        if series.startswith(MLB_SERIES_PREFIX):
            family = series[len(MLB_SERIES_PREFIX):]
            counts[family] = counts.get(family, 0) + 1
    return counts


def main() -> int:
    try:
        collections = fetch_all_collections()
    except Exception as err:  # noqa: BLE001 — any scan failure must exit nonzero
        print("!!!! MVEC ELIGIBILITY SCAN FAILED (surface UNKNOWN -- re-run) !!!!")
        print(f"!!!! {err}")
        return 3

    bearing: dict[str, dict[str, int]] = {}
    for collection in collections:
        counts = mlb_family_counts(event_tickers(collection))
        if counts:
            bearing[collection["collection_ticker"]] = counts

    print(f"scanned {len(collections)} MVE collections (public API, {BASE})")
    print(f"MLB-bearing collections: {len(bearing)}")

    union: dict[str, int] = {}
    for ticker, counts in sorted(bearing.items()):
        fams = ", ".join(f"{f}={n}" for f, n in sorted(counts.items()))
        print(f"  {ticker}: {sum(counts.values())} MLB events | {fams}")
        for family, n in counts.items():
            union[family] = union.get(family, 0) + n

    added = set(union) - BASELINE_FAMILIES
    removed = BASELINE_FAMILIES - set(union)
    collection_drift = set(bearing) != EXPECTED_COLLECTIONS

    if added or removed:
        print("!" * 72)
        print("!!!! MVEC ELIGIBILITY DRIFT -- MLB FAMILY SET CHANGED !!!!")
        for family in sorted(added):
            tag = " (KNOWN ALARM FAMILY)" if family.startswith(KNOWN_ALARM_FAMILIES) else ""
            in_cols = [t for t, c in bearing.items() if family in c]
            print(
                f"!!!! ADDED beyond baseline: KXMLB{family}{tag} "
                f"(n={union[family]} events, in {', '.join(sorted(in_cols))})"
            )
        for family in sorted(removed):
            print(f"!!!! REMOVED from surface: KXMLB{family} (was baseline)")
        print("!!!! ACTION: re-audit classification/rho-table/settlement before any")
        print("!!!! MLB combo quoting; update staged_mlb_props.md + this baseline.")
        print("!" * 72)
        return 2

    if collection_drift:
        print("!" * 72)
        print("!!!! MVEC COLLECTION-SET DRIFT (families still = baseline) !!!!")
        for ticker in sorted(set(bearing) - EXPECTED_COLLECTIONS):
            print(f"!!!! NEW MLB-bearing collection: {ticker}")
        for ticker in sorted(EXPECTED_COLLECTIONS - set(bearing)):
            print(f"!!!! EXPECTED collection missing/renamed: {ticker}")
        print("!!!! ACTION: check collection whitelist coverage + update")
        print("!!!! EXPECTED_COLLECTIONS in this tool after review.")
        print("!" * 72)
        return 1

    print(
        "OK -- eligibility surface matches baseline: "
        f"{len(bearing)}/2 expected collections, families = "
        f"{{{', '.join(sorted(BASELINE_FAMILIES))}}}, no drift."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
