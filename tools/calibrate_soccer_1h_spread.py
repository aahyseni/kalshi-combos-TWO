"""Calibrate FIRST-HALF-SPREAD x FULL-TIME soccer correlations (SGP rank 1/2).

The engine had no typed prior for ``KXWC1HSPREAD`` (first-half spread = 1H goal
margin), so every 1H-spread combo fell to the flat +0.6 same-event fallback — a
wrong-signed default. This measures the REACHABLE pairs (Kalshi allows 1H-spread
with FULL-TIME legs; it blocks 1H-spread x 1H-total / x 1H-over, so those are
skipped) from the EXISTING football-data.co.uk club CSVs in ``data/history/``,
which carry half-time goals (HTHG/HTAG) alongside full-time (FTHG/FTAG).

A 1H spread NAMES a team, so the spread x spread and spread x moneyline pairs are
ORIENTATION-DEPENDENT, resolved to ``:same`` (both legs name the same team) vs
``:opp`` (different teams) — the same/opposite analogue of the shipped
``first_half_moneyline|moneyline`` winner prior. ``spread x total`` is
orientation-independent (a total names no team).

Line convention (SOURCE OF TRUTH, prod RFQ tape 2026-07-07): the ONLY 1H-spread
line traded is ``2`` (``…-<TEAM>2`` = that team leads at half by over 1.5, i.e.
1H margin >= 2). Full-game spread is modal at line 2 (margin >= 2). FT total is
measured at over 2.5 (>= 3), the classic soccer line, with robustness at over
1.5 / over 3.5.

Method mirrors ``tools/calibrate_soccer_1h_winner_total.py`` /
``tools/calibrate_soccer_firsthalf.py`` exactly: over N matches measure P(A),
P(B), P(A∩B); invert the SAME copula the pricer runs
(``combomaker.pricing.copula.gaussian_copula_joint_prob``) by bisection to a
drop-in ``pair_rho`` (``implied_rho``); 99% CI = binomial SE on P(A∩B) pushed
through the monotone solver (``_Z99 = 2.576``).

Run: uv run python tools/calibrate_soccer_1h_spread.py
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
_Z99 = 2.576
_CLUB_PREFIXES = {"E0", "D1", "F1", "I1", "SP1"}


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """The rho making copula(p_a, p_b; rho) == p_ab (monotone => bisection)."""

    def joint(rho: float) -> float:
        corr = np.array([[1.0, rho], [rho, 1.0]])
        return gaussian_copula_joint_prob([p_a, p_b], corr)

    lo, hi = -0.99, 0.99
    if p_ab <= joint(lo):
        return lo
    if p_ab >= joint(hi):
        return hi
    for _ in range(80):
        mid = (lo + hi) / 2
        if joint(mid) < p_ab:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def load() -> list[dict[str, bool]]:
    """Every club match as booleans for the 1H-spread (line 2 = margin>=2) x FT
    events, from BOTH the home team's and the away team's naming perspective."""
    rows: list[dict[str, bool]] = []
    for path in sorted(HISTORY.glob("*.csv")):
        if path.stem.split("-")[0] not in _CLUB_PREFIXES:
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    hthg = int(r["HTHG"])
                    htag = int(r["HTAG"])
                    fthg = int(r["FTHG"])
                    ftag = int(r["FTAG"])
                except (KeyError, ValueError):
                    continue
                hm1 = hthg - htag  # home 1H margin (signed toward home)
                hmf = fthg - ftag  # home FT margin
                ftt = fthg + ftag  # FT total
                rows.append({
                    # 1H spread line 2, named team leads at half by >= 2
                    "fhs_home": hm1 >= 2,
                    "fhs_away": hm1 <= -2,
                    # FT spread line 2, named team leads at full by >= 2
                    "ft_sprd_home": hmf >= 2,
                    "ft_sprd_away": hmf <= -2,
                    # FT moneyline, named team wins
                    "ft_win_home": hmf >= 1,
                    "ft_win_away": hmf <= -1,
                    # FT totals
                    "ft_over15": ftt >= 2,
                    "ft_over25": ftt >= 3,
                    "ft_over35": ftt >= 4,
                })
    return rows


def measure(rows: list[dict[str, bool]], a: str, b: str, label: str) -> float:
    n = len(rows)
    pa = sum(r[a] for r in rows) / n
    pb = sum(r[b] for r in rows) / n
    pab = sum(r[a] and r[b] for r in rows) / n
    rho = implied_rho(pa, pb, pab)
    se = math.sqrt(max(pab * (1 - pab), 1e-12) / n)
    lo = implied_rho(pa, pb, max(0.0, pab - _Z99 * se))
    hi = implied_rho(pa, pb, min(1.0, pab + _Z99 * se))
    cond = pab / pb if pb else float("nan")  # P(A | B)
    print(f"{label:46s} n={n} P(A)={pa:.3f} P(B)={pb:.3f} P(AB)={pab:.3f} "
          f"P(A|B)={cond:.3f}  rho={rho:+.3f}  99%CI[{lo:+.3f},{hi:+.3f}]")
    return rho


def measure_pooled(
    rows: list[dict[str, bool]],
    pairs: list[tuple[str, str]],
    label: str,
) -> float:
    """Pool both team-naming orientations of an orientation class into one
    measurement (each match contributes one obs per orientation; the two share a
    match so the CI is mildly optimistic — the shipped band is wider)."""
    pooled: list[dict[str, bool]] = []
    for a, b in pairs:
        pooled.extend({"A": r[a], "B": r[b]} for r in rows)
    return measure(pooled, "A", "B", label)


def main() -> None:
    rows = load()
    print(f"=== 1H-spread(line2) x full-time, {len(rows)} club matches "
          f"(top-5 EU 20/21-24/25) ===\n")

    print("first_half_spread|spread  (1H margin>=2  x  FT margin>=2):")
    measure(rows, "fhs_home", "ft_sprd_home", "  :same home (1H home>=2 x FT home>=2)")
    measure(rows, "fhs_away", "ft_sprd_away", "  :same away (1H away>=2 x FT away>=2)")
    measure_pooled(rows, [("fhs_home", "ft_sprd_home"),
                          ("fhs_away", "ft_sprd_away")], "  :same POOLED")
    measure(rows, "fhs_home", "ft_sprd_away", "  :opp  (1H home>=2 x FT away>=2)")
    measure(rows, "fhs_away", "ft_sprd_home", "  :opp  (1H away>=2 x FT home>=2)")
    measure_pooled(rows, [("fhs_home", "ft_sprd_away"),
                          ("fhs_away", "ft_sprd_home")], "  :opp  POOLED")
    print()

    print("first_half_spread|moneyline  (1H margin>=2  x  FT win):")
    measure(rows, "fhs_home", "ft_win_home", "  :same home (1H home>=2 x FT home win)")
    measure(rows, "fhs_away", "ft_win_away", "  :same away (1H away>=2 x FT away win)")
    measure_pooled(rows, [("fhs_home", "ft_win_home"),
                          ("fhs_away", "ft_win_away")], "  :same POOLED")
    measure(rows, "fhs_home", "ft_win_away", "  :opp  (1H home>=2 x FT away win)")
    measure(rows, "fhs_away", "ft_win_home", "  :opp  (1H away>=2 x FT home win)")
    measure_pooled(rows, [("fhs_home", "ft_win_away"),
                          ("fhs_away", "ft_win_home")], "  :opp  POOLED")
    print()

    print("first_half_spread|total  (1H margin>=2  x  FT total; orientation-free):")
    for tot, lab in [("ft_over15", "over1.5"), ("ft_over25", "over2.5"),
                     ("ft_over35", "over3.5")]:
        measure_pooled(rows, [("fhs_home", tot), ("fhs_away", tot)],
                       f"  POOLED x FT {lab}")
    print()

    # sanity: containments the sign relies on
    lead = [r for r in rows if r["fhs_home"]]
    print(f"sanity: of {len(lead)} matches with 1H home-lead>=2, "
          f"{sum(r['ft_win_home'] for r in lead)} are FT home wins, "
          f"{sum(r['ft_win_away'] for r in lead)} are FT away wins "
          f"(opp near-0 => strong -rho)")


if __name__ == "__main__":
    main()
