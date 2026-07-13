"""B stage 4 — split the graded soccer universe by SAME-GAME vs MULTI-GAME.

Motivation: the go-live bot quotes KXMVESPORTSMULTIGAMEEXTENDED parlays (legs span
>1 game). The re-grade (03) proved a +5.8pp Soccer-FAT seller edge but pooled ALL
combos. This asks the question that matters for what we're LIVE-quoting: does the
edge live in same-game SGPs, multi-game exotic parlays, or both?

Reuses 03_grade_sweep's EXACT method + input (graded_settled.csv), the same
FAT_CUT_CC=200, the same match-day-clustered block bootstrap, and the same
fair-INDEPENDENT reality test (implied_hit vs actual_hit). The ONLY added dimension
is game-count per combo. Keep in sync with 03_grade_sweep.py (method is identical).

No live-module touch, no P&L refit — this measures.
"""
import csv
import os
import re
import sys
from collections import Counter

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
TMP = r"C:\Users\aahys\.claude\jobs\24844262\tmp"
IN = os.path.join(TMP, "graded_settled.csv")
RNG = np.random.default_rng(20260713)          # same seed as 03
FAT_CUT_CC = 200                                # room > 2c = FAT (same as 03)
DAY_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})")
GAME_RE = re.compile(r"\d{2}[A-Z]{3}\d{2}[A-Z]{6}")   # date + two 3-letter teams


def log(m=""):
    print(m, flush=True)


def match_day(leg_tickers: str) -> str:
    days = DAY_RE.findall(leg_tickers or "")
    return min(days) if days else "unknown"


def n_games(leg_tickers: str) -> int:
    return len(set(GAME_RE.findall(leg_tickers or "")))


def load():
    rows = []
    with open(IN, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["resolved"] != "1" or r["sport"] != "soccer":
                continue
            ng = n_games(r["leg_tickers"])
            rows.append({
                "n_legs": int(r["n_legs"]) if r["n_legs"] else 0,
                "fair": float(r["our_fair_cc"]) / 10000.0,
                "clearing": float(r["clearing_cc"]) / 10000.0,
                "room_cc": int(r["room_cc"]),
                "yes": int(r["combo_yes"]),
                "day": match_day(r["leg_tickers"]),
                "gclass": "same" if ng == 1 else "multi",
                "n_games": ng,
            })
    return rows


def clustered_boot(vals, days, stat, n=2000):
    vals = np.asarray(vals, float)
    days = np.asarray(days)
    uniq = np.unique(days)
    idx_by_day = {d: np.where(days == d)[0] for d in uniq}
    out = []
    for _ in range(n):
        pick = RNG.choice(uniq, size=len(uniq), replace=True)
        sel = np.concatenate([idx_by_day[d] for d in pick])
        out.append(stat(vals[sel]))
    return np.percentile(out, [5, 95])


def reality_test(rows, tier, gclass):
    sub = [r for r in rows
           if r["gclass"] == gclass
           and (tier == "ALL" or (tier == "FAT") == (r["room_cc"] > FAT_CUT_CC))]
    if len(sub) < 30:
        log(f"[reality] soccer/{tier}/{gclass}: n={len(sub)} <30, skip")
        return
    days = np.array([r["day"] for r in sub])
    implied = np.array([r["clearing"] for r in sub])
    actual = np.array([r["yes"] for r in sub], float)
    fairm = np.array([r["fair"] for r in sub])
    over = implied - actual
    lo, hi = clustered_boot(over, days, np.mean)
    verdict = ("REAL EDGE" if lo > 0 else "straddles 0 (not distinguishable)")
    log(f"[reality] soccer/{tier:>3}/{gclass:>5}: n={len(sub):>4} "
        f"days={len(set(days))} implied={implied.mean()*100:>4.1f}% "
        f"actual={actual.mean()*100:>4.1f}% fair={fairm.mean()*100:>4.1f}% "
        f"| overprice={over.mean()*100:+5.1f}pp [CI5 {lo*100:+.1f},CI95 {hi*100:+.1f}] -> {verdict}")


def sweep(rows, tier, gclass, markups=(220, 300, 400)):
    sub = [r for r in rows
           if r["gclass"] == gclass
           and (tier == "ALL" or (tier == "FAT") == (r["room_cc"] > FAT_CUT_CC))]
    if len(sub) < 30:
        return
    total = len(sub)
    log(f"  -- markup sweep soccer/{tier}/{gclass} (n={total}) --")
    for m in markups:
        won = [r for r in sub if r["room_cc"] >= m]
        if len(won) < 30:
            log(f"    {m/100:.1f}c: <30 won, skip")
            continue
        ask = np.array([r["fair"] for r in won]) + m / 10000.0
        yes = np.array([r["yes"] for r in won], float)
        pnl = (ask - yes) * 100.0
        wdays = np.array([r["day"] for r in won])
        lo, hi = clustered_boot(pnl, wdays, np.mean)
        log(f"    {m/100:.1f}c: won={len(won):>4} fill={100*len(won)/total:>3.0f}% "
            f"edge={pnl.mean():+5.2f}c/ct [CI5 {lo:+.2f}] YEShit={100*yes.mean():.1f}%")


def main():
    rows = load()
    log(f"loaded {len(rows):,} RESOLVED soccer combos")
    dist = Counter(r["n_games"] for r in rows)
    log(f"games-per-combo: {dict(sorted(dist.items()))}")
    same = sum(r["gclass"] == "same" for r in rows)
    log(f"same-game={same:,}  multi-game={len(rows)-same:,}")
    log("\n=== REALITY TEST (fair-independent) — where does the edge live? ===")
    for tier in ("FAT", "NORMAL", "ALL"):
        for gclass in ("same", "multi"):
            reality_test(rows, tier, gclass)
        log("")
    log("=== MARKUP SWEEP (FAT tier) — does +EV hold per bucket? ===")
    for gclass in ("same", "multi"):
        sweep(rows, "FAT", gclass)


if __name__ == "__main__":
    main()
