"""P0-1: the CANDIDATE P(ruin) equity basis must be COMMITTED-ONLY, not merged.

The audit's P0-1 defect: the candidate gate built the ruin equity basis from the
MERGED model (committed + reservations + candidate) cost basis, i.e.

    available_cash + Σ price·c over (committed + reservations + candidate).

But the reservation and candidate premiums have NOT yet been debited from
``available_cash``. The sampled POST book P&L already carries each combo's
``payout − price``. Adding the unpaid premium to the basis therefore cancels the
candidate/reservation cost twice:

    cash + cand_price + (cand_payout − cand_price) = cash + cand_payout

overstating post-candidate equity by exactly the premium and UNDER-stating
P(ruin). The fix builds the basis from COMMITTED modeled positions only:

    available_cash + Σ price·c over committed

so each reservation/candidate combo's sampled ``payout − price`` supplies its own
cost, yielding the CORRECT post-fill terminal equity

    cash + terminal_value(committed) + Σ_resv(payout − price)
        + cand_payout − cand_price.

These tests pin, all against the LIVE modules:

  1. THE EXACT IDENTITY (both candidate terminal outcomes, and with MULTIPLE
     outstanding reservations): for every sampled settlement scenario,
        committed_only_basis + post_book_pnl
          == cash + terminal_value(committed) + candidate_payout − candidate_premium
             (+ each reservation's payout − premium)
     to the cent — and the OLD merged basis overshoots it by exactly the unpaid
     reservation+candidate premiums (the double count).
  2. MONOTONICITY: at a FIXED payout distribution, candidate P(ruin) is
     non-decreasing in the candidate PRICE (a more expensive candidate can only
     make ruin more likely), and strictly increases across a floor-crossing price.
  3. The LIFECYCLE seam ``_build_candidate_gate_inputs`` now composes the basis
     from committed positions ONLY — the reservation and candidate premiums do
     NOT enter ``current_equity_cc`` (the regression against the exact defect).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from combomaker.core.conventions import Side
from combomaker.core.money import CC_PER_DOLLAR
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model, position_to_combo
from combomaker.sim.book_risk import (
    _book_pnl_from_values,
    _p_ruin_from_pnl,
    evaluate_candidate_book_risk,
    modeled_cost_basis_cc,
)
from combomaker.sim.engine import sample_leg_values


def _leg(ticker: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    price_cc: int = 5_000,
    risk_modeled: bool = True,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=our_side,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
        risk_modeled=risk_modeled,
    )


def _per_scenario_settle(
    values: np.ndarray, combo: object
) -> np.ndarray:
    """Σ payout (NOT P&L) per scenario for one ComboPosition, on ``values`` — the
    exact settlement leg of ``_position_pnl`` (payout − price) without the price,
    so the test reconstructs terminal value INDEPENDENTLY of the module's basis."""
    cols = values[:, list(combo.leg_indices)]  # type: ignore[attr-defined]
    if combo.leg_sides is not None:  # type: ignore[attr-defined]
        flip = np.array(
            [s == "no" for s in combo.leg_sides], dtype=bool  # type: ignore[attr-defined]
        )
        if flip.any():
            cols = np.where(flip[np.newaxis, :], 1.0 - cols, cols)
    prod = np.minimum(np.prod(cols, axis=1), 1.0) * float(CC_PER_DOLLAR)
    settle = prod if combo.side == "yes" else (float(CC_PER_DOLLAR) - prod)  # type: ignore[attr-defined]
    return settle * combo.contracts  # type: ignore[attr-defined]  # fee_cc == 0


# --------------------------------------------------------------------------- #
# (1) THE EXACT IDENTITY — one candidate, both outcomes, multiple reservations. #
# --------------------------------------------------------------------------- #


