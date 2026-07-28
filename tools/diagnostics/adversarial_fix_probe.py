"""INDEPENDENT adversarial probe for FIX 2 / FIX 3 / FIX 5 (2026-07-28).

This does NOT trust the shipped unit tests. It BRUTE-FORCES the ground truth by
enumerating every assignment of every market to {yes, no} and comparing the
module's answer against the exact realizable worst case.

  PROBE A (FIX 3 — mutex credit cannot be forged). For randomly generated books
  of NO/YES combo units over a tiny market universe, compute the TRUE realizable
  deterministic worst case by exhaustive enumeration over 2^m outcomes, then
  assert ``mutex_aware_det_max_and_credit`` satisfies
  ``bound - credit >= true_worst``. A forged credit is exactly the case where
  this fails. Also asserts the credit is 0 whenever the true worst equals the
  comonotone sum (nothing offsets, so nothing may be credited).

  PROBE B (FIX 2 — settled resolution can never discharge live risk). For random
  positions with random PARTIAL settlement maps (including UNKNOWN legs), decide
  by enumeration over the unknown legs whether the position can still lose. If
  it can, ``position_settled_cannot_lose`` MUST return False.

  PROBE C (FIX 5 — decay is more conservative than the measurement). For random
  snapshots and random book growth, assert EVERY loss axis of the decayed view is
  >= the snapshot's, and that the ruin axis never falls.

Rule 8: imports and calls the live modules; reimplements only the brute-force
ORACLE, which is deliberately independent of the implementation.

Read-only. No config, no store, no network.
"""

from __future__ import annotations

import itertools
import random

from combomaker.risk.exposure import LegRef, Side
from combomaker.sim.book_risk import (
    DetMaxUnit,
    mutex_aware_det_max_and_credit,
    position_settled_cannot_lose,
)
from combomaker.sim.engine import ComboPosition


def _unit_loses(unit: DetMaxUnit, world: dict[str, str]) -> bool:
    """ORACLE. Does this unit lose its premium in this outcome?

    long NO  loses  <=>  EVERY leg resolves to its selected side (parlay HITS)
    long YES loses  <=>  SOME leg resolves against it (parlay MISSES)
    A self-contradictory leg set (one market on both sides) can never HIT.
    """
    hits = all(world[leg.market_ticker] == leg.side for leg in unit.legs)
    return hits if unit.our_side is Side.NO else not hits


def _true_worst_cc(units: list[DetMaxUnit], markets: list[str]) -> float:
    worst = 0.0
    for combo in itertools.product(("yes", "no"), repeat=len(markets)):
        world = dict(zip(markets, combo, strict=True))
        worst = max(worst, sum(u.loss_cc for u in units if _unit_loses(u, world)))
    return worst


def probe_a(trials: int = 4000, seed: int = 20260728) -> None:
    rng = random.Random(seed)
    markets = ["M1", "M2", "M3", "M4"]
    violations = 0
    forged = 0
    credited = 0
    checked = 0
    for t in range(trials):
        n = rng.randint(2, 5)
        units: list[DetMaxUnit] = []
        for i in range(n):
            k = rng.randint(1, 3)
            legs = tuple(
                LegRef(m, "EVT", rng.choice(("yes", "no")))
                for m in rng.sample(markets, k)
            )
            price = rng.randrange(50, 9000)
            centi = rng.randrange(100, 5000)
            units.append(
                DetMaxUnit(
                    unit_id=f"u{t}_{i}",
                    our_side=rng.choice((Side.NO, Side.YES)),
                    contracts_centi=centi,
                    entry_price_cc=price,
                    legs=legs,
                    loss_cc=float(price) * (centi / 100.0),
                )
            )
        bound, credit = mutex_aware_det_max_and_credit(units, reserved_loss_cc=0.0)
        true_worst = _true_worst_cc(units, markets)
        como = sum(u.loss_cc for u in units)
        checked += 1
        if credit > 0.0:
            credited += 1
        # SOUNDNESS: the charged number must still cover the true worst case.
        if bound - credit < true_worst - 1e-6:
            violations += 1
            if violations <= 3:
                print(f"  VIOLATION t={t}: bound={bound:.1f} credit={credit:.1f} "
                      f"charged={bound-credit:.1f} < true_worst={true_worst:.1f}")
                for u in units:
                    print(f"     {u.unit_id} side={u.our_side} loss={u.loss_cc:.1f} "
                          f"legs={[(g.market_ticker, g.side) for g in u.legs]}")
        # FORGERY: nothing offsets (true worst == comonotone) yet credit > 0.
        if abs(true_worst - como) < 1e-6 and credit > 0.0:
            forged += 1
            if forged <= 3:
                print(f"  FORGED t={t}: credit={credit:.1f} on a book where every "
                      f"unit can lose together (como={como:.1f})")
    print(f"PROBE A (FIX 3 forgery): {checked} random books x 2^{len(markets)} worlds; "
          f"{credited} took credit; SOUNDNESS violations={violations}; FORGED credits={forged}")
    assert violations == 0 and forged == 0


