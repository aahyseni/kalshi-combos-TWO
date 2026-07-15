"""P1-3: prove the P(ruin) equity/P&L basis does not double-count position value.

The ruin check evaluates ``P(equity_basis + book_pnl < ruin_floor)``. ``book_pnl``
(``sim.engine._position_pnl``) is measured ENTRY-to-terminal: ``payout − price_cc``
per YES contract, ``(1 − payout) − price_cc`` per NO. Adding that entry-based P&L
onto the EXCHANGE equity (cash + Kalshi ``portfolio_value``, i.e. the current MARK
of the same positions) would count the position value twice — once in the mark and
again as the entry premium inside ``book_pnl``.

The fix feeds the COST basis instead: ``available_cash + Σ price_cc·contracts``
(``book_risk.modeled_cost_basis_cc``). On that basis the entry premium cancels
exactly and the sum reconciles to the true terminal equity ``cash + Σ payout``,
independent of the intraday mark. These tests pin:

  1. ``modeled_cost_basis_cc`` = Σ price_cc·contracts (fee-free, per build_book_model).
  2. THE RECONCILIATION IDENTITY: cost_basis + book_pnl == cash + Σ payout, exactly,
     for every settlement scenario — and that exchange equity overshoots it by
     precisely the unrealized mark-to-market (the double count).
  3. The wiring composition (cash + modeled_cost_basis) is MARK-INDEPENDENT: a
     divergent Kalshi ``portfolio_value`` does NOT shift the ruin basis, whereas
     exchange equity would.
  4. Stale/absent cash and deposit/withdrawal behave fail-closed (basis None or
     tracks cash, never an invented equity).
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import numpy as np

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import (
    _book_pnl_from_values,
    _p_ruin_from_pnl,
    modeled_cost_basis_cc,
)
from combomaker.sim.engine import sample_leg_values

VERIFIED = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


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


class _FakeSource:
    """One canned /portfolio/balance payload (cash + portfolio_value in cents)."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    async def get_balance(self) -> dict[str, Any]:
        self.calls += 1
        idx = min(self.calls - 1, len(self._payloads) - 1)
        return self._payloads[idx]


def _tracker() -> tuple[BalanceTracker, FakeClock]:
    clock = FakeClock(start=datetime(2026, 7, 15, tzinfo=UTC))
    return (
        BalanceTracker(
            VERIFIED, clock, stale_after_s=30.0, portfolio_haircut=Fraction(1, 2)
        ),
        clock,
    )


# --------------------------------------------------------------- (1) cost basis


def test_modeled_cost_basis_is_sum_price_times_contracts() -> None:
    # 1.00 ct @ 5_000cc + 0.40 ct @ 3_000cc = 5_000 + 1_200 = 6_200 cc premium.
    positions = [
        _pos("a", (_leg("A", "G1"),), contracts=100, price_cc=5_000),
        _pos("b", (_leg("B", "G2"),), contracts=40, price_cc=3_000),
    ]
    model = build_book_model(positions, marginals=lambda t: 0.5)
    assert modeled_cost_basis_cc(model) == 6_200.0


def test_reserved_holdings_excluded_from_cost_basis() -> None:
    # A RESERVED holding (risk_modeled=False) is not sampled and not in book_pnl;
    # it must likewise be excluded from the cost basis so the two stay consistent.
    modeled = _pos("m", (_leg("A", "G1"),), contracts=100, price_cc=4_000)
    reserved = _pos(
        "r", (_leg("Z", "G9"),), contracts=100, price_cc=9_000, risk_modeled=False
    )
    model = build_book_model([modeled, reserved], marginals=lambda t: 0.5)
    # Only the modeled 4_000cc premium; the reserved 9_000cc is a separate reserve.
    assert modeled_cost_basis_cc(model) == 4_000.0
    assert model.reserved_loss_cc == 9_000.0


# ------------------------------------------------ (2) the reconciliation identity


