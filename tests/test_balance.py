"""risk.balance.BalanceTracker — live bankroll (fail-closed on stale) + a
realized-P&L ledger advanced on settlement.

Fakes only: a ``FakeClock`` drives staleness deterministically and a
``FakeBalanceSource`` returns canned /portfolio/balance payloads — no live
credentials, ever. The credit-on-NO-settle case is anchored to the 2026-07-10
demo ground truth (LONG NO 1.00 ct paid $0.50, settled NO, paid $1.00,
balance 1082.62 -> 1083.62, realized +$0.50).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.balance import (
    BalanceParseError,
    BalanceTracker,
    Settlement,
    StaleBalanceError,
    _no_payout_per_contract_cc,
    _parse_balance_cc,
    _parse_portfolio_value_cc,
)

CC = CentiCents
Q = CentiContracts

VERIFIED = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# combo_no_pays_complement unverified (None): a NO credit must refuse.
NO_UNVERIFIED = Conventions(
    verified=True,
    source="test-no-unverified",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=None,
)


class FakeBalanceSource:
    """Canned /portfolio/balance payloads, one per poll (last repeats)."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    async def get_balance(self) -> dict[str, Any]:
        self.calls += 1
        idx = min(self.calls - 1, len(self._payloads) - 1)
        return self._payloads[idx]


def tracker(
    conventions: Conventions = VERIFIED,
    *,
    stale_after_s: float = 30.0,
    portfolio_haircut: Fraction = Fraction(1, 2),
) -> tuple[BalanceTracker, FakeClock]:
    clock = FakeClock()
    return (
        BalanceTracker(
            conventions,
            clock,
            stale_after_s=stale_after_s,
            portfolio_haircut=portfolio_haircut,
        ),
        clock,
    )


# --- balance payload parsing -------------------------------------------------


class TestParseBalance:
    def test_prefers_exact_dollars_string(self) -> None:
        assert _parse_balance_cc({"balance": 500000, "balance_dollars": "5000.00"}) == CC(
            50_000_000
        )

    def test_falls_back_to_int_cents(self) -> None:
        # 500000 cents = $5000.00 = 50_000_000 cc.
        assert _parse_balance_cc({"balance": 500000}) == CC(50_000_000)

    def test_demo_balance_to_the_cent(self) -> None:
        # $1082.62 -> 108262 cents -> 10_826_200 cc, exact.
        assert _parse_balance_cc({"balance": 108262}) == CC(10_826_200)

    def test_missing_both_fields_raises(self) -> None:
        with pytest.raises(BalanceParseError):
            _parse_balance_cc({"portfolio_value": 1})

    def test_bool_is_not_int_cents(self) -> None:
        with pytest.raises(BalanceParseError):
            _parse_balance_cc({"balance": True})


# --- live bankroll + staleness ----------------------------------------------


class TestBankrollPollAndStaleness:
    async def test_fresh_after_refresh(self) -> None:
        bt, _clock = tracker()
        assert bt.is_stale  # no poll yet
        src = FakeBalanceSource(
            {"balance_dollars": "5000.00", "portfolio_value": 0}
        )
        got = await bt.refresh(src)
        assert got == CC(50_000_000)
        assert not bt.is_stale
        assert bt.bankroll_cc == CC(50_000_000)
        assert bt.available_cash_cc == CC(50_000_000)
        assert bt.bankroll_cc_or_none() == CC(50_000_000)

    async def test_goes_stale_after_window(self) -> None:
        bt, clock = tracker(stale_after_s=30.0)
        await bt.refresh(FakeBalanceSource({"balance": 500000, "portfolio_value": 0}))
        clock.advance(30.0)
        assert not bt.is_stale  # exactly at the window is still fresh
        clock.advance(0.001)
        assert bt.is_stale
        assert bt.bankroll_cc_or_none() is None
        with pytest.raises(StaleBalanceError):
            _ = bt.bankroll_cc

    async def test_no_poll_is_stale_and_fails_closed(self) -> None:
        bt, _clock = tracker()
        assert bt.is_stale
        with pytest.raises(StaleBalanceError):
            _ = bt.bankroll_cc

    async def test_bad_poll_does_not_overwrite_good_value(self) -> None:
        # A parse failure must leave the last good bankroll AND its freshness
        # stamp untouched (a bad poll is not a fresh reading).
        bt, clock = tracker(stale_after_s=30.0)
        await bt.refresh(FakeBalanceSource({"balance": 500000, "portfolio_value": 0}))
        clock.advance(10.0)
        with pytest.raises(BalanceParseError):
            await bt.refresh(FakeBalanceSource({"garbage": 1}))
        # still the good value, and its ORIGINAL stamp (age is now 10s < 30s).
        assert bt.bankroll_cc == CC(50_000_000)
        clock.advance(20.001)  # 30.001s since the GOOD poll
        assert bt.is_stale


