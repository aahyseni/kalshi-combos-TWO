"""Build per-sport history datasets from Kalshi's OWN settled markets.

The freshest and best-fitting odds source is the venue itself. For every
settled game/total/spread market we take the PRE-GAME price (last hourly
candle mid before the start time encoded in the ticker) and the settlement
result, giving per-game market-implied marginals + outcomes on exactly the
prices we quote against. Supersedes fetch_kalshi_mlb_history.py (MLB-only,
no spreads).

Outputs per sport (data/history/):
  kalshi_{sport}_history.csv — one row per game:
    game_code, team, p_team_close, team_won, total_line, p_over_close, went_over
  kalshi_{sport}_spreads.csv — one row per game (MAIN spread line):
    game_code, spread_team, spread_line, p_spread_close, covered

Notes:
  - spread semantics DOC-VERIFIED (market metadata): suffix TEAMn ==
    "TEAM wins by over n-0.5"; the line is taken from ``floor_strike``
    directly (strike_type=greater), no suffix parsing
  - per game the MAIN total/spread line = the one whose pre-game mid is
    closest to 50c, chosen by probing the WHOLE strike ladder (an earlier
    cap of 6 total / 8 smallest-strike spread lines structurally excluded
    the real main line — the favourite's spread and high-scoring totals sit
    well inside the ladder, not at its small-strike end)
  - start time parsed from the ticker (YYMMMDD[HHMM], US/Eastern; a fixed
    -4h offset biases EARLY year-round, so mids stay pre-game). Tickers
    without HHMM assume 13:00 ET — for evening NBA/WNBA games the mid is a
    few hours before close (noisier, unbiased); rare pre-13:00 tips may
    leak an in-game candle
  - the settled-market listing only exposes ~2 recent months, so run this
    periodically to grow the CSVs; safe to re-run, it skips game codes
    already present (per file)
  - throttled ~8 req/s

Run:  uv run python tools/fetch_kalshi_history.py [--sport mlb|wnba|nba|all]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from combomaker.exchange.rest import (
    KalshiApiError,
    KalshiRestClient,
    RateLimitedError,
)

BASE = "https://external-api.kalshi.com/trade-api/v2"
HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
SPORTS: dict[str, dict[str, str]] = {
    "mlb": {"game": "KXMLBGAME", "total": "KXMLBTOTAL", "spread": "KXMLBSPREAD"},
    "wnba": {"game": "KXWNBAGAME", "total": "KXWNBATOTAL", "spread": "KXWNBASPREAD"},
    "nba": {"game": "KXNBAGAME", "total": "KXNBATOTAL", "spread": "KXNBASPREAD"},
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
)}
_CODE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z0-9]+)$")
_SPREAD_SUFFIX = re.compile(r"^([A-Z]+)(\d+)$")
_THROTTLE_S = 0.125
_RETRY_ATTEMPTS = 4        # bounded retry on 429 before giving up on a rung
_RETRY_BACKOFF_S = 0.5


def start_ts_of(game_code: str) -> int | None:
    m = _CODE.match(game_code)
    if m is None:
        return None
    yy, mon, dd, hhmm, _teams = m.groups()
    hour, minute = (13, 0) if hhmm is None else (int(hhmm[:2]), int(hhmm[2:]))
    try:
        est = datetime(
            2000 + int(yy), _MONTHS[mon], int(dd), hour, minute,
            tzinfo=timezone(timedelta(hours=-4)),  # -4h biases early == pre-game safe
        )
    except (KeyError, ValueError):
        return None
    return int(est.timestamp())


async def pregame_mid(
    rest: KalshiRestClient, series: str, ticker: str, start_ts: int
) -> float | None:
    payload: dict[str, Any] | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        await asyncio.sleep(_THROTTLE_S)
        try:
            payload = await rest.get_candlesticks(
                series, ticker, start_ts=start_ts - 6 * 3600, end_ts=start_ts,
                period_interval=60,
            )
            break
        except RateLimitedError:
            # Transient rate limit: back off and retry so a 429 is not
            # silently recorded as "no candle" (which, combined with the
            # full-ladder probe, could otherwise crown a wrong main line).
            if attempt == _RETRY_ATTEMPTS - 1:
                return None
            await asyncio.sleep(_RETRY_BACKOFF_S * (2 ** attempt))
        except KalshiApiError:
            return None
    if payload is None:
        return None
    candles = payload.get("candlesticks") or []
    for candle in reversed(candles):
        # wire format: dollar STRINGS ("0.4100"), key suffix _dollars
        bid_raw = (candle.get("yes_bid") or {}).get("close_dollars")
        ask_raw = (candle.get("yes_ask") or {}).get("close_dollars")
        if not isinstance(bid_raw, str) or not isinstance(ask_raw, str):
            continue
        try:
            bid, ask = float(bid_raw), float(ask_raw)
        except ValueError:
            continue
        if 0.0 < bid < 1.0 and 0.0 < ask <= 1.0 and ask - bid < 0.15:
            return (bid + ask) / 2.0
    return None


async def list_settled(rest: KalshiRestClient, series: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        await asyncio.sleep(_THROTTLE_S)
        params: dict[str, str | int] = {
            "series_ticker": series, "status": "settled", "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        payload = await rest.get_markets(**params)
        markets.extend(payload.get("markets") or [])
        cursor = payload.get("cursor")
        if not cursor:
            return markets


def done_codes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path, encoding="utf-8", newline="") as f:
        return {row["game_code"] for row in csv.DictReader(f)}


def by_game_code(markets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        parts = m.get("ticker", "").split("-")
        if len(parts) == 3 and m.get("result") in ("yes", "no"):
            grouped.setdefault(parts[1], []).append(m)
    return grouped


async def best_mid(
    rest: KalshiRestClient, series: str, candidates: list[dict[str, Any]], start_ts: int
) -> tuple[dict[str, Any], float] | None:
    """The market whose pre-game mid is closest to 50c (the MAIN line)."""
    best: tuple[dict[str, Any], float] | None = None
    for m in candidates:
        mid = await pregame_mid(rest, series, m["ticker"], start_ts)
        if mid is None:
            continue
        if best is None or abs(mid - 0.5) < abs(best[1] - 0.5):
            best = (m, mid)
    return best


async def fetch_main(rest: KalshiRestClient, sport: str) -> None:
    series = SPORTS[sport]
    out = HISTORY / f"kalshi_{sport}_history.csv"
    done = done_codes(out)
    new_file = not out.exists()

    games = await list_settled(rest, series["game"])
    totals = await list_settled(rest, series["total"])
    games_by_code = {c: ms[0] for c, ms in by_game_code(games).items()}
    totals_by_game = by_game_code(totals)
    print(f"[{sport}] distinct games: {len(games_by_code)} ({len(done)} already fetched)")

    written = 0
    with open(out, "a", encoding="utf-8", newline="") as f:  # noqa: ASYNC230 (one-shot tool)
        writer = csv.writer(f)
        if new_file:
            writer.writerow(
                ["game_code", "team", "p_team_close", "team_won",
                 "total_line", "p_over_close", "went_over"]
            )
        for code, gm in games_by_code.items():
            if code in done or code not in totals_by_game:
                continue
            start_ts = start_ts_of(code)
            if start_ts is None:
                continue
            p_team = await pregame_mid(rest, series["game"], gm["ticker"], start_ts)
            if p_team is None:
                continue
            lined = sorted(
                (
                    tm for tm in totals_by_game[code]
                    if isinstance(tm.get("floor_strike"), (int, float))
                ),
                key=lambda tm: float(tm["floor_strike"]),
            )
            best = await best_mid(rest, series["total"], lined, start_ts)
            if best is None:
                continue
            tm, p_over = best
            writer.writerow(
                [
                    code,
                    gm["ticker"].split("-")[-1],
                    f"{p_team:.4f}",
                    1 if gm["result"] == "yes" else 0,
                    float(tm["floor_strike"]),
                    f"{p_over:.4f}",
                    1 if tm["result"] == "yes" else 0,
                ]
            )
            written += 1
            if written % 100 == 0:
                f.flush()
                print(f"[{sport}]   {written} games written")
    print(f"[{sport}] main done: +{written} games -> {out}")


async def fetch_spreads(rest: KalshiRestClient, sport: str) -> None:
    series = SPORTS[sport]
    out = HISTORY / f"kalshi_{sport}_spreads.csv"
    done = done_codes(out)
    new_file = not out.exists()

    spreads_by_game = by_game_code(await list_settled(rest, series["spread"]))
    print(f"[{sport}] games with settled spreads: {len(spreads_by_game)} "
          f"({len(done)} already fetched)")

    written = 0
    with open(out, "a", encoding="utf-8", newline="") as f:  # noqa: ASYNC230 (one-shot tool)
        writer = csv.writer(f)
        if new_file:
            writer.writerow(
                ["game_code", "spread_team", "spread_line", "p_spread_close", "covered"]
            )
        for code, markets in spreads_by_game.items():
            if code in done:
                continue
            start_ts = start_ts_of(code)
            if start_ts is None:
                continue
            lined = sorted(
                (
                    m for m in markets
                    if isinstance(m.get("floor_strike"), (int, float))
                    and _SPREAD_SUFFIX.match(m["ticker"].split("-")[-1])
                ),
                key=lambda m: float(m["floor_strike"]),
            )
            best = await best_mid(rest, series["spread"], lined, start_ts)
            if best is None:
                continue
            sm, p_spread = best
            suffix_m = _SPREAD_SUFFIX.match(sm["ticker"].split("-")[-1])
            assert suffix_m is not None  # filtered above
            writer.writerow(
                [
                    code,
                    suffix_m.group(1),
                    float(sm["floor_strike"]),
                    f"{p_spread:.4f}",
                    1 if sm["result"] == "yes" else 0,
                ]
            )
            written += 1
            if written % 100 == 0:
                f.flush()
                print(f"[{sport}]   {written} spreads written")
    print(f"[{sport}] spreads done: +{written} games -> {out}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=[*SPORTS, "all"], default="all")
    args = parser.parse_args()
    sports = list(SPORTS) if args.sport == "all" else [args.sport]

    async with KalshiRestClient(BASE, None) as rest:
        for sport in sports:
            await fetch_main(rest, sport)
            await fetch_spreads(rest, sport)


if __name__ == "__main__":
    asyncio.run(main())
