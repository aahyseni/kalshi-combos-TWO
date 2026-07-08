"""Calibrate FT-BTTS x 1H-TOTAL-over-N soccer correlation (the copula prior for
``soccer:btts|first_half_total``).

The pair was UNLISTED in the config, so every FT-BTTS x 1H-total combo that the
DC structural pricer declined fell to the flat +0.6 same-event fallback (combos
C22/C27/C28, live-RFQ validation). This derives the correct YES-YES copula rho
TWO independent ways and reconciles:

  A. STRUCTURAL (our own OOS-gated half-time Dixon-Coles model). Per club match,
     invert (lam_a, lam_b) from the FT closing 1X2 + O/U-2.5 lines (the SAME
     invert() the pricer runs), then read P(FT-BTTS), P(1H-over-N),
     P(FT-BTTS & 1H-over-N) off the DC half-split scoreline at the SHIPPED
     h=0.45 / dc_rho=-0.05. POOL across games and invert to a drop-in copula
     rho. Mirrors tools/validate_halftime_dc_oos.py (model_conditionals).

  B. EMPIRICAL (football-data.co.uk HT/FT ground truth). Over the same club
     matches count P(FT-BTTS), P(1H-total>=N), P(both) directly from HTHG/HTAG/
     FTHG/FTAG and invert the SAME copula. Mirrors
     tools/calibrate_soccer_1h_spread.py exactly.

Settlement windows: FT-BTTS = both teams score in REGULATION 90' (include_et
False); 1H-total = goals through 45'. Line semantics (SOURCE OF TRUTH): a Kalshi
1H total ``…-N`` is "over (N-0.5)" i.e. 1H goals >= N. The common line is
KXWC1HTOTAL-…-1 (over 0.5, N=1); also report N=2 (over 1.5).

Run: uv run python tools/calibrate_soccer_btts_1h_total.py
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from combomaker.ops.config import StructuralConfig
from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import (
    Btts,
    Draw,
    HalfTotalOver,
    LegSpec,
    MatchFormat,
    StructuralError,
    Team,
    TeamWin,
    TotalOver,
    invert,
    joint_probability,
)

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
_Z99 = 2.576
_CLUB_PREFIXES = {"E0", "D1", "F1", "I1", "SP1"}


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """The rho making copula(p_a, p_b; rho) == p_ab (monotone => bisection).
    Identical to the shipped calibration tools."""

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


# --------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class Row:
    hthg: int
    htag: int
    fthg: int
    ftag: int
    # FT closing marginals (None when no usable line)
    p_home: float | None
    p_draw: float | None
    p_over: float | None


def load_rows() -> list[Row]:
    rows: list[Row] = []
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
                p_home = p_draw = p_over = None
                try:
                    oh = float(r.get("B365H") or r.get("AvgH") or 0)
                    od = float(r.get("B365D") or r.get("AvgD") or 0)
                    oa = float(r.get("B365A") or r.get("AvgA") or 0)
                    oo = float(r.get("B365>2.5") or r.get("Avg>2.5") or 0)
                    ou = float(r.get("B365<2.5") or r.get("Avg<2.5") or 0)
                    if min(oh, od, oa, oo, ou) > 1.0:
                        ih, id_, ia = 1 / oh, 1 / od, 1 / oa
                        s3 = ih + id_ + ia
                        io, iu = 1 / oo, 1 / ou
                        p_home, p_draw, p_over = ih / s3, id_ / s3, io / (io + iu)
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    pass
                rows.append(Row(hthg, htag, fthg, ftag, p_home, p_draw, p_over))
    return rows


# --------------------------------------------------------------- B. EMPIRICAL


def measure_empirical(rows: list[Row], n_line: int, label: str) -> float:
    n = len(rows)
    a = [r.fthg >= 1 and r.ftag >= 1 for r in rows]            # FT-BTTS (reg 90')
    b = [(r.hthg + r.htag) >= n_line for r in rows]            # 1H total >= N
    pa = sum(a) / n
    pb = sum(b) / n
    pab = sum(x and y for x, y in zip(a, b, strict=True)) / n
    rho = implied_rho(pa, pb, pab)
    se = math.sqrt(max(pab * (1 - pab), 1e-12) / n)
    lo = implied_rho(pa, pb, max(0.0, pab - _Z99 * se))
    hi = implied_rho(pa, pb, min(1.0, pab + _Z99 * se))
    cond = pab / pb if pb else float("nan")
    prod = pa * pb
    print(f"{label:40s} n={n} P(BTTS)={pa:.3f} P(1H>={n_line})={pb:.3f} "
          f"P(both)={pab:.3f} (indep {prod:.3f})  P(BTTS|1H)={cond:.3f}  "
          f"rho={rho:+.3f}  99%CI[{lo:+.3f},{hi:+.3f}]")
    return rho


# --------------------------------------------------------------- A. STRUCTURAL


def invert_game(r: Row, cfg: StructuralConfig):  # type: ignore[no-untyped-def]
    if r.p_home is None or r.p_draw is None or r.p_over is None:
        return None
    legs: list[tuple[LegSpec, float]] = [
        (TeamWin(Team.A, include_et=False), r.p_home),
        (Draw(), r.p_draw),
        (TotalOver(3, include_et=False), r.p_over),
    ]
    try:
        model = invert(
            legs,
            dc_rho=cfg.dc_rho,
            et_factor=cfg.et_factor,
            match_format=MatchFormat.GROUP,
            max_goals=cfg.max_goals,
            half_share=cfg.half_share,
        )
    except StructuralError:
        return None
    return model.params


def measure_structural(rows: list[Row], n_line: int, cfg: StructuralConfig,
                       label: str) -> float:
    """POOLED DC-induced copula rho: sum model P(A), P(B), P(A&B) over games,
    invert. Btts/HalfTotalOver read no ET, so match format is irrelevant here."""
    a_spec = Btts(include_et=False)
    b_spec = HalfTotalOver(min_total=n_line)
    sum_a = sum_b = sum_ab = 0.0
    n = 0
    for r in rows:
        params = invert_game(r, cfg)
        if params is None:
            continue
        pa = joint_probability(params, [(a_spec, True)], {})
        pb = joint_probability(params, [(b_spec, True)], {})
        pab = joint_probability(params, [(a_spec, True), (b_spec, True)], {})
        sum_a += pa
        sum_b += pb
        sum_ab += pab
        n += 1
    pa, pb, pab = sum_a / n, sum_b / n, sum_ab / n
    rho = implied_rho(pa, pb, pab)
    cond = pab / pb if pb else float("nan")
    prod = pa * pb
    print(f"{label:40s} n={n} P(BTTS)={pa:.3f} P(1H>={n_line})={pb:.3f} "
          f"P(both)={pab:.3f} (indep {prod:.3f})  P(BTTS|1H)={cond:.3f}  "
          f"rho={rho:+.3f}")
    return rho


# --------------------------------------------------------------- main


def main() -> None:
    rows = load_rows()
    cfg = StructuralConfig()
    with_lines = sum(1 for r in rows if r.p_home is not None)
    print(f"=== btts | first_half_total  ({len(rows)} club matches, top-5 EU "
          f"20/21-24/25; {with_lines} with FT closing lines) ===")
    print(f"    shipped model params: dc_rho={cfg.dc_rho}  half_share="
          f"{cfg.half_share}  max_goals={cfg.max_goals}\n")

    print("B. EMPIRICAL (football-data HT/FT ground truth, invert shipped copula):")
    b1 = measure_empirical(rows, 1, "  BTTS x 1H over0.5 (N=1, common line)")
    b2 = measure_empirical(rows, 2, "  BTTS x 1H over1.5 (N=2)")
    print()

    print("A. STRUCTURAL (half-time Dixon-Coles, inverted per game from FT lines):")
    a1 = measure_structural(rows, 1, cfg, "  BTTS x 1H over0.5 (N=1, common line)")
    a2 = measure_structural(rows, 2, cfg, "  BTTS x 1H over1.5 (N=2)")
    print()

    print("RECONCILIATION:")
    for lab, a, b in [("N=1 (over0.5)", a1, b1), ("N=2 (over1.5)", a2, b2)]:
        agree = "AGREE" if abs(a - b) <= 0.10 else "DISAGREE"
        print(f"  {lab:16s} structural={a:+.3f}  empirical={b:+.3f}  "
              f"|diff|={abs(a - b):.3f}  -> {agree}")
    print(f"\n  line-dependence (empirical):  N=1 {b1:+.3f} vs N=2 {b2:+.3f}  "
          f"(delta {b2 - b1:+.3f})")
    print(f"  line-dependence (structural): N=1 {a1:+.3f} vs N=2 {a2:+.3f}  "
          f"(delta {a2 - a1:+.3f})")
    print("\n  coherence anchor: shipped soccer:first_half_total|total = +0.73, "
          "soccer:btts|total = +0.70")


if __name__ == "__main__":
    main()