def test_committed_only_basis_reconciles_post_terminal_equity_exactly() -> None:
    """For EVERY sampled scenario:

        committed_only_basis + post_book_pnl
          == cash + terminal_value(committed)
             + Σ_reservations(payout − premium)
             + candidate_payout − candidate_premium

    to the cent. And the OLD merged basis overshoots by EXACTLY the unpaid
    reservation + candidate premiums (the double count P0-1 removes).

    Both candidate terminal outcomes are exercised: the candidate is a single-leg
    NO, so across the sampled scenarios its leg both HITS (payout 0 for the NO,
    P&L = −premium) and MISSES (payout $1, P&L = $1 − premium). The identity holds
    scenario-by-scenario, hence for both terminal states simultaneously.
    """
    available_cash_cc = 300_000

    committed = [
        _pos("c1", (_leg("A", "G1"),), our_side=Side.NO, contracts=100, price_cc=5_000),
        _pos("c2", (_leg("B", "G2"),), our_side=Side.YES, contracts=40, price_cc=3_000),
    ]
    reservations = [
        _pos("r1", (_leg("C", "G3"),), our_side=Side.NO, contracts=100, price_cc=4_000),
        _pos("r2", (_leg("D", "G4"),), our_side=Side.NO, contracts=60, price_cc=2_500),
    ]
    candidate = _pos(
        "cand", (_leg("E", "G5"),), our_side=Side.NO, contracts=100, price_cc=6_000
    )

    # COMMITTED-ONLY basis (the fix).
    committed_model = build_book_model(committed, marginals=lambda t: 0.6)
    committed_cost = modeled_cost_basis_cc(committed_model)
    committed_only_basis = available_cash_cc + committed_cost

    # MERGED model — the shared leg universe / sampled matrix the candidate gate
    # scores PRE and POST on (common random numbers), exactly as
    # ``evaluate_candidate_book_risk`` builds it.
    merged_positions = [*committed, *reservations, candidate]
    merged_model = build_book_model(merged_positions, marginals=lambda t: 0.6)
    leg_index = merged_model.leg_index

    committed_combos = [position_to_combo(p, leg_index) for p in committed]
    reservation_combos = [position_to_combo(p, leg_index) for p in reservations]
    cand_combo = position_to_combo(candidate, leg_index)
    post_combos = [*committed_combos, *reservation_combos, cand_combo]

    corr = merged_model.corr_for_band("high")
    values = sample_leg_values(
        merged_model.legs, corr, 20_000, np.random.default_rng(11)
    )
    post_book = _book_pnl_from_values(values, post_combos)

    # Independent per-scenario reconstruction of TRUE post-fill terminal equity:
    #   cash + Σ payout(committed) + Σ (payout − premium)(reservations)
    #        + candidate_payout − candidate_premium.
    committed_terminal = np.zeros(values.shape[0], dtype=np.float64)
    for cp in committed_combos:
        committed_terminal += _per_scenario_settle(values, cp)

    resv_pnl = np.zeros(values.shape[0], dtype=np.float64)
    for rp, r in zip(reservation_combos, reservations, strict=True):
        resv_pnl += _per_scenario_settle(values, rp) - float(r.max_loss_cc)

    cand_payout = _per_scenario_settle(values, cand_combo)
    cand_premium = float(candidate.max_loss_cc)

    true_terminal_equity = (
        available_cash_cc
        + committed_terminal
        + resv_pnl
        + cand_payout
        - cand_premium
    )

    # THE IDENTITY (to the cent, every scenario).
    np.testing.assert_allclose(
        committed_only_basis + post_book, true_terminal_equity, atol=1e-6
    )

    # Both candidate terminal outcomes are present in the sample (the single NO leg
    # both hits and misses), so the identity above covers both terminal states.
    assert np.any(cand_payout == 0.0)  # candidate leg HITS ⇒ NO pays 0
    assert np.any(cand_payout > 0.0)   # candidate leg MISSES ⇒ NO pays $1

    # The OLD (buggy) MERGED basis = cash + Σ price·c over committed+resv+cand,
    # i.e. it ADDS the unpaid reservation + candidate premiums. It overshoots the
    # true terminal equity by exactly those unpaid premiums, for every scenario.
    merged_cost = modeled_cost_basis_cc(merged_model)
    merged_basis = available_cash_cc + merged_cost
    unpaid_premiums = (
        sum(float(r.max_loss_cc) for r in reservations) + cand_premium
    )
    overshoot = (merged_basis + post_book) - true_terminal_equity
    np.testing.assert_allclose(overshoot, unpaid_premiums, atol=1e-6)
    assert unpaid_premiums > 0.0  # the double count is real and positive