# --- realized-P&L ledger -----------------------------------------------------


class TestRealizedLedger:
    def test_no_settle_miss_credits_one_dollar_demo_ground_truth(self) -> None:
        # THE ground-truth assertion: LONG NO 1.00 ct paid $0.50, settled NO
        # (parlay MISSED, V=0) -> pays $1.00 -> realized +$0.50, exact.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement.binary(
                "demo",
                Side.NO,
                Q(100),          # 1.00 contract
                CC(5_000),       # $0.50 premium
                settled_yes=False,  # parlay MISSED -> V=0 -> NO pays $1
            )
        )
        assert realized == 5_000            # +$0.50 to the cent
        assert bt.realized_pnl_cc == 5_000
        assert bt.cumulative_loss_cc == 0   # a win adds nothing to losses
        assert bt.accrued_fees_cc == 0      # combo maker fill: $0 fee
        assert bt.settled_count == 1

    def test_no_settle_hit_debits_the_premium(self) -> None:
        # LONG NO, parlay HIT (settles YES, V=1) -> NO worthless -> lose premium.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement.binary("p", Side.NO, Q(100), CC(5_000), settled_yes=True)
        )
        assert realized == -5_000
        assert bt.realized_pnl_cc == -5_000
        assert bt.cumulative_loss_cc == 5_000

    def test_ledger_accumulates_wins_and_losses(self) -> None:
        bt, _clock = tracker()
        # 2.00 ct NO @ $0.40 miss: (contracts*$1 - premium) = 20000 - 8000 = +12000
        bt.apply_settlement(
            Settlement.binary("win", Side.NO, Q(200), CC(4_000), settled_yes=False)
        )
        # 1.00 ct NO @ $0.30 hit: -3000
        bt.apply_settlement(
            Settlement.binary("loss", Side.NO, Q(100), CC(3_000), settled_yes=True)
        )
        assert bt.realized_pnl_cc == 12_000 - 3_000
        assert bt.cumulative_loss_cc == 3_000
        assert bt.settled_count == 2

    def test_settlement_is_idempotent_per_position(self) -> None:
        bt, _clock = tracker()
        s = Settlement.binary("dup", Side.NO, Q(100), CC(5_000), settled_yes=False)
        first = bt.apply_settlement(s)
        second = bt.apply_settlement(s)  # replayed message
        assert first == 5_000
        assert second == 0  # no double-count
        assert bt.realized_pnl_cc == 5_000
        assert bt.settled_count == 1

    def test_no_credit_refuses_when_convention_unverified(self) -> None:
        bt, _clock = tracker(NO_UNVERIFIED)
        with pytest.raises(StaleBalanceError):
            bt.apply_settlement(
                Settlement.binary("x", Side.NO, Q(100), CC(5_000), settled_yes=False)
            )

    def test_long_yes_mirror_hit_wins(self) -> None:
        # Defensive (not sell-only): LONG YES, parlay HITS (V=1) -> YES pays $1.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement.binary("y", Side.YES, Q(100), CC(5_000), settled_yes=True)
        )
        assert realized == 5_000

    def test_long_yes_mirror_miss_loses(self) -> None:
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement.binary("y", Side.YES, Q(100), CC(5_000), settled_yes=False)
        )
        assert realized == -5_000
        assert bt.cumulative_loss_cc == 5_000


class TestBankrollLedgerIndependence:
    def test_ledger_does_not_touch_bankroll(self) -> None:
        # The realized tally is a cross-check, NEVER summed into the live
        # bankroll (the live poll already contains the money).
        bt, _clock = tracker()
        await_none = bt.bankroll_cc_or_none()
        assert await_none is None  # no poll
        bt.apply_settlement(
            Settlement.binary("w", Side.NO, Q(100), CC(5_000), settled_yes=False)
        )
        assert bt.realized_pnl_cc == 5_000
        assert bt.bankroll_cc_or_none() is None  # ledger did not fabricate a bankroll


# --- FIX 1: equity-aware bankroll denominator --------------------------------


