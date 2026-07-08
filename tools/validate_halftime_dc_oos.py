"""OOS gate: the half-time DC extension must reproduce the empirical 1H x FT
conditionals on a HELD-OUT split, and must NOT degrade the full-game gate.

This is the ship gate for the 1H structural extension (directive point 5). The
model gets NO 1H information at fit time: (lam_a, lam_b) invert per game from
its FT closing 1X2 + O/U-2.5 lines (the SAME invert() the pricer runs), then the
1H legs price off the DC half split at a banded-constant first-half goal share
``h`` (MEASURED on train, never invented). The 1H x FT conditionals must EMERGE
from that structure and land within tolerance of the held-out empirical rates —
if they don't, the independent-increment split is mis-specified and 1H legs must
stay on the copula.

Procedure (club soccer, football-data.co.uk CSVs already under data/history):
  1. TRAIN (seasons < 2024): MEASURE h = (1H goals)/(FT goals); fit dc_rho by
     scoreline log-likelihood through the production inversion (grid MLE).
  2. TEST (2023/24 + 24/25, never touched by the fit): per game invert
     (lam_a, lam_b) from devigged FT lines; POOL the model's 1H x FT joint /
     marginal probabilities across games -> model conditionals. Compare to the
     empirical held-out conditionals (counts of actual HT/FT outcomes).
  3. FULL-GAME NON-DEGRADATION: the FT-only path is byte-identical (with_halves
     defaults False; asserted in tests/test_dixon_coles.py), so the shipped
     full-game OOS gate is unchanged. We re-assert here that an FT-only joint is
     bit-identical whether or not the half machinery is available.

Gate: the SHIPPED goal-timing families (1H total / 1H BTTS, and base rates) land
within tolerance AND the FT-only invariant holds. The 1H-RESULT persistence
conditionals are REPORTED but NOT gated: the independent-increment split
over-states them (~6pt, no h fixes it — the missing negative inter-half serial
correlation), so 1H winner/spread legs DEFER to the copula's directly-measured
first_half_moneyline / first_half_spread priors (structural.py). This is the
evidence for that fail-closed split.

Run:  uv run python tools/validate_halftime_dc_oos.py
"""

from __future__ import annotations

import csv
import math
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from combomaker.pricing.dixon_coles import (
    Btts,
    Draw,
    HalfBtts,
    HalfResult,
    HalfTotalOver,
    MatchFormat,
    ModelParams,
    StructuralError,
    Team,
    TeamWin,
    TotalOver,
    _dc_grid,
    invert,
    joint_probability,
)

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
TRAIN_BEFORE = 2024
MAX_GOALS = 12
DC_RHO_GRID = (0.00, -0.05, -0.10, -0.15)
_CLUB = {"E0", "D1", "F1", "I1", "SP1"}


@dataclass(frozen=True, slots=True)
class Game:
    p_home: float
    p_draw: float
    p_over: float
    hg: int
    ag: int
    hthg: int
    htag: int
    season: int

    # full-time markers
    @property
    def ft_home_win(self) -> bool:
        return self.hg > self.ag

    @property
    def ft_away_win(self) -> bool:
        return self.ag > self.hg

    @property
    def ft_over25(self) -> bool:
        return self.hg + self.ag >= 3

    @property
    def ft_btts(self) -> bool:
        return self.hg >= 1 and self.ag >= 1

    # first-half markers
    @property
    def h1_home_lead(self) -> bool:
        return self.hthg > self.htag

    @property
    def h1_away_lead(self) -> bool:
        return self.htag > self.hthg

    @property
    def h1_over05(self) -> bool:
        return self.hthg + self.htag >= 1

    @property
    def h1_over15(self) -> bool:
        return self.hthg + self.htag >= 2

    @property
    def h1_btts(self) -> bool:
        return self.hthg >= 1 and self.htag >= 1


def load_games() -> list[Game]:
    games: list[Game] = []
    for path in sorted(HISTORY.glob("*-2*.csv")):
        if path.stem.split("-")[0] not in _CLUB:
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    hg, ag = int(row["FTHG"]), int(row["FTAG"])
                    hthg, htag = int(row["HTHG"]), int(row["HTAG"])
                    oh = float(row.get("B365H") or row.get("AvgH") or 0)
                    od = float(row.get("B365D") or row.get("AvgD") or 0)
                    oa = float(row.get("B365A") or row.get("AvgA") or 0)
                    oo = float(row.get("B365>2.5") or row.get("Avg>2.5") or 0)
                    ou = float(row.get("B365<2.5") or row.get("Avg<2.5") or 0)
                    season = int("20" + path.stem.split("-")[1][:2])
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                if min(oh, od, oa, oo, ou) <= 1.0:
                    continue
                ih, id_, ia = 1 / oh, 1 / od, 1 / oa
                s3 = ih + id_ + ia
                io, iu = 1 / oo, 1 / ou
                games.append(
                    Game(
                        p_home=ih / s3, p_draw=id_ / s3, p_over=io / (io + iu),
                        hg=hg, ag=ag, hthg=hthg, htag=htag, season=season,
                    )
                )
    return games