def test_single_candidate_both_outcomes_identity_no_reservations() -> None:
    """The minimal audit case: one candidate, no reservations, BOTH terminal
    outcomes. Post terminal equity == cash + terminal_value(committed)
    + candidate_payout − candidate_premium, exactly, and the merged basis
    overshoots by exactly the candidate premium."""
    available_cash_cc = 150_000
    committed = [
        _pos("c1", (_leg("A", "G1"),), our_side=Side.NO, contracts=100, price_cc=5_000)
    ]
    candidate = _pos(
        "cand", (_leg("B", "G2"),), our_side=Side.NO, contracts=100, price_cc=4_000
    )

    committed_model = build_book_model(committed, marginals=lambda t: 0.5)
    committed_only_basis = available_cash_cc + modeled_cost_basis_cc(committed_model)

    merged_model = build_book_model(
        [*committed, candidate], marginals=lambda t: 0.5
    )
    leg_index = merged_model.leg_index
    committed_combos = [position_to_combo(p, leg_index) for p in committed]
    cand_combo = position_to_combo(candidate, leg_index)
    post_combos = [*committed_combos, cand_combo]

    corr = merged_model.corr_for_band("high")
    values = sample_leg_values(
        merged_model.legs, corr, 20_000, np.random.default_rng(4)
    )
    post_book = _book_pnl_from_values(values, post_combos)

    committed_terminal = np.zeros(values.shape[0], dtype=np.float64)
    for cp in committed_combos:
        committed_terminal += _per_scenario_settle(values, cp)
    cand_payout = _per_scenario_settle(values, cand_combo)
    cand_premium = float(candidate.max_loss_cc)

    true_terminal = (
        available_cash_cc + committed_terminal + cand_payout - cand_premium
    )
    np.testing.assert_allclose(
        committed_only_basis + post_book, true_terminal, atol=1e-6
    )

    # Both outcomes present.
    assert np.any(cand_payout == 0.0)
    assert np.any(cand_payout > 0.0)

    merged_basis = available_cash_cc + modeled_cost_basis_cc(merged_model)
    overshoot = (merged_basis + post_book) - true_terminal
    np.testing.assert_allclose(overshoot, cand_premium, atol=1e-6)


# --------------------------------------------------------------------------- #
# (2) MONOTONICITY: P(ruin) increases with candidate PRICE at fixed payout.     #
# --------------------------------------------------------------------------- #


def test_candidate_p_ruin_increases_with_price_at_fixed_payout() -> None:
    """At a FIXED payout distribution (same leg, same marginal ⇒ identical sampled
    payouts), a more EXPENSIVE candidate can only make P(ruin) LARGER — because
    with the correct committed-only basis its premium is a pure subtraction from
    every scenario's terminal equity. Monotone non-decreasing overall, and STRICT
    across a price that pushes a loss cluster through the ruin floor."""
    # Fixed committed book + a single-leg NO candidate. Payout distribution is set
    # by the marginal (0.85 ⇒ the NO forfeits its premium 85% of the time) and does
    # NOT depend on the candidate's PRICE — only the P&L subtracts the price.
    committed = [
        _pos("c1", (_leg("A", "G1"),), our_side=Side.NO, contracts=100, price_cc=5_000)
    ]
    marginal = 0.85

    # Cash chosen so the ruin floor sits inside the range the candidate premium
    # sweeps: a bigger premium drops more scenarios under the floor.
    bankroll_cc = 100_000
    available_cash_cc = 66_000  # floor = 70_000cc ⇒ small cushion above it

    p_ruins: list[float] = []
    prices = [1_000, 3_000, 5_000, 7_000, 9_000]
    for price_cc in prices:
        cand = _pos(
            "cand", (_leg("B", "G2"),), our_side=Side.NO, contracts=100,
            price_cc=price_cc,
        )
        # Committed-only basis (the fix): cash + committed cost ONLY.
        committed_model = build_book_model(committed, marginals=lambda t: marginal)
        basis = available_cash_cc + int(round(modeled_cost_basis_cc(committed_model)))
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: marginal,
            n_samples=40_000,
            seed=9,
            bankroll_cc=bankroll_cc,
            current_equity_cc=basis,
            ruin_floor_frac=0.70,
            portfolio_ruin_prob_budget=1.0,  # do not let the gate decline; read p_ruin
        )
        p_ruins.append(r.post.p_ruin)

    # Non-decreasing across the whole price sweep (fixed payout distribution).
    for lo, hi in zip(p_ruins, p_ruins[1:], strict=False):
        assert hi >= lo - 1e-12, f"p_ruin fell as price rose: {p_ruins}"
    # And strictly higher at the top of the sweep than the bottom (the premium
    # genuinely pushes scenarios through the floor).
    assert p_ruins[-1] > p_ruins[0], f"p_ruin did not rise with price: {p_ruins}"