class TestPortfolioValueParsing:
    def test_prefers_exact_dollars_string(self) -> None:
        assert _parse_portfolio_value_cc(
            {"portfolio_value": 50000, "portfolio_value_dollars": "500.00"}
        ) == CC(5_000_000)

    def test_falls_back_to_int_cents_times_100(self) -> None:
        # 50000 cents = $500.00 = 5_000_000 cc (the explicit x100 boundary).
        assert _parse_portfolio_value_cc({"portfolio_value": 50000}) == CC(5_000_000)

    def test_zero_positions(self) -> None:
        assert _parse_portfolio_value_cc({"portfolio_value": 0}) == CC(0)

    def test_missing_raises(self) -> None:
        with pytest.raises(BalanceParseError):
            _parse_portfolio_value_cc({"balance": 1})

    def test_bool_is_not_int_cents(self) -> None:
        with pytest.raises(BalanceParseError):
            _parse_portfolio_value_cc({"portfolio_value": True})


class TestEquityAwareDenominator:
    async def test_cash_and_equity_are_separate_never_conflated(self) -> None:
        # $1082.62 cash + $500.00 positions. Units: cents x100 = cc, exact.
        bt, _clock = tracker()
        await bt.refresh(
            FakeBalanceSource({"balance": 108262, "portfolio_value": 50000})
        )
        assert bt.available_cash_cc == CC(10_826_200)   # cash alone
        assert bt.portfolio_value_cc == CC(5_000_000)   # mark alone
        assert bt.exchange_equity_cc == CC(15_826_200)  # their sum, derived
        # The two raw figures are never merged into one another.
        assert bt.available_cash_cc != bt.exchange_equity_cc

    async def test_min_picks_the_smaller_mark_gain_cannot_inflate(self) -> None:
        # First poll (SOD) anchors equity at cash+pv. A later intraday poll shows
        # a mark-to-model GAIN; the denominator must stay capped at SOD equity.
        bt, clock = tracker()
        # SOD: cash $1000 (100000 cents -> 10_000_000 cc), pv 0 -> SOD equity $1000.
        await bt.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 0})
        )
        sod = bt.start_of_day_equity_cc
        # Same UTC day: mark jumps to pv=$500 (50000 cents), cash unchanged.
        await bt.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 50000})
        )
        # cash + 0.5*pv = 10_000_000 + 0.5*(5_000_000) = 10_000_000 + 2_500_000
        # = 12_500_000, but SOD equity was 10_000_000 -> min caps it.
        assert bt.start_of_day_equity_cc == sod == CC(10_000_000)
        assert bt.risk_bankroll_cc == CC(10_000_000)  # min picked SOD, not the gain

    async def test_deploy_capital_denominator_stays_flat_not_shrunk(self) -> None:
        # SOD: all cash, nothing deployed. Then deploy $500 into positions:
        # cash falls, equity ~flat. The denominator must NOT collapse to cash.
        bt, _clock = tracker()
        # SOD equity $1582.62 (all cash).
        await bt.refresh(
            FakeBalanceSource({"balance": 158262, "portfolio_value": 0})
        )
        assert bt.risk_bankroll_cc == CC(15_826_200)  # all cash, pv=0
        # Deploy $500: cash 1082.62, pv 500.00 (marked flat) — same UTC day.
        await bt.refresh(
            FakeBalanceSource({"balance": 108262, "portfolio_value": 50000})
        )
        # cash + 0.5*pv = 10_826_200 + 2_500_000 = 13_326_200 < SOD 15_826_200.
        assert bt.risk_bankroll_cc == CC(13_326_200)
        # It did NOT shrink to bare cash (deployed != lost).
        assert bt.risk_bankroll_cc > bt.available_cash_cc

    async def test_haircut_applies_only_to_portfolio_value(self) -> None:
        # haircut=0 -> denominator ignores pv entirely (min(SOD, cash)).
        bt0, _c0 = tracker(portfolio_haircut=Fraction(0))
        await bt0.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 50000})
        )
        # New day path already anchored SOD=cash+pv=$1500. cash+0*pv=$1000 < SOD.
        assert bt0.risk_bankroll_cc == CC(10_000_000)  # cash only

        # haircut=1 -> full pv counts; equals min(SOD, full equity) = equity.
        bt1, _c1 = tracker(portfolio_haircut=Fraction(1))
        await bt1.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 50000})
        )
        assert bt1.risk_bankroll_cc == CC(15_000_000)  # cash + full pv, == SOD

    def test_haircut_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            tracker(portfolio_haircut=Fraction(3, 2))
        with pytest.raises(ValueError):
            tracker(portfolio_haircut=Fraction(-1, 2))

    async def test_stale_fails_closed_on_every_denominator_accessor(self) -> None:
        bt, clock = tracker(stale_after_s=30.0)
        await bt.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 50000})
        )
        clock.advance(30.001)
        assert bt.is_stale
        accessors: list[Callable[[], CentiCents]] = [
            lambda: bt.available_cash_cc,
            lambda: bt.portfolio_value_cc,
            lambda: bt.exchange_equity_cc,
            lambda: bt.start_of_day_equity_cc,
            lambda: bt.risk_bankroll_cc,
        ]
        for accessor in accessors:
            with pytest.raises(StaleBalanceError):
                accessor()
        assert bt.risk_bankroll_cc_or_none() is None

    async def test_day_boundary_reanchors_on_new_utc_date(self) -> None:
        clock = FakeClock(start=datetime(2026, 7, 12, 23, 0, tzinfo=UTC))
        bt = BalanceTracker(VERIFIED, clock, stale_after_s=1e9)
        await bt.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 0})
        )
        assert bt.start_of_day_equity_cc == CC(10_000_000)  # $1000 SOD
        # Advance 2h -> crosses UTC midnight to 2026-07-13; equity now higher.
        clock.advance(2 * 3600)
        await bt.refresh(
            FakeBalanceSource({"balance": 120000, "portfolio_value": 0})
        )
        assert bt.start_of_day_equity_cc == CC(12_000_000)  # re-anchored to new day

    async def test_same_day_does_not_reanchor(self) -> None:
        clock = FakeClock(start=datetime(2026, 7, 12, 1, 0, tzinfo=UTC))
        bt = BalanceTracker(VERIFIED, clock, stale_after_s=1e9)
        await bt.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 0})
        )
        clock.advance(3600)  # same UTC day
        await bt.refresh(
            FakeBalanceSource({"balance": 120000, "portfolio_value": 0})
        )
        assert bt.start_of_day_equity_cc == CC(10_000_000)  # UNCHANGED (same day)

    async def test_operator_override_of_anchor(self) -> None:
        bt, _clock = tracker()
        await bt.refresh(
            FakeBalanceSource({"balance": 100000, "portfolio_value": 0})
        )
        bt.set_start_of_day_equity(CC(20_000_000))  # e.g. after a deposit
        assert bt.start_of_day_equity_cc == CC(20_000_000)
        # Denominator now floored by the override, not the auto-anchor.
        assert bt.risk_bankroll_cc == CC(10_000_000)  # cash $1000 < override $2000