# --------------------------------------------------------------- train fits


def measure_h(games: list[Game]) -> float:
    """First-half goal SHARE h = (1H goals) / (FT goals) — MEASURED, not set."""
    ht = sum(g.hthg + g.htag for g in games)
    ft = sum(g.hg + g.ag for g in games)
    return ht / ft


def invert_game(g: Game, dc_rho: float, half_share: float) -> ModelParams | None:
    legs = [
        (TeamWin(Team.A, include_et=False), g.p_home),
        (Draw(), g.p_draw),
        (TotalOver(3, include_et=False), g.p_over),
    ]
    try:
        model = invert(
            legs, dc_rho=dc_rho, et_factor=1.0 / 3.0,
            match_format=MatchFormat.GROUP, max_goals=MAX_GOALS, half_share=half_share,
        )
    except StructuralError:
        return None
    return model.params


def fit_dc_rho(train: list[Game], half_share: float) -> float:
    best_rho, best_ll = 0.0, -math.inf
    for rho in DC_RHO_GRID:
        ll, n = 0.0, 0
        for g in train:
            params = invert_game(g, rho, half_share)
            if params is None:
                continue
            grid = _dc_grid(params.lam_a, params.lam_b, params.dc_rho, params.max_goals)
            ll += math.log(max(grid[min(g.hg, MAX_GOALS), min(g.ag, MAX_GOALS)], 1e-12))
            n += 1
        print(f"  dc_rho={rho:+.2f}: train scoreline loglik/game = {ll / n:.5f} (n={n})")
        if ll > best_ll:
            best_rho, best_ll = rho, ll
    return best_rho


# --------------------------------------------------------------- conditionals


@dataclass(frozen=True, slots=True)
class Cond:
    label: str
    tol: float
    # (cond_marker, target_marker) attribute names; target None => base rate
    cond: str
    target: str | None
    # model spec builders (a=cond leg, b=target leg); target None => marginal
    a_spec: object
    b_spec: object | None
    # gates: True => a SHIPPED goal-timing family (must pass to flip the gate).
    # False => a DEFERRED 1H-result-persistence conditional, REPORTED to document
    # why 1H winner/spread legs stay on the copula (not part of the ship gate).
    gates: bool = True


def emp_conditional(games: list[Game], c: Cond) -> tuple[float, int]:
    if c.target is None:
        n = len(games)
        return sum(1 for g in games if getattr(g, c.cond)) / n, n
    rows = [g for g in games if getattr(g, c.cond)]
    n = len(rows)
    return sum(1 for g in rows if getattr(g, c.target)) / n, n


def model_conditionals(
    games: list[Game], conds: list[Cond], dc_rho: float, half_share: float
) -> dict[str, float]:
    """POOLED model conditionals — one inversion per game, all conditionals
    accumulated in a single pass (num = sum joint, den = sum P(cond))."""
    num = {c.label: 0.0 for c in conds}
    den = {c.label: 0.0 for c in conds}
    for g in games:
        params = invert_game(g, dc_rho, half_share)
        if params is None:
            continue
        for c in conds:
            p_cond = joint_probability(params, [(c.a_spec, True)], {})  # type: ignore[list-item]
            if c.b_spec is None:
                num[c.label] += p_cond
                den[c.label] += 1.0
            else:
                num[c.label] += joint_probability(
                    params, [(c.a_spec, True), (c.b_spec, True)], {}  # type: ignore[list-item]
                )
                den[c.label] += p_cond
    return {c.label: (num[c.label] / den[c.label] if den[c.label] > 0 else float("nan"))
            for c in conds}


# SHIPPED goal-timing families (gate on these) + base rates.
CONDITIONALS = [
    Cond("P(FT over2.5 | 1H over0.5)", 0.03, "h1_over05", "ft_over25",
         HalfTotalOver(1), TotalOver(3, include_et=False)),
    Cond("P(FT over2.5 | 1H over1.5)", 0.03, "h1_over15", "ft_over25",
         HalfTotalOver(2), TotalOver(3, include_et=False)),
    Cond("P(FT btts | 1H btts)", 0.03, "h1_btts", "ft_btts",
         HalfBtts(), Btts(include_et=False)),
    Cond("base P(1H over1.5)", 0.02, "h1_over15", None,
         HalfTotalOver(2), None),
    Cond("base P(1H btts)", 0.02, "h1_btts", None, HalfBtts(), None),
    Cond("base P(1H home-lead)", 0.02, "h1_home_lead", None,
         HalfResult(Team.A), None),
    # DEFERRED — 1H result persistence; reported, NOT gated (winner/spread legs
    # defer to the copula's measured first_half_moneyline prior instead).
    Cond("P(FT home-win | 1H home-lead)", 0.03, "h1_home_lead", "ft_home_win",
         HalfResult(Team.A), TeamWin(Team.A, include_et=False), gates=False),
    Cond("P(FT away-win | 1H away-lead)", 0.03, "h1_away_lead", "ft_away_win",
         HalfResult(Team.B), TeamWin(Team.B, include_et=False), gates=False),
]


