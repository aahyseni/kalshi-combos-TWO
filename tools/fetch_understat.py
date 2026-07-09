"""Fetch Understat player-scorer data into data/history/understat/ (raw cache).

Understat exposes clean JSON endpoints (discovered from js/league.min.js and
js/match.min.js, 2026-07-06):
  GET https://understat.com/getLeagueData/{league}/{season}
      -> {dates:[{id,isResult,h,a,goals,xG,forecast,...}], teams, players}
  GET https://understat.com/getMatchData/{match_id}
      -> {rosters:{h,a}, shots:{h,a}, tmpl}
      rosters[side] = {player_id: {goals, own_goals, xG, time, team_id, h_a,...}}

Responses are gzipped. Everything is cached to disk so re-runs are free and the
calibration script reads only local files (offline, reproducible).

Run: C:/.../.venv/Scripts/python.exe tools/fetch_understat.py [--seasons 2021 2022] [--leagues EPL La_liga ...]
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from pathlib import Path

import aiohttp

CACHE = Path(__file__).resolve().parents[1] / "data" / "history" / "understat"
BASE = "https://understat.com"
LEAGUES = ["EPL", "La_liga", "Bundesliga", "Serie_A", "Ligue_1"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Encoding": "gzip",
}


async def _get_json(session: aiohttp.ClientSession, url: str, retries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=40)) as r:
                raw = await r.read()
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} for {url}")
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(1.0 + attempt * 1.5)
    raise RuntimeError(f"failed {url}: {last}")


async def fetch_match(session, sem, match_id: str) -> str:
    path = CACHE / f"match_{match_id}.json"
    if path.exists() and path.stat().st_size > 50:
        return "cached"
    async with sem:
        data = await _get_json(session, f"{BASE}/getMatchData/{match_id}")
    path.write_text(json.dumps(data), encoding="utf-8")
    return "fetched"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", default=["2021", "2022"])
    ap.add_argument("--leagues", nargs="+", default=LEAGUES)
    ap.add_argument("--concurrency", type=int, default=12)
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(args.concurrency)
    async with aiohttp.ClientSession() as session:
        match_ids: list[str] = []
        for league in args.leagues:
            for season in args.seasons:
                lpath = CACHE / f"league_{league}_{season}.json"
                if lpath.exists() and lpath.stat().st_size > 50:
                    ld = json.loads(lpath.read_text(encoding="utf-8"))
                else:
                    ld = await _get_json(session, f"{BASE}/getLeagueData/{league}/{season}")
                    lpath.write_text(json.dumps(ld), encoding="utf-8")
                ids = [d["id"] for d in ld["dates"] if d.get("isResult")]
                match_ids.extend(ids)
                print(f"{league} {season}: {len(ids)} result matches")
        match_ids = list(dict.fromkeys(match_ids))
        print(f"total unique matches to ensure cached: {len(match_ids)}")

        done = 0
        fetched = 0
        # process in chunks so progress prints and we can be interrupted safely
        chunk = 200
        for i in range(0, len(match_ids), chunk):
            batch = match_ids[i : i + chunk]
            results = await asyncio.gather(*(fetch_match(session, sem, mid) for mid in batch))
            done += len(batch)
            fetched += sum(1 for r in results if r == "fetched")
            print(f"  progress {done}/{len(match_ids)} (newly fetched so far: {fetched})", flush=True)
        print(f"DONE. matches cached: {len(match_ids)}, newly fetched: {fetched}")


if __name__ == "__main__":
    asyncio.run(main())