# --- FIX 2: fees booked in the settlement ledger -----------------------------


class TestFeeBooking:
    def test_combo_maker_fill_books_zero_fee(self) -> None:
        # Ground truth: our combo maker fill pays $0 fee. Book it (0), net it.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("demo", Side.NO, Q(100), CC(5_000), settled_value=0.0, fee_cc=CC(0))
        )
        assert realized == 5_000        # +$0.50, unchanged by a $0 fee
        assert bt.accrued_fees_cc == 0

    def test_realized_nets_a_nonzero_fee(self) -> None:
        # A synthetic nonzero-fee series: fee is subtracted from realized.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("f", Side.NO, Q(100), CC(5_000), settled_value=0.0, fee_cc=CC(175))
        )
        assert realized == 5_000 - 175   # win minus the fee
        assert bt.realized_pnl_cc == 4_825
        assert bt.accrued_fees_cc == 175

    def test_fee_pushes_a_marginal_win_into_loss_column(self) -> None:
        # NO @ $0.9999 miss -> gross +1cc; a 5cc fee makes it a net loss.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("m", Side.NO, Q(100), CC(9_999), settled_value=0.0, fee_cc=CC(5))
        )
        assert realized == 1 - 5  # (10000-9999) - 5 = -4
        assert bt.cumulative_loss_cc == 4

    def test_fee_computed_by_real_fee_model_is_zero_for_combo_maker(self) -> None:
        # PROOF via pricing/fees.py (never reimplemented): a QUADRATIC series
        # with maker attribution charges $0 -> the field we book is 0.
        from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType

        model = FeeModel(FeeSchedule.from_strings("0.07", "0.0175"), VERIFIED)
        fee = model.trade_fee_cc(
            price_cc=CC(5_000), qty=Q(100), fee_type=FeeType.QUADRATIC
        )
        assert int(fee) == 0
        # And a taker series books the real 175cc (0.07 * 1ct * 0.5 * 0.5 = $0.0175).
        taker_conv = Conventions(
            verified=True, source="taker",
            maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
            maker_pays_own_bid=True, maker_is_taker_on_fill=True,
            combo_no_pays_complement=True,
        )
        taker_model = FeeModel(FeeSchedule.from_strings("0.07", "0.0175"), taker_conv)
        taker_fee = taker_model.trade_fee_cc(
            price_cc=CC(5_000), qty=Q(100), fee_type=FeeType.QUADRATIC
        )
        assert int(taker_fee) == 175
        # Book the taker fee end-to-end.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("t", Side.NO, Q(100), CC(5_000), settled_value=0.0, fee_cc=taker_fee)
        )
        assert realized == 5_000 - 175
        assert bt.accrued_fees_cc == 175