def test_p_ruin_strictly_higher_on_merged_vs_committed_basis() -> None:
    """DIRECT bug reproduction: on the SAME sampled book, the OLD merged basis
    yields a STRICTLY LOWER P(ruin) than the correct committed-only basis, because
    the unpaid candidate+reservation premiums inflate the equity floor cushion.
    The fix (committed-only) reports the higher, correct ruin probability."""
    available_cash_cc = 66_000
    ruin_floor_cc = 0.70 * 100_000  # 70_000cc

    committed = [
        _pos("c1", (_leg("A", "G1"),), our_side=Side.NO, contracts=100, price_cc=5_000)
    ]
    reservations = [
        _pos("r1", (_leg("C", "G3"),), our_side=Side.NO, contracts=100, price_cc=4_000)
    ]
    candidate = _pos(
        "cand", (_leg("E", "G5"),), our_side=Side.NO, contracts=100, price_cc=6_000
    )

    committed_model = build_book_model(committed, marginals=lambda t: 0.85)
    committed_basis = available_cash_cc + int(
        round(modeled_cost_basis_cc(committed_model))
    )

    merged_positions = [*committed, *reservations, candidate]
    merged_model = build_book_model(merged_positions, marginals=lambda t: 0.85)
    merged_basis = available_cash_cc + int(round(modeled_cost_basis_cc(merged_model)))

    leg_index = merged_model.leg_index
    post_combos = [position_to_combo(p, leg_index) for p in merged_positions]
    corr = merged_model.corr_for_band("high")
    values = sample_leg_values(
        merged_model.legs, corr, 40_000, np.random.default_rng(2)
    )
    post_book = _book_pnl_from_values(values, post_combos)

    p_committed = _p_ruin_from_pnl(post_book, committed_basis, ruin_floor_cc)
    p_merged = _p_ruin_from_pnl(post_book, merged_basis, ruin_floor_cc)

    # The committed-only basis reports the HIGHER (correct, un-inflated) ruin; the
    # merged basis UNDER-states it by crediting the unpaid premiums into equity.
    assert merged_basis > committed_basis  # merged inflates the cushion
    assert p_committed > p_merged, (p_committed, p_merged)


# --------------------------------------------------------------------------- #
# (3) LIFECYCLE seam: the basis is composed from COMMITTED positions ONLY.       #
# --------------------------------------------------------------------------- #


