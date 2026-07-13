"""B stage 3 — grade the settled universe: the two-tier markup sweep, day-clustered
bootstrap CIs, and the reality test that decides whether the edge is real.

Parlay-seller mechanics (per docs/reports/2026-07-12-pnl-markup-sweep.md): requester
buys combo YES at clearing; we sell YES (=take NO) at ask = our_fair + markup m. We
WIN iff ask <= clearing  <=>  m <= room  (room = clearing - our_fair). On a won combo,
per-contract P&L = ask - combo_yes  (keep ask if it settles NO, pay 1 if YES).

THE REALITY TEST (is the edge real or favorite-hot beta?): as a seller we only profit
if combos settle YES LESS often than the market's clearing price implied. So per
sport x tier we compare, DAY-CLUSTERED:
    implied_hit (mean clearing prob)  vs  actual_hit (mean settlement)
If implied - actual > 0 with a lower-CI bound above 0 => the market overprices this
flow (retail overpays) => structural edge. If favorites just won, actual >= implied
and the edge vanishes. Equal-weight per combo (robust to a few huge combos).

Deps: numpy (repo venv). No P&L refit — this measures; the markup decision is the
POOLED multi-week lower-CI bound, of which this one week is the first sample.
"""
import csv
import os
import re
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
TMP = r"C:\Users\aahys\.claude\jobs\24844262\tmp"
IN = os.path.join(TMP, "graded_settled.csv")
RNG = np.random.default_rng(20260713)  # fixed seed — no Date/rand nondeterminism
FAT_CUT_CC = 200          # room > 2c = FAT, else NORMAL
MARKUPS_CC = list(range(0, 825, 25))
DAY_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})")


def log(m=""):
    print(m, flush=True)


def match_day(leg_tickers: str) -> str:
    days = DAY_RE.findall(leg_tickers or "")
    return min(days) if days else "unknown"


def load():
    rows = []
    with open(IN, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["resolved"] != "1":
                continue
            rows.append({
                "sport": r["sport"],
                "n_legs": int(r["n_legs"]) if r["n_legs"] else 0,
                "fair": float(r["our_fair_cc"]) / 10000.0,
                "clearing": float(r["clearing_cc"]) / 10000.0,
                "room_cc": int(r["room_cc"]),
                "yes": int(r["combo_yes"]),
                "day": match_day(r["leg_tickers"]),
            })
    return rows


def clustered_boot(vals, days, stat, n=2000):
    """Block bootstrap resampling whole match-days (the outcome-correlation unit)."""
    vals = np.asarray(vals, float)
    days = np.asarray(days)
    uniq = np.unique(days)
    idx_by_day = {d: np.where(days == d)[0] for d in uniq}
    out = []
    for _ in range(n):
        pick = RNG.choice(uniq, size=len(uniq), replace=True)
        sel = np.concatenate([idx_by_day[d] for d in pick])
        out.append(stat(vals[sel]))
    lo, hi = np.percentile(out, [5, 95])
    return lo, hi


def sweep(rows, sport, tier):
    sub = [r for r in rows if r["sport"] == sport and (
        tier == "ALL" or (tier == "FAT") == (r["room_cc"] > FAT_CUT_CC))]
    if not sub:
        return None
    total = len(sub)
    days = np.array([r["day"] for r in sub])
    log(f"\n=== {sport.upper()} / {tier} — {total:,} resolved combos, "
        f"{len(set(days))} match-days ===")
    log(f"{'mk¢':>4} {'won':>6} {'fill%':>5} {'edge¢/ct':>9} "
        f"{'[CI5':>7} {'CI95]':>7} {'YEShit%':>7}")
    best = None
    for m in MARKUPS_CC:
        won = [r for r in sub if r["room_cc"] >= m]
        if len(won) < 30:
            continue
        ask = np.array([r["fair"] for r in won]) + m / 10000.0
        yes = np.array([r["yes"] for r in won], float)
        pnl = (ask - yes) * 100.0            # ¢ per contract
        wdays = np.array([r["day"] for r in won])
        edge = pnl.mean()
        lo, hi = clustered_boot(pnl, wdays, np.mean)
        fill = 100.0 * len(won) / total
        log(f"{m/100:>4.1f} {len(won):>6,} {fill:>5.0f} {edge:>9.2f} "
            f"{lo:>7.2f} {hi:>7.2f} {100*yes.mean():>7.1f}")
        if lo > 0 and best is None:
            best = (m, edge, lo)
    if best:
        log(f"  -> min robustly +EV markup (CI5>0): {best[0]/100:.1f}¢ "
            f"(edge {best[1]:.2f}¢/ct, CI5 {best[2]:.2f})")
    else:
        log("  -> NO markup has a day-clustered lower-CI edge above 0 "
            "(one week is thin — expected; pool weeks)")
    return best


def reality_test(rows, sport, tier):
    sub = [r for r in rows if r["sport"] == sport and (
        tier == "ALL" or (tier == "FAT") == (r["room_cc"] > FAT_CUT_CC))]
    if len(sub) < 30:
        return
    days = np.array([r["day"] for r in sub])
    implied = np.array([r["clearing"] for r in sub])
    actual = np.array([r["yes"] for r in sub], float)
    fairm = np.array([r["fair"] for r in sub])
    over = implied - actual   # >0 => market overpriced (retail overpays) => our edge
    lo, hi = clustered_boot(over, days, np.mean)
    verdict = ("REAL EDGE (market overprices this flow)" if lo > 0
               else "not distinguishable from zero this week")
    log(f"\n[reality] {sport.upper()}/{tier}: implied_hit={implied.mean()*100:.1f}% "
        f"actual_hit={actual.mean()*100:.1f}% our_fair={fairm.mean()*100:.1f}%")
    log(f"          overprice(implied-actual)={over.mean()*100:+.1f}pp "
        f"[CI5 {lo*100:+.1f}, CI95 {hi*100:+.1f}] -> {verdict}")


def main():
    rows = load()
    log(f"loaded {len(rows):,} RESOLVED graded combos")
    for sport in ("soccer", "mlb"):
        n = sum(r["sport"] == sport for r in rows)
        if not n:
            continue
        for tier in ("NORMAL", "FAT", "ALL"):
            sweep(rows, sport, tier)
        for tier in ("NORMAL", "FAT"):
            reality_test(rows, sport, tier)


if __name__ == "__main__":
    main()
