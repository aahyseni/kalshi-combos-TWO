"""ADVERSARIAL ATTACK on FIX 5's REMOVAL assumption (2026-07-28).

``QuoteLifecycle._book_risk_decayed`` charges only for positions the snapshot
never saw and states, in its own comment:

    "REMOVED positions are ignored (dropping a position can only lower the loss)"

That claim is the one thing in the decay that is NOT trivially monotone, so it
gets its own attack. Book P&L is measured RELATIVE TO ENTRY, so a position that
WINS contributes POSITIVE P&L and therefore SUBTRACTS from the book's loss in
the states where it wins. Dropping such a position RAISES the loss in exactly
those states — the opposite of the comment's claim.

The attack: build a two-position book where position B almost always WINS (a NO
on a parlay that essentially never hits), snapshot it, then REMOVE B and compare
the snapshot's axes (which is what the decay carries forward unchanged, since
nothing was ADDED) against the TRUE risk of the surviving book {A}.

Runs the REAL ``compute_book_risk`` on both books. Read-only, no config, no I/O.
"""

from __future__ import annotations

import numpy as np

from combomaker.sim.book_model import BookModel
from combomaker.sim.book_risk import compute_book_risk
from combomaker.sim.engine import ComboPosition, LegModel


def _model(positions: tuple[ComboPosition, ...], ps: list[float]) -> BookModel:
    n = len(ps)
    eye = np.eye(n)
    return BookModel(
        legs=tuple(LegModel(p=p) for p in ps),
        positions=positions,
        corr_location_point=eye,
        corr_tail_stress_point=eye.copy(),
        corr_tail_stress_low=eye.copy(),
        corr_tail_stress_high=eye.copy(),
        leg_index={f"T{i}": i for i in range(n)},
        event_by_index={i: f"E{i}" for i in range(n)},
        unknown=False,
    )


def main() -> None:
    # Leg 0: hits ~90% of the time  -> A (long NO on it) usually LOSES.
    # Leg 1: hits ~0.5% of the time -> B (long NO on it) almost always WINS.
    ps = [0.90, 0.005]

    a = ComboPosition(
        leg_indices=(0,), side="no", contracts=100.0, price_cc=1000, leg_sides=("yes",)
    )
    b = ComboPosition(
        leg_indices=(1,), side="no", contracts=100.0, price_cc=9900, leg_sides=("yes",)
    )

    kw = dict(
        n_samples=200_000,
        seed=20260728,
        bankroll_cc=100_000_000,
        current_equity_cc=100_000_000,
        ruin_floor_frac=0.30,
    )

    before = compute_book_risk(_model((a, b), ps), **kw)   # snapshot: {A, B}
    after = compute_book_risk(_model((a,), ps), **kw)      # TRUE now:  {A}

    print("Book P&L is measured RELATIVE TO ENTRY, so a winning position offsets")
    print("the book's loss. Removing it can therefore RAISE the loss.\n")
    print(f"{'axis':<34}{'snapshot {A,B}':>18}{'TRUE now {A}':>16}{'understated by':>18}")
    print("-" * 86)
    worst = 0.0
    for axis in (
        "governing_model_es_99_cc",
        "es_99_cc",
        "deterministic_max_loss_cc",
        "mutex_aware_det_max_cc",
    ):
        s = getattr(before, axis, None)
        t = getattr(after, axis, None)
        if s is None or t is None:
            continue
        gap = t - s
        worst = max(worst, gap)
        print(f"{axis:<34}{s:>18,.0f}{t:>16,.0f}{gap:>18,.0f}")

    print()
    print("The DECAY charges 0 here (nothing was ADDED — only removed), so the")
    print("decayed view equals the snapshot column and is fed to the caps as if it")
    print("described the surviving book.")
    print()
    if worst > 0:
        print(f"RESULT: the decayed view UNDERSTATES the surviving book by "
              f"{worst:,.0f} cc = ${worst/10_000:,.2f} on its worst axis.")
    else:
        print("RESULT: no axis understated — the removal assumption holds here.")


if __name__ == "__main__":
    main()