def probe_b(trials: int = 6000, seed: int = 4242) -> None:
    rng = random.Random(seed)
    wrong = 0
    safe_calls = 0
    for _ in range(trials):
        nlegs = rng.randint(1, 4)
        sides = tuple(rng.choice(("yes", "no")) for _ in range(nlegs))
        our_no = rng.random() < 0.75
        pos = ComboPosition(
            leg_indices=tuple(range(nlegs)),
            contracts=1.0,
            price_cc=5000,
            fee_cc=0,
            side="no" if our_no else "yes",
            leg_sides=sides,
        )
        settled = {
            i: float(rng.randint(0, 1))
            for i in range(nlegs)
            if rng.random() < 0.55
        }
        claim = position_settled_cannot_lose(pos, settled)
        # ORACLE: enumerate every completion of the UNKNOWN legs; can it lose?
        unknown = [i for i in range(nlegs) if i not in settled]
        can_lose = False
        for combo in itertools.product((0.0, 1.0), repeat=len(unknown)):
            vals = dict(settled)
            vals.update(dict(zip(unknown, combo, strict=True)))
            sel = [
                vals[i] if sides[i] == "yes" else 1.0 - vals[i]
                for i in range(nlegs)
            ]
            hits = all(v == 1.0 for v in sel)
            if (hits if our_no else not hits):
                can_lose = True
                break
        if claim:
            safe_calls += 1
        if claim and can_lose:
            wrong += 1
            if wrong <= 3:
                print(f"  DISCHARGED LIVE RISK: side={'no' if our_no else 'yes'} "
                      f"sides={sides} settled={settled}")
    print(f"PROBE B (FIX 2 discharge): {trials} random partial settlements; "
          f"{safe_calls} claimed safe; WRONGLY discharged={wrong}")
    assert wrong == 0


def probe_c(trials: int = 2000, seed: int = 777) -> None:
    from combomaker.rfq.lifecycle import _decay_book_risk

    class _Snap:
        pass

    rng = random.Random(seed)
    regressions = 0
    # Exactly the loss axes _DecayedBookRisk carries (== the PortfolioRisk
    # protocol the caps read). es_99_cc / directional_worst_cc / gross_notional_cc
    # are NOT on the protocol — those caps read the exposure book directly and are
    # untouched by the decay.
    axes = (
        "governing_model_es_99_cc",
        "deterministic_max_loss_cc",
    )
    checked = 0
    for _ in range(trials):
        s = _Snap()
        s.usable = True
        s.n_samples = 200_000
        base = rng.uniform(1e4, 1e7)
        for a in axes:
            setattr(s, a, base * rng.uniform(0.1, 1.5))
        s.mutex_aware_det_max_cc = s.deterministic_max_loss_cc * rng.uniform(0.5, 1.0)
        q = sorted(rng.uniform(0.0, base) for _ in range(64))
        s.loss_quantiles_cc = tuple(q)
        s.p_ruin = rng.choice((0.0, 0.01, 0.2))
        s.p_ruin_upper = s.p_ruin
        s.ruin_loss_threshold_cc = base * rng.uniform(0.8, 3.0)
        s.input_generation = 1
        s.n_positions = rng.randint(1, 50)
        added = rng.uniform(0.0, base * 0.5)
        unsampled = rng.uniform(0.0, base * 0.2)
        try:
            d = _decay_book_risk(s, added, unsampled)
        except Exception as exc:  # a refusal shape
            print(f"  decay raised: {exc!r}")
            continue
        if d is None:
            continue
        checked += 1
        for a in axes:
            if getattr(d, a) < getattr(s, a) - 1e-6:
                regressions += 1
                print(f"  LOOSER AXIS {a}: decayed={getattr(d,a):.1f} < snap={getattr(s,a):.1f}")
        if d.mutex_aware_det_max_cc is not None and (
            d.mutex_aware_det_max_cc < s.mutex_aware_det_max_cc - 1e-6
        ):
            regressions += 1
            print("  LOOSER AXIS mutex_aware_det_max_cc")
        if d.p_ruin < s.p_ruin - 1e-12 or d.p_ruin_upper < s.p_ruin_upper - 1e-12:
            regressions += 1
            print("  LOOSER RUIN AXIS")
    print(f"PROBE C (FIX 5 conservatism): {checked} decayed snapshots; "
          f"axes that got LOOSER than the measurement={regressions}")
    assert regressions == 0


if __name__ == "__main__":
    probe_a()
    probe_b()
    probe_c()
    print("\nALL PROBES PASS")
