"""Measure soccer `corners_team|moneyline` with ORIENTATION (:same / :opp / :tie).

A team's corners vs a match-result leg. Raw pooled corr is a Simpson's-paradox
trap (+0.03: strong teams both win and take corners), so we STRENGTH-CONTROL —
bin by the market-implied win/draw prob (devigged closing odds) and pool the
within-bin copula rho, the residual dependence the copula prior needs.

  :same  team's corners  ×  THAT team wins      (chasing team earns corners -> -)
  :opp   team's corners  ×  the OPPONENT wins    (~ +, mirror)
  :tie   team's corners  ×  the match is drawn

Data: football-data.co.uk club CSVs (HC/AC corners, FTR result, closing odds).
Both team perspectives pooled. Run: uv run python tools/calibrate_soccer_corners_team_winner.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
_CLUB_PREFIXES = {"E0", "D1", "F1", "I1", "SP1"}
_TEAM_LINES = (4, 5, 6)
_NBINS = 10


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
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


def _f(row: dict, *keys: str) -> float | None:
    for k in keys:
        v = row.get(k)
        if v:
            try:
                return float(v)
            except ValueError:
                pass
    return None


def load() -> list[dict]:
    """One record per (match, team-perspective): the team's corners + strength +
    the three result events from that team's point of view."""
    recs: list[dict] = []
    for path in sorted(HISTORY.glob("*.csv")):
        if path.stem.split("-")[0] not in _CLUB_PREFIXES:
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                hc, ac = _f(r, "HC"), _f(r, "AC")
                ftr = r.get("FTR")
                oh = _f(r, "AvgH", "B365H", "PSH")
                od = _f(r, "AvgD", "B365D", "PSD")
                oa = _f(r, "AvgA", "B365A", "PSA")
                if None in (hc, ac, oh, od, oa) or ftr not in ("H", "D", "A"):
                    continue
                ph, pd, pa = 1 / oh, 1 / od, 1 / oa
                s = ph + pd + pa
                ph, pd, pa = ph / s, pd / s, pa / s  # devig
                # home perspective
                recs.append({"corners": hc, "win_p": ph, "draw_p": pd,
                             "same": ftr == "H", "opp": ftr == "A", "tie": ftr == "D"})
                # away perspective
                recs.append({"corners": ac, "win_p": pa, "draw_p": pd,
                             "same": ftr == "A", "opp": ftr == "H", "tie": ftr == "D"})
    return recs


def binned_rho(recs: list[dict], corner_line: int, event: str, strength_key: str) -> float:
    corner = np.array([r["corners"] >= corner_line for r in recs])
    ev = np.array([r[event] for r in recs])
    strength = np.array([r[strength_key] for r in recs])
    order = np.argsort(strength)
    rhos, weights = [], []
    for chunk in np.array_split(order, _NBINS):
        c, e = corner[chunk], ev[chunk]
        p1, p2 = c.mean(), e.mean()
        p12 = (c & e).mean()
        if min(p1, p2, p12) < 1e-3 or max(p1, p2) > 1 - 1e-3:
            continue
        rhos.append(implied_rho(float(p1), float(p2), float(p12)))
        weights.append(len(chunk))
    return float(np.average(rhos, weights=weights))


def main() -> None:
    recs = load()
    print(f"=== corners_team | moneyline  ({len(recs)} team-perspectives, "
          f"{len(recs)//2} matches, strength-binned) ===\n")
    print(f"{'line':>5}  {'same (team wins)':>18}  {'opp (rival wins)':>18}  {'tie (draw)':>12}")
    agg = {"same": [], "opp": [], "tie": []}
    for n in _TEAM_LINES:
        rs = binned_rho(recs, n, "same", "win_p")
        ro = binned_rho(recs, n, "opp", "win_p")
        rt = binned_rho(recs, n, "tie", "draw_p")
        agg["same"].append(rs)
        agg["opp"].append(ro)
        agg["tie"].append(rt)
        print(f"{n:>5}  {rs:>+18.3f}  {ro:>+18.3f}  {rt:>+12.3f}")
    print(f"\n{'MEAN':>5}  {np.mean(agg['same']):>+18.3f}  {np.mean(agg['opp']):>+18.3f}  "
          f"{np.mean(agg['tie']):>+12.3f}")
    # unconditional (no strength control) for contrast — the Simpson trap
    print("\n(unconditional, NO strength control — the Simpson's-paradox trap:)")
    all_recs = recs
    for n in (5,):
        c = np.array([r["corners"] >= n for r in all_recs])
        for ev in ("same", "opp", "tie"):
            e = np.array([r[ev] for r in all_recs])
            p1, p2, p12 = c.mean(), e.mean(), (c & e).mean()
            r = implied_rho(float(p1), float(p2), float(p12))
            print(f"  line{n} {ev:>4}: raw copula rho {r:+.3f}")


if __name__ == "__main__":
    main()
