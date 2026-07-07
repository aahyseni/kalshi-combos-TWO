"""Measure WITHIN-first-half YES-YES correlations from football-data HT scores.

Specifically the pair the engine currently defaults to +0.6:
  first_half_moneyline x first_half_total, resolved by team vs tie.

Also confirms the structural containments:
  1H under 0.5 (0-0)  == subset of 1H tie   (tie x over  => strong negative)
  1H team-lead        == subset of 1H over   (lead x over => strong positive)
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
_Z99 = 2.576


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
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


def load() -> list[dict]:
    rows = []
    for path in sorted(HISTORY.glob("*.csv")):
        stem = path.stem
        if stem.split("-")[0] not in {"E0", "D1", "F1", "I1", "SP1"}:
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    hthg = int(r["HTHG"])
                    htag = int(r["HTAG"])
                except (KeyError, ValueError):
                    continue
                ht = hthg + htag
                rows.append({
                    "ht_tie": hthg == htag,
                    "ht_home_lead": hthg > htag,
                    "ht_away_lead": htag > hthg,
                    "ht_lead": hthg != htag,
                    "ht_over05": ht >= 1,
                    "ht_over15": ht >= 2,
                })
    return rows


def measure(rows, a, b, label):
    n = len(rows)
    pa = sum(r[a] for r in rows) / n
    pb = sum(r[b] for r in rows) / n
    pab = sum(r[a] and r[b] for r in rows) / n
    rho = implied_rho(pa, pb, pab)
    se = math.sqrt(max(pab * (1 - pab), 1e-12) / n)
    lo = implied_rho(pa, pb, max(0.0, pab - _Z99 * se))
    hi = implied_rho(pa, pb, min(1.0, pab + _Z99 * se))
    cond = pab / pb if pb else float("nan")  # P(A | B)
    print(f"{label:42s} n={n} P(A)={pa:.3f} P(B)={pb:.3f} P(AB)={pab:.3f} "
          f"P(A|B)={cond:.3f}  rho={rho:+.3f}  99%CI[{lo:+.3f},{hi:+.3f}]")
    return rho


def main():
    rows = load()
    print(f"=== within-first-half pairs, {len(rows)} club matches (top-5 EU 20/21-24/25) ===\n")
    print("TIE-oriented (first_half_moneyline=TIE  x  first_half_total over 0.5):")
    measure(rows, "ht_tie", "ht_over05", "  1H tie  x 1H over0.5")
    print("  ^ note: 1H under0.5 (0-0) is a SUBSET of 1H tie, so tie x over is strongly NEG\n")

    print("TEAM-oriented (first_half_moneyline=TEAM x first_half_total over 0.5):")
    measure(rows, "ht_home_lead", "ht_over05", "  1H home-lead x 1H over0.5")
    measure(rows, "ht_away_lead", "ht_over05", "  1H away-lead x 1H over0.5")
    measure(rows, "ht_lead", "ht_over05", "  1H any-lead x 1H over0.5 (pooled)")
    print("  ^ note: a 1H lead REQUIRES >=1 goal, so lead x over is strongly POS\n")

    # sanity: containments
    under = [r for r in rows if not r["ht_over05"]]
    print(f"sanity: of {len(under)} matches with 1H under0.5 (0-0), "
          f"{sum(r['ht_tie'] for r in under)} are ties "
          f"(should be ALL: 0-0 is a tie)")
    lead = [r for r in rows if r["ht_lead"]]
    print(f"sanity: of {len(lead)} matches with a 1H lead, "
          f"{sum(r['ht_over05'] for r in lead)} are over0.5 "
          f"(should be ALL: a lead needs a goal)")


if __name__ == "__main__":
    main()
