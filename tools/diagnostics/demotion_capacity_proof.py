"""CAPACITY PROOF for the RATIFIED det-max demotion (operator 2026-07-31;
reading ratified 2026-08-01, verbatim: "Reading b" — the ruin anchor is an
EQUITY FLOOR, backstop = (1 - 0.30) x bankroll; see
``combomaker.risk.cap_family.det_max_backstop_frac``).

Rule 8 (testing isolation): imports and DRIVES the live ``LimitChecker`` +
``compute_book_risk`` (via the helpers in ``kill_anchor_live_book``) and edits
nothing. The store is opened READ-ONLY. Nothing here touches the bot.

PORT NOTE (2026-08-01): the original file was staged untracked in the
kct-reanchor worktree and its source was lost with the burst-floor cleanup;
this is a faithful reconstruction from its compiled bytecode
(demotion_capacity_proof.cpython-313.pyc, 2026-07-31 18:27) — same functions,
same constants, same output format.

WHAT IT ANSWERS (the three halves of Task A step 3):

  1. ADMITTED PREMIUM today vs ARMED (KILL gate governing + det-max demoted to
     the ruin-anchor backstop), at n = the LIVE book and along a synthetic
     diversified growth path through n = 20 and n = 46 — the operator's
     thesis: "if we raise to 2k we'd get a very diverse and profitable book".
  2. THE SMALL-BOOK REGIME, honestly: below n ~ 15 the KILL-line hit count is
     lattice-quantized (at n = 8, P(>=6 lose) = 1.13% and P(>=5) = 5.80% at
     p = 0.30 — the 2% budget falls INSIDE the gap), so the armed gate binds
     TIGHTER than today's dollar wall on tiny CONCENTRATED books. That is the
     gate working, not a defect — but it must be said, not discovered.
  3. THE CHEAP-NO QUESTION re-examined UNDER the demotion: the pre-demotion
     verdict called the armed gate "a structural cheap-NO tax (monotone in
     p_hit)". With det-max demoted that can invert for DIVERSIFIED books
     (binomial tails thin fast at large n). Measured both directions at
     n = 46, prices 25/40/50/70/85/92 cents.

Method: for each (n, price) the book is n equal-premium single-leg long-NO
tickets on n INDEPENDENT games; per-ticket premium is bisected to the largest
value each regime admits (LimitChecker verdict off a fresh 40k-sample MC
snapshot — the LIVE mechanism, not an analytic shortcut). "today" = the
enforced dollar regime (det-max 0.36); "armed" = kill_anchored_book_gate True
(KILL gate governs, det-max = the ruin-anchor backstop 0.70)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO / "src"), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.core.conventions import Side  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.risk.cap_family import det_max_backstop_frac  # noqa: E402
from combomaker.risk.exposure import LegRef, OpenPosition  # noqa: E402
from combomaker.risk.limits import threshold_cc  # noqa: E402
from tools.diagnostics.kill_anchor_live_book import (  # noqa: E402
    DB_DEFAULT,
    DET_MAX_FRAC,
    KILL_FRAC,
    _limits,
    implied_marginals,
    load_positions,
    measure,
    p_kill,
    portfolio_breaches,
)


def synth_book(n: int, premium_each_cc: int, price_cc: int) -> list[OpenPosition]:
    contracts = max(1, premium_each_cc * 100 // price_cc)
    return [
        OpenPosition(
            position_id=f"s{i}",
            combo_ticker=f"SYN-{i}",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(contracts),
            entry_price_cc=price_cc,  # type: ignore[arg-type]
            legs=(
                LegRef(f"SL{i}", f"KXSG-{i}", "yes"),
            ),
        )
        for i in range(n)
    ]


def admitted(book, p_hit, bank, armed, samples, seed) -> bool:
    marg = {leg.market_ticker: p_hit for p in book for leg in p.legs}
    snap = measure(book, marg, bank, samples, seed)
    return not portfolio_breaches(_limits(armed=armed), snap, bank)


def max_premium(n, price_cc, bank, armed, samples, seed):
    """Largest per-ticket premium the regime admits (bisection, 12 rounds) and
    the book P(KILL) at that stop. p_hit = the price-implied hit probability
    (1 - P), the same proxy the live-book study uses."""
    p_hit = max(0.0001, min(0.9999, 1.0 - price_cc / 10000.0))
    lo, hi = 0.0, float(bank) / n
    for _ in range(12):
        mid = 0.5 * (lo + hi)
        if admitted(
            synth_book(n, int(mid), price_cc), p_hit, bank, armed,
            samples, seed,
        ):
            lo = mid
        else:
            hi = mid
    stop_book = synth_book(n, int(lo), price_cc)
    marg = {leg.market_ticker: p_hit for p in stop_book for leg in p.legs}
    snap = measure(stop_book, marg, bank, samples, seed)
    kill_line = threshold_cc(KILL_FRAC, bank)
    return (lo * n, p_kill(snap, kill_line))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--bankroll-cc", type=int, default=29985806,
        help="live exchange_equity_cc (2026-07-31 17:23 boot)",
    )
    ap.add_argument("--samples", type=int, default=40000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    args = ap.parse_args()
    bank = args.bankroll_cc

    wall = threshold_cc(DET_MAX_FRAC, bank)
    backstop = threshold_cc(det_max_backstop_frac(), bank)
    kill_line = threshold_cc(KILL_FRAC, bank)
    print("=" * 78)
    print("DEMOTION CAPACITY PROOF - armed = KILL gate governs, det-max = backstop")
    print("=" * 78)
    print(
        f"bankroll ${bank / 10000.0:,.2f} | KILL line ${kill_line / 10000.0:,.2f}"
        f" | today's wall ${wall / 10000.0:,.2f} (0.36) | backstop "
        f"${backstop / 10000.0:,.2f} ({det_max_backstop_frac()})"
    )

    live = load_positions(args.db)
    if live:
        marg = implied_marginals(live)
        snap = measure(live, marg, bank, args.samples, args.seed)
        prem = sum(p.max_loss_cc for p in live)
        pk = p_kill(snap, kill_line)
        today = portfolio_breaches(_limits(armed=False), snap, bank)
        armed_b = portfolio_breaches(_limits(armed=True), snap, bank)
        print(
            f"\n--- LIVE BOOK: n={len(live)} premium ${prem / 10000.0:,.2f}"
            f" P(KILL)={pk:.4f}"
        )
        print(
            f"    today: {'ADMITTED' if not today else sorted(today)}"
            f" | armed: {'ADMITTED' if not armed_b else sorted(armed_b)}"
        )
        print(
            f"    headroom to next ticket: today "
            f"${max(0, wall - prem) / 10000.0:,.2f} | armed "
            f"${max(0, backstop - prem) / 10000.0:,.2f}"
            f" (if P(KILL) stays inside budget)"
        )
    else:
        print("\n--- LIVE BOOK: empty position ledger")

    print("\n--- SYNTHETIC DIVERSIFIED BOOKS (equal tickets, independent games)")
    print(
        f"{'n':>4} {'price':>6} {'today $':>10} {'armed $':>10} "
        f"{'ratio':>6} {'P(KILL)@armed stop':>19}"
    )
    for n, price in ((8, 5000), (20, 5000), (46, 5000)):
        t_prem, _ = max_premium(n, price, bank, False, args.samples, args.seed)
        a_prem, a_pk = max_premium(n, price, bank, True, args.samples, args.seed)
        print(
            f"{n:>4} {price / 100:>5.0f}c ${t_prem / 10000.0:>9,.2f} "
            f"${a_prem / 10000.0:>9,.2f} {a_prem / max(1, t_prem):>6.2f} "
            f"{a_pk:>19.4f}"
        )

    print("\n--- CHEAP-NO RE-EXAM UNDER THE DEMOTION (n=46, price sweep)")
    print(
        f"{'price':>6} {'p_hit':>6} {'today $':>10} {'armed $':>10} "
        f"{'ratio':>6} {'P(KILL)@stop':>13}"
    )
    for price in (2500, 4000, 5000, 7000, 8500, 9200):
        t_prem, _ = max_premium(46, price, bank, False, args.samples, args.seed)
        a_prem, a_pk = max_premium(46, price, bank, True, args.samples, args.seed)
        print(
            f"{price / 100:>5.0f}c {1 - price / 10000:>6.3f} "
            f"${t_prem / 10000.0:>9,.2f} ${a_prem / 10000.0:>9,.2f} "
            f"{a_prem / max(1, t_prem):>6.2f} {a_pk:>13.4f}"
        )

    print(
        "\nratio > 1.00 = the demotion ADMITS more than today's wall at that "
        "shape;\nratio < 1.00 = the KILL gate binds tighter than today "
        "(small/concentrated regime)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