# --- FIX 3: scalar settlement in the ledger ----------------------------------


class TestScalarSettlement:
    @pytest.mark.parametrize(
        "v, expected",
        [
            (0.0, 5_000),    # binary MISS -> (1-0)-0.5 = +$0.50  (Phase-0 parity)
            (0.5, 0),        # scalar -> (0.5)-0.5 = $0.00
            (0.7, -2_000),   # scalar -> (0.3)-0.5 = -$0.20
            (1.0, -5_000),   # binary HIT -> (0)-0.5 = -$0.50  (Phase-0 parity)
        ],
    )
    def test_scalar_table_1ct_at_50c(self, v: float, expected: int) -> None:
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("s", Side.NO, Q(100), CC(5_000), settled_value=v)
        )
        assert realized == expected

    def test_binary_helpers_reproduce_phase0_via_settled_yes(self) -> None:
        # Settlement.binary(settled_yes=True/False) is exactly V=1 / V=0.
        miss = Settlement.binary("a", Side.NO, Q(100), CC(5_000), settled_yes=False)
        hit = Settlement.binary("b", Side.NO, Q(100), CC(5_000), settled_yes=True)
        assert miss.settled_value == 0.0 and miss.settled_yes is False
        assert hit.settled_value == 1.0 and hit.settled_yes is True

    def test_retains_the_actual_scalar_never_coerced(self) -> None:
        s = Settlement("s", Side.NO, Q(100), CC(5_000), settled_value=0.7)
        assert s.settled_value == 0.7          # exact scalar retained
        assert s.settled_yes is False          # 0.7 is NOT a clean HIT

    def test_scalar_settlement_is_idempotent(self) -> None:
        bt, _clock = tracker()
        s = Settlement("dup", Side.NO, Q(100), CC(5_000), settled_value=0.7)
        first = bt.apply_settlement(s)
        second = bt.apply_settlement(s)
        assert first == -2_000
        assert second == 0
        assert bt.realized_pnl_cc == -2_000
        assert bt.settled_count == 1

    def test_scalar_no_credit_gated_on_convention(self) -> None:
        # A scalar NO payout is exactly the case behind combo_no_pays_complement;
        # unverified -> refuse (never fabricate a fractional credit).
        bt, _clock = tracker(NO_UNVERIFIED)
        with pytest.raises(StaleBalanceError):
            bt.apply_settlement(
                Settlement("x", Side.NO, Q(100), CC(5_000), settled_value=0.7)
            )

    def test_no_payout_helper_rounds_down_v_no_seller_favorable(self) -> None:
        # V floored onto the cc grid -> 1 - floor(V) >= 1 - V (NO-seller favored).
        assert _no_payout_per_contract_cc(0.0) == 10_000   # $1.00
        assert _no_payout_per_contract_cc(1.0) == 0        # $0.00
        assert _no_payout_per_contract_cc(0.7) == 3_000    # exact on-grid
        # An off-grid V (0.70005 -> 7000.5 cc): floor V -> NO gets 3000, not 2999.
        assert _no_payout_per_contract_cc(0.70005) == 10_000 - 7_000

    def test_long_yes_scalar_mirror(self) -> None:
        # Defensive: LONG YES pays V per contract. V=0.7 -> pays $0.70.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("y", Side.YES, Q(100), CC(5_000), settled_value=0.7)
        )
        assert realized == 7_000 - 5_000  # $0.70 payout - $0.50 premium = +$0.20
