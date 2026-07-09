"""Fetch StatsBomb OPEN DATA for INTERNATIONAL / World Cup soccer into a local
cache (data/history/statsbomb/), as compact per-match player-scorer extracts.

StatsBomb open data (free, CC-BY): https://github.com/statsbomb/open-data
  GET .../data/competitions.json          -> [{competition_id, season_id, ...}]
  GET .../data/matches/{comp}/{season}.json -> [{match_id, home_team, away_team,
                                                 home_score, away_score, ...}]
  GET .../data/events/{match_id}.json      -> event list (Shot events carry
                                              shot.statsbomb_xg + shot.outcome)
  GET .../data/lineups/{match_id}.json     -> per-team lineup + minutes played

Per match we extract, for every player who played: team, side, sum xG over
their shots, goals (shots with outcome == "Goal" -> OWN GOALS EXCLUDED, since
own goals are separate event types), minutes. Match-level totals/result/BTTS
come from the match json's home_score/away_score (which DO include own goals).

Compact extracts (not raw events) are cached so re-runs are free and disk stays
small; the raw events JSON can be 1-3 MB each and is discarded after extraction.

Run: C:/.../.venv/Scripts/python.exe tools/fetch_statsbomb.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp

CACHE = Path(__file__).resolve().parents[1] / "data" / "history" / "statsbomb"
BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
HEADERS = {"User-Agent": "Mozilla/5.0 (research; kalshi-combos-TWO calibration)"}

# (competition_id, season_id, label, tournament_class)
MEN_SENIOR = [
    (43, 106, "FIFA World Cup 2022", "WC_MEN"),
    (43, 3, "FIFA World Cup 2018", "WC_MEN"),
    (55, 282, "UEFA Euro 2024", "EURO_MEN"),
    (55, 43, "UEFA Euro 2020", "EURO_MEN"),
    (223, 282, "Copa America 2024", "COPA_MEN"),
    (1267, 107, "AFCON 2023", "AFCON_MEN"),
]
WOMEN = [
    (72, 107, "Women's World Cup 2023", "WC_WOMEN"),
    (72, 30, "Women's World Cup 2019", "WC_WOMEN"),
]
TARGETS = MEN_SENIOR + WOMEN


async def _get_json(session: aiohttp.ClientSession, url: str, retries: int = 5) -> object:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with session.get(
                url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=90)
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} for {url}")
                return json.loads(await r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(1.0 + attempt * 1.5)
    raise RuntimeError(f"failed {url}: {last}")


def _mmss_to_min(s: str | None) -> float | None:
    if not s or ":" not in s:
        return None
    mm, ss = s.split(":")[:2]
    try:
        return int(mm) + int(ss) / 60.0
    except ValueError:
        return None


def extract(match: dict, events: list, lineups: list) -> dict:
    home = match["home_team"]["home_team_name"]
    away = match["away_team"]["away_team_name"]
    hs = int(match["home_score"])
    as_ = int(match["away_score"])

    match_end = 90.0
    for e in events:
        m = e.get("minute")
        if isinstance(m, int) and m > match_end:
            match_end = float(m)

    # played set + minutes from lineups
    played: dict[str, dict] = {}
    for team in lineups:
        tname = team["team_name"]
        for pl in team["lineup"]:
            pid = pl["player_id"]
            positions = pl.get("positions") or []
            if not positions:
                continue  # named in squad but did not play
            mins = 0.0
            for seg in positions:
                frm = _mmss_to_min(seg.get("from")) or 0.0
                to = _mmss_to_min(seg.get("to"))
                if to is None:
                    to = match_end
                mins += max(0.0, to - frm)
            played[str(pid)] = {
                "player": pl.get("player_nickname") or pl["player_name"],
                "team": tname,
                "is_home": tname == home,
                "xg": 0.0,
                "goals": 0,
                "minutes": round(mins, 1),
            }

    xg_home = xg_away = 0.0
    for e in events:
        if e.get("type", {}).get("name") != "Shot":
            continue
        shot = e.get("shot", {})
        xg = float(shot.get("statsbomb_xg", 0.0) or 0.0)
        pid = str(e.get("player", {}).get("id", ""))
        tname = e.get("team", {}).get("name", "")
        is_goal = shot.get("outcome", {}).get("name") == "Goal"
        if tname == home:
            xg_home += xg
        elif tname == away:
            xg_away += xg
        rec = played.get(pid)
        if rec is None:
            # shooter not in played dict (rare data gap): synthesize
            rec = {
                "player": e.get("player", {}).get("name", "?"),
                "team": tname,
                "is_home": tname == home,
                "xg": 0.0,
                "goals": 0,
                "minutes": match_end,
            }
            played[pid] = rec
        rec["xg"] += xg
        if is_goal:
            rec["goals"] += 1

    total = hs + as_
    return {
        "match_id": match["match_id"],
        "home_team": home,
        "away_team": away,
        "home_score": hs,
        "away_score": as_,
        "total": total,
        "home_win": hs > as_,
        "away_win": as_ > hs,
        "btts": hs >= 1 and as_ >= 1,
        "over25": total >= 3,
        "over35": total >= 4,
        "xg_home": round(xg_home, 4),
        "xg_away": round(xg_away, 4),
        "players": [
            {"player": r["player"], "team": r["team"], "is_home": r["is_home"],
             "xg": round(r["xg"], 4), "goals": r["goals"], "minutes": r["minutes"]}
            for r in played.values()
        ],
    }


async def fetch_match(session, sem, match: dict, comp: int, season: int,
                      label: str, tclass: str) -> str:
    mid = match["match_id"]
    path = CACHE / f"sb_match_{mid}.json"
    if path.exists() and path.stat().st_size > 50:
        return "cached"
    async with sem:
        events = await _get_json(session, f"{BASE}/events/{mid}.json")
        lineups = await _get_json(session, f"{BASE}/lineups/{mid}.json")
    rec = extract(match, events, lineups)  # type: ignore[arg-type]
    rec["comp_id"] = comp
    rec["season_id"] = season
    rec["comp_label"] = label
    rec["tournament_class"] = tclass
    path.write_text(json.dumps(rec), encoding="utf-8")
    return "fetched"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(args.concurrency)
    async with aiohttp.ClientSession() as session:
        # competitions.json for provenance
        comp_path = CACHE / "competitions.json"
        if not comp_path.exists():
            comps = await _get_json(session, f"{BASE}/competitions.json")
            comp_path.write_text(json.dumps(comps), encoding="utf-8")

        total_fetched = 0
        for comp, season, label, tclass in TARGETS:
            mpath = CACHE / f"matches_{comp}_{season}.json"
            if mpath.exists() and mpath.stat().st_size > 50:
                matches = json.loads(mpath.read_text(encoding="utf-8"))
            else:
                matches = await _get_json(session, f"{BASE}/matches/{comp}/{season}.json")
                mpath.write_text(json.dumps(matches), encoding="utf-8")
            results = await asyncio.gather(
                *(fetch_match(session, sem, m, comp, season, label, tclass) for m in matches)
            )
            fetched = sum(1 for r in results if r == "fetched")
            total_fetched += fetched
            print(f"{label:26} comp={comp} season={season}: "
                  f"{len(matches)} matches ({fetched} newly fetched)", flush=True)
        print(f"DONE. total newly fetched: {total_fetched}")


if __name__ == "__main__":
    asyncio.run(main())