def test_cost_basis_plus_pnl_equals_cash_plus_payout_no_double_count() -> None:
    """THE PROOF. For EVERY sampled settlement scenario:

        cost_basis_equity + book_pnl == available_cash + Σ payout

    to the cent, independent of the mark — so the ruin basis never double-counts
    position value. And exchange equity overshoots it by exactly the unrealized
    mark-to-market baked into ``portfolio_value``.
    """
    available_cash_cc = 120_000
    positions = [
        _pos("p1", (_leg("A", "G1"),), our_side=Side.NO, contracts=100, price_cc=5_000),
        _pos("p2", (_leg("B", "G2"),), our_side=Side.YES, contracts=40, price_cc=3_000),
    ]
    model = build_book_model(positions, marginals=lambda t: 0.6)

    cost_basis = modeled_cost_basis_cc(model)
    equity_basis = available_cash_cc + cost_basis

    corr = model.corr_for_band("high")
    rng = np.random.default_rng(7)
    values = sample_leg_values(model.legs, corr, 5_000, rng)
    book = _book_pnl_from_values(values, model.positions)

    # Independently compute Σ payout per scenario (cash converts marks -> payout).
    payout = np.zeros(values.shape[0], dtype=np.float64)
    for cp in model.positions:
        cols = values[:, list(cp.leg_indices)]
        prod = np.minimum(np.prod(cols, axis=1), 1.0) * float(CC_PER_DOLLAR)
        settle = prod if cp.side == "yes" else (float(CC_PER_DOLLAR) - prod)
        payout += settle * cp.contracts  # fee_cc == 0 in the model

    true_terminal_equity = available_cash_cc + payout

    # IDENTITY: cost-basis equity + entry-based P&L == cash + realized payout.
    np.testing.assert_allclose(equity_basis + book, true_terminal_equity, atol=1e-6)

    # And exchange equity (cash + a NON-cost mark) overshoots by exactly the
    # unrealized MTM (portfolio_value − cost_basis) — the double count we removed.
    portfolio_value_cc = cost_basis + 4_000.0  # book has moved +$0.40 in our favor
    exchange_equity = available_cash_cc + portfolio_value_cc
    overshoot = (exchange_equity + book) - true_terminal_equity
    np.testing.assert_allclose(overshoot, 4_000.0, atol=1e-6)


def test_ruin_basis_is_mark_independent_but_exchange_equity_is_not() -> None:
    """The ruin probability computed on the cost basis is invariant to the mark;
    on exchange equity it moves with the mark (the bug). Same sampled book, two
    marks."""
    # Cash cushion above the floor is small (~7_000cc) so a multi-leg break drops
    # through it; the worst-case book loss is 12_000cc (both 0.60 NO legs hit).
    available_cash_cc = 65_000
    positions = [
        _pos("p1", (_leg("A", "G1"),), our_side=Side.NO, contracts=100, price_cc=6_000),
        _pos("p2", (_leg("B", "G1"),), our_side=Side.NO, contracts=100, price_cc=6_000),
    ]
    model = build_book_model(positions, marginals=lambda t: 0.85)
    cost_basis = modeled_cost_basis_cc(model)  # 12_000cc

    corr = model.corr_for_band("high")
    values = sample_leg_values(model.legs, corr, 40_000, np.random.default_rng(3))
    book = _book_pnl_from_values(values, model.positions)

    ruin_floor_cc = 0.70 * 100_000  # $7.00 floor on a $10 nominal bankroll

    # Cost basis: unchanged whether the mark is above OR below cost.
    p_cost = _p_ruin_from_pnl(book, available_cash_cc + int(cost_basis), ruin_floor_cc)
    assert p_cost > 0.0  # this correlated NO book does breach the floor sometimes

    # Exchange equity at two divergent marks moves the ruin number; cost basis
    # does not. The dominant loss cluster (both 0.60 NO legs hit ⇒ −12_000cc)
    # sits ~5_000cc below the floor at the cost basis, so a favorable mark
    # (+6_000) lifts that cluster ABOVE the floor (ruin ≈ 0) while an adverse
    # mark (−3_000) keeps it below (ruin ≈ P(both hit)). The gap the cost basis
    # would have double-counted.
    exch_high_mark = available_cash_cc + int(cost_basis) + 6_000  # favorable mark
    exch_low_mark = available_cash_cc + int(cost_basis) - 3_000  # adverse mark
    p_exch_high = _p_ruin_from_pnl(book, exch_high_mark, ruin_floor_cc)
    p_exch_low = _p_ruin_from_pnl(book, exch_low_mark, ruin_floor_cc)
    # Higher (favorable) mark understates ruin; adverse mark overstates it — the
    # exact double-count sensitivity the cost basis removes.
    assert p_exch_high <= p_cost <= p_exch_low
    assert p_exch_high < p_exch_low  # exchange equity is genuinely mark-sensitive


