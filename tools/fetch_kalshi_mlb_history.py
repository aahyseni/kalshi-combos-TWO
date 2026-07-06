"""Build an MLB history dataset from Kalshi's OWN settled markets.

The freshest and best-fitting odds source is the venue itself: Kalshi has
run full MLB seasons since 2025. For every settled KXMLBGAME / KXMLBTOTAL
market we take the PRE-GAME price (last hourly candle mid before the start
time encoded in the ticker) and the settlement result, giving per-game
market-implied marginals + outcomes on exactly the prices we quote against.

Output: data/history/kalshi_mlb_history.csv with one row per game:
  game_code, team, p_team_close, team_won, total_line, p_over_close, went_over

Notes:
  - start time parsed from the ticker (DDMMMYY[HHMM], US/Eastern; the MLB
    season is EDT so a fixed -4h offset is used)
  - per game the MAIN total line = the one whose pre-game mid is closest to
    50c (max 6 lines probed)
  - throttled ~8 req/s; a full 2025+2026 pull is ~30-45 min — safe to re-run,
    it skips game codes already in the CSV

Run:  uv run python tools/fetch_kalshi_mlb_history.py
"""

from __future__ import annotations

import asyncio
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from combomaker.exchange.rest import KalshiApiError, KalshiRestClient

BASE = "https://external-api.kalshi.com/trade-api/v2"
OUT = Path(__file__).resolve().parent.parent / "data" / "history" / "kalshi_mlb_history.csv"
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
)}
_CODE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z0-9]+)$")
_THROTTLE_S = 0.125
_MAX_TOTAL_LINES = 6


def start_ts_of(game_code: str) -> int | None:
    m = _CODE.match(game_code)
    if m is None:
        return None
    yy, mon, dd, hhmm, _teams = m.groups()
    hour, minute = (13, 0) if hhmm is None else (int(hhmm[:2]), int(hhmm[2:]))
    try:
        est = datetime(
            2000 + int(yy), _MONTHS[mon], int(dd), hour, minute,
            tzinfo=timezone(timedelta(hours=-4)),  # MLB season == EDT
        )
    except (KeyError, ValueError):
        return None
    return int(est.timestamp())


async def pregame_mid(
    rest: KalshiRestClient, series: str, ticker: str, start_ts: int
) -> float | None:
    await asyncio.sleep(_THROTTLE_S)
    try:
        payload = await rest.get_candlesticks(
            series, ticker, start_ts=start_ts - 6 * 3600, end_ts=start_ts,
            period_interval=60,
        )
    except KalshiApiError:
        return None
    candles = payload.get("candlesticks") or []
    for candle in reversed(candles):
        # wire format: dollar STRINGS ("0.4100"), key suffix _dollars
        bid_raw = (candle.get("yes_bid") or {}).get("close_dollars")
        ask_raw = (candle.get("yes_ask") or {}).get("close_dollars")
        try:
            bid, ask = float(bid_raw), float(ask_raw)
        except (TypeError, ValueError):
            continue
        if 0.0 < bid < 1.0 and 0.0 < ask <= 1.0 and ask - bid < 0.15:
            return (bid + ask) / 2.0
    return None


async def list_settled(rest: KalshiRestClient, series: str) -> list[dict]:
    markets: list[dict] = []
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


async def main() -> None:
    done: set[str] = set()
    if OUT.exists():
        with open(OUT, encoding="utf-8", newline="") as f:  # noqa: ASYNC230 (one-shot tool)
            done = {row["game_code"] for row in csv.DictReader(f)}
    new_file = not OUT.exists()

    async with KalshiRestClient(BASE, None) as rest:
        games = await list_settled(rest, "KXMLBGAME")
        totals = await list_settled(rest, "KXMLBTOTAL")
        print(f"settled markets: {len(games)} game, {len(totals)} total")

        totals_by_game: dict[str, list[dict]] = {}
        for m in totals:
            parts = m.get("ticker", "").split("-")
            if len(parts) == 3 and m.get("result") in ("yes", "no"):
                totals_by_game.setdefault(parts[1], []).append(m)

        games_by_code: dict[str, dict] = {}
        for m in games:
            parts = m.get("ticker", "").split("-")
            if len(parts) == 3 and m.get("result") in ("yes", "no"):
                games_by_code.setdefault(parts[1], m)  # one team's market per game

        print(f"distinct games: {len(games_by_code)} ({len(done)} already fetched)")
        written = 0
        with open(OUT, "a", encoding="utf-8", newline="") as f:  # noqa: ASYNC230 (one-shot tool)
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
                p_team = await pregame_mid(rest, "KXMLBGAME", gm["ticker"], start_ts)
                if p_team is None:
                    continue
                best: tuple[float, float, float, str] | None = None
                for tm in totals_by_game[code][:_MAX_TOTAL_LINES]:
                    suffix = tm["ticker"].split("-")[-1]
                    if not re.fullmatch(r"\d+", suffix):
                        continue
                    p_over = await pregame_mid(rest, "KXMLBTOTAL", tm["ticker"], start_ts)
                    if p_over is None:
                        continue
                    line = int(suffix) - 0.5
                    if best is None or abs(p_over - 0.5) < abs(best[1] - 0.5):
                        best = (line, p_over, 0.0, tm["result"])
                if best is None:
                    continue
                writer.writerow(
                    [
                        code,
                        gm["ticker"].split("-")[-1],
                        f"{p_team:.4f}",
                        1 if gm["result"] == "yes" else 0,
                        best[0],
                        f"{best[1]:.4f}",
                        1 if best[3] == "yes" else 0,
                    ]
                )
                written += 1
                if written % 100 == 0:
                    f.flush()
                    print(f"  {written} games written")
        print(f"done: +{written} games -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