# --------------------------------------------------------------- FT invariant


def full_game_unchanged() -> float:
    """Max |FT-only joint  -  same joint with the half machinery available|
    over a battery of full-game combos (the byte-for-byte invariant)."""
    worst = 0.0
    legsets = [
        [(TeamWin(Team.A, include_et=False), True)],
        [(Btts(include_et=False), True)],
        [(TotalOver(3, include_et=False), True)],
        [(TeamWin(Team.A, include_et=False), True), (TotalOver(3, include_et=False), True),
         (Btts(include_et=False), True)],
    ]
    for fmt in (MatchFormat.GROUP, MatchFormat.KNOCKOUT):
        for la, lb, rho in [(1.7, 1.05, -0.05), (2.1, 0.8, -0.1), (1.2, 1.3, 0.0)]:
            ft = ModelParams(la, lb, rho, 1 / 3, fmt, max_goals=MAX_GOALS)
            half = replace(ft, with_halves=True)
            for legs in legsets:
                worst = max(worst, abs(joint_probability(ft, legs, {})
                                       - joint_probability(half, legs, {})))
    return worst


# --------------------------------------------------------------- main


def main() -> None:
    games = load_games()
    train = [g for g in games if g.season < TRAIN_BEFORE]
    test = [g for g in games if g.season >= TRAIN_BEFORE]
    print(f"games: {len(games)} (train {len(train)} / test {len(test)}; "
          f"split at season {TRAIN_BEFORE})\n")

    h_train = measure_h(train)
    h_all = measure_h(games)
    print(f"MEASURED first-half goal share h: train={h_train:.4f}  all={h_all:.4f}")
    print("fitting dc_rho on TRAIN scorelines (production inversion):")
    t0 = time.perf_counter()
    dc_rho = fit_dc_rho(train, h_train)
    print(f"  -> fitted dc_rho = {dc_rho:+.2f}, h = {h_train:.4f} "
          f"(frozen for test; {time.perf_counter() - t0:.0f}s)\n")

    print("HELD-OUT 1H x FT conditionals (model inverted per game from FT lines "
          "only; 1H emerges from structure):")
    t0 = time.perf_counter()
    mods = model_conditionals(test, CONDITIONALS, dc_rho, h_train)
    print(f"  (test conditionals in {time.perf_counter() - t0:.0f}s)\n")

    gate = True
    print("SHIPPED goal-timing families (GATE):")
    print(f"  {'conditional':34s} {'model':>8} {'empirical':>10} {'diff':>8} "
          f"{'tol':>6}  verdict")
    for c in CONDITIONALS:
        if not c.gates:
            continue
        emp, n = emp_conditional(test, c)
        diff = abs(mods[c.label] - emp)
        ok = diff <= c.tol
        gate = gate and ok
        print(f"  {c.label:34s} {mods[c.label]:>8.4f} {emp:>10.4f} {diff:>8.4f} "
              f"{c.tol:>6.2f}  {'PASS' if ok else 'FAIL'}  (n={n})")

    print("\nDEFERRED 1H-result persistence (REPORTED; these legs stay on the "
          "copula's measured prior — NOT gated):")
    for c in CONDITIONALS:
        if c.gates:
            continue
        emp, n = emp_conditional(test, c)
        diff = abs(mods[c.label] - emp)
        note = "OVER-states persistence" if mods[c.label] > emp else "understates"
        print(f"  {c.label:34s} {mods[c.label]:>8.4f} {emp:>10.4f} {diff:>8.4f} "
              f"    {note}  (n={n})")
    print()

    ft_err = full_game_unchanged()
    ft_ok = ft_err < 1e-9
    gate = gate and ft_ok
    print(f"FULL-GAME NON-DEGRADATION: max |FT-only - with-halves| joint = "
          f"{ft_err:.2e}  {'PASS (<1e-9)' if ft_ok else 'FAIL'}\n")

    print("GATE: " + (
        "PASS — the half extension reproduces the held-out 1H x FT conditionals "
        "within tolerance and leaves the full-game path bit-identical."
        if gate else
        "FAIL — a conditional is out of tolerance or the full-game path moved; "
        "1H legs must stay on the copula (directive point 5)."
    ))
    sys.exit(0 if gate else 1)


if __name__ == "__main__":
    main()