async def test_build_candidate_gate_inputs_basis_is_committed_only(
    tmp_path: Path,
) -> None:
    """The regression at the exact defect site. The ``CandidateBookRiskInputs``
    shipped off-loop by ``_build_candidate_gate_inputs`` must carry
    ``current_equity_cc`` = cash + cost_basis(COMMITTED only). The CANDIDATE fill's
    premium must NOT appear in it — the pre-fix merged-model basis would have added
    it. Proven by capturing the real shipped inputs via the stub pool and comparing
    to the committed-only basis computed from the LIVE exposure book, AND showing
    the merged basis (which the bug used) is strictly larger."""
    from combomaker.risk.balance import BalanceTracker
    from tests.test_candidate_gate_wiring import _make_rig
    from tests.test_lifecycle import TEST_CONVENTIONS
    from tests.test_lifecycle import rfq as _rfq

    rig = await _make_rig(tmp_path)
    lifecycle = rig.lifecycle

    # Balance tracker with a known available cash so the basis is defined.
    class _Src:
        async def get_balance(self) -> dict[str, int]:
            return {"balance": 800000, "portfolio_value": 5000}  # $8000 cash

    bt = BalanceTracker(
        TEST_CONVENTIONS, rig.h.clock, stale_after_s=1_000.0
    )
    await bt.refresh(_Src())
    cash_cc = bt.available_cash_cc_or_none()
    assert cash_cc is not None

    # Open a quote FIRST (no balance wired yet, so the R2 %-cap layer stays inactive
    # and the quote opens normally), then pull its state. The seam under test
    # (``_build_candidate_gate_inputs``) is called directly afterward — we are not
    # testing the accept-time quote gate here, only the ruin-basis composition.
    await rig.lifecycle.handle_rfq(_rfq())

    # Now wire the balance tracker so the ruin basis is defined, and seed a
    # COMMITTED position on a market with a live book (M1) so it is risk-modeled
    # with a resolvable marginal.
    lifecycle._balance = bt  # noqa: SLF001
    committed_pos = OpenPosition(
        position_id="held:c1",
        combo_ticker="COMBO-C1",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=5_000,  # type: ignore[arg-type]
        legs=(LegRef("M1", "E1", "yes"),),
    )
    rig.exposure.add_position(committed_pos)

    # COMMITTED-ONLY basis computed independently from the seeded book.
    committed_now = tuple(rig.exposure.positions.values())
    committed_model = build_book_model(
        committed_now,
        marginals=lifecycle._marginals,  # noqa: SLF001
        within_game_rho=lifecycle._within_game_rho,  # noqa: SLF001
    )
    expected_basis = int(cash_cc) + int(round(modeled_cost_basis_cc(committed_model)))

    # Set a pending fill on the open quote and call the seam DIRECTLY so the
    # CandidateBookRiskInputs are built by the real, unmodified
    # ``_build_candidate_gate_inputs`` from a live committed book + a candidate fill.
    open_quotes = lifecycle._open  # noqa: SLF001
    assert len(open_quotes) == 1, "expected exactly one open quote to build from"
    quote_id, state = next(iter(open_quotes.items()))
    state.pending_fill = (
        Side.YES,
        state.constructed.yes_bid_cc,
        state.risk_qty,
    )
    inputs = lifecycle._build_candidate_gate_inputs(quote_id, state)  # noqa: SLF001

    # The shipped ruin basis is COMMITTED-ONLY: it EXCLUDES the candidate premium.
    assert inputs.current_equity_cc == expected_basis
    assert inputs.committed == committed_now  # the committed set it was built on

    # And the pre-fix MERGED basis (committed + the built candidate) would have been
    # STRICTLY LARGER by the candidate premium — so the choice is load-bearing.
    candidate = inputs.candidate
    merged_model = build_book_model(
        [*committed_now, candidate],
        marginals=lifecycle._marginals,  # noqa: SLF001
        within_game_rho=lifecycle._within_game_rho,  # noqa: SLF001
    )
    merged_basis = lifecycle._ruin_equity_basis_cc(merged_model)  # noqa: SLF001
    assert merged_basis is not None
    cand_premium = int(round(modeled_cost_basis_cc(build_book_model(
        [candidate],
        marginals=lifecycle._marginals,  # noqa: SLF001
        within_game_rho=lifecycle._within_game_rho,  # noqa: SLF001
    ))))
    assert cand_premium > 0  # the candidate is risk-modeled with a real premium
    assert merged_basis > expected_basis  # the double count the fix removes
    assert merged_basis - expected_basis == cand_premium