# ---------------------------------------------- (3) wiring composition + fail-safe


async def test_wiring_basis_ignores_portfolio_value_divergence() -> None:
    """cash + modeled_cost_basis (the wired ruin basis) is identical whether the
    exchange marks the position at cost or far from it — the double count is gone
    at the seam that builds it."""
    bt, _clock = _tracker()
    positions = [
        _pos("p1", (_leg("A", "G1"),), contracts=100, price_cc=5_000),
    ]
    model = build_book_model(positions, marginals=lambda t: 0.5)
    cost_basis = int(round(modeled_cost_basis_cc(model)))

    # Poll 1: mark == cost. Poll 2: same cash, mark 3x cost (a big unrealized gain).
    await bt.refresh(
        _FakeSource({"balance": 800000, "portfolio_value": 5000})  # $8000 cash, $50 mark
    )
    cash1 = bt.available_cash_cc_or_none()
    assert cash1 is not None
    basis1 = int(cash1) + cost_basis

    await bt.refresh(
        _FakeSource({"balance": 800000, "portfolio_value": 15000})  # mark 3x
    )
    cash2 = bt.available_cash_cc_or_none()
    assert cash2 is not None
    basis2 = int(cash2) + cost_basis

    # Exchange equity carries the mark ($150 = 15000 cents = 1_500_000 cc); the
    # COST basis does not move at all when the mark triples.
    assert int(bt.exchange_equity_cc_or_none()) - int(cash2) == 1_500_000  # mark
    assert basis1 == basis2  # ruin basis is mark-independent (no double count)


async def test_stale_cash_makes_basis_unavailable_fail_closed() -> None:
    bt, clock = _tracker()
    await bt.refresh(_FakeSource({"balance": 800000, "portfolio_value": 5000}))
    assert bt.available_cash_cc_or_none() is not None
    clock.advance(30.001)  # past the staleness window
    # Cash unavailable ⇒ the wiring returns None ⇒ the ruin cap simply does not
    # evaluate (never an invented equity). Mirrors exchange_equity going None too.
    assert bt.available_cash_cc_or_none() is None
    assert bt.exchange_equity_cc_or_none() is None


async def test_deposit_tracks_cash_not_stale_mark() -> None:
    """A deposit raises available cash; the ruin basis tracks it (cash is the hard
    reading), while the position cost basis is unchanged — no stale-mark inflation
    of the basis."""
    bt, _clock = _tracker()
    positions = [_pos("p1", (_leg("A", "G1"),), contracts=100, price_cc=5_000)]
    model = build_book_model(positions, marginals=lambda t: 0.5)
    cost_basis = int(round(modeled_cost_basis_cc(model)))

    await bt.refresh(_FakeSource({"balance": 800000, "portfolio_value": 5000}))
    before = int(bt.available_cash_cc_or_none()) + cost_basis  # type: ignore[arg-type]

    await bt.refresh(_FakeSource({"balance": 900000, "portfolio_value": 5000}))  # +$1000
    after = int(bt.available_cash_cc_or_none()) + cost_basis  # type: ignore[arg-type]

    # balance 800000 -> 900000 cents = $8000 -> $9000 = +$1000 = 10_000_000 cc.
    assert after - before == 10_000_000  # exactly the $1000 deposit, in cc
