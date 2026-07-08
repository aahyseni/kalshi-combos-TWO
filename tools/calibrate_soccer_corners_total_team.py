"""Measure soccer `corners|corners_team` = TOTAL corners x ONE team's corners
(same match), inverted through the SHIPPED copula (the value the engine prices
with). Total corners CONTAIN a team's corners as a large component, so the pair
is strongly comonotone — the one corners pair where the old +0.6 fallback point
was accidentally close, but its zero-spanning fail-safe band over-widened
corners-heavy quotes (RFQ test C24/C25/C26). Ships the typed value + tight band.

Data: football-data.co.uk club CSVs (HC/AC = home/away corners), same 8,981-match
set as the sister calibrators. Run: `uv run python tools/calibrate_soccer_corners_total_team.py`
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
_CLUB_PREFIXES = {"E0", "D1", "F1", "I1", "SP1"}
# real Kalshi lines observed in the prod RFQ tape: KXWCCORNERS (total) 7-10,
# KXWCTCORNERS (team) 4-6 dominate; grid straddles those.
_TOTAL_LINES = (8, 9, 10, 11)
_TEAM_LINES = (4, 5, 6)


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """The rho making copula(p_a, p_b; rho) == p_ab (monotone => bisection)."""

    def joint(rho: float) -> float:
        return gaussian_copula_joint_prob([p_a, p_b], np.array([[1.0, rho], [rho, 1.0]]))

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


def load() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    tot, home, away = [], [], []
    for path in sorted(HISTORY.glob("*.csv")):
        if path.stem.split("-")[0] not in _CLUB_PREFIXES:
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    hc, ac = float(r["HC"]), float(r["AC"])
                except (TypeError, ValueError, KeyError):
                    continue
                if hc != hc or ac != ac:  # NaN
                    continue
                tot.append(hc + ac)
                home.append(hc)
                away.append(ac)
    return np.array(tot), np.array(home), np.array(away)


def main() -> None:
    tot, home, away = load()
    print(f"=== corners | corners_team  ({len(tot)} club matches, top-5 EU) ===")
    print(f"mean total {tot.mean():.2f} | home {home.mean():.2f} | away {away.mean():.2f}")
    print(f"count-level: corr(HC,AC)={np.corrcoef(home, away)[0, 1]:+.3f}  "
          f"corr(total,home)={np.corrcoef(tot, home)[0, 1]:+.3f}  "
          f"corr(total,away)={np.corrcoef(tot, away)[0, 1]:+.3f}\n")

    rows = []
    print(f"{'tot>=':>6}{'team>=':>7}{'side':>5}{'P(tot)':>8}{'P(team)':>9}{'P(both)':>9}{'rho':>8}")
    for nt in _TOTAL_LINES:
        for nm in _TEAM_LINES:
            for lbl, team in (("H", home), ("A", away)):
                p1 = float((tot >= nt).mean())
                p2 = float((team >= nm).mean())
                p12 = float(((tot >= nt) & (team >= nm)).mean())
                if min(p1, p2, p12) < 1e-4 or max(p1, p2) > 1 - 1e-4:
                    continue
                rho = implied_rho(p1, p2, p12)
                rows.append((lbl, rho))
                print(f"{nt:>6}{nm:>7}{lbl:>5}{p1:>8.3f}{p2:>9.3f}{p12:>9.3f}{rho:>+8.3f}")

    allr = [r for _, r in rows]
    h = [r for s, r in rows if s == "H"]
    a = [r for s, r in rows if s == "A"]
    print(f"\nrho: mean {np.mean(allr):+.3f}  median {np.median(allr):+.3f}  "
          f"range [{min(allr):+.3f}, {max(allr):+.3f}]")
    print(f"home mean {np.mean(h):+.3f} | away mean {np.mean(a):+.3f}")
    print(f"\nSHIPPED: corners|corners_team = +0.62, band 0.15 "
          f"(home/away blend; band spans the {min(allr):.2f}-{max(allr):.2f} line/side range)")


if __name__ == "__main__":
    main()
