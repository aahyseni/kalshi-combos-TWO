"""risk.balance.BalanceTracker — live bankroll (fail-closed on stale) + a
realized-P&L ledger advanced on settlement.

Fakes only: a ``FakeClock`` drives staleness deterministically and a
``FakeBalanceSource`` returns canned /portfolio/balance payloads — no live
credentials, ever. The credit-on-NO-settle case is anchored to the 2026-07-10
demo ground truth (LONG NO 1.00 ct paid $0.50, settled NO, paid $1.00,
balance 1082.62 -> 1083.62, realized +$0.50).
"""

from __future__ import annotations

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
    _parse_balance_cc,
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


def tracker(conventions: Conventions = VERIFIED, *, stale_after_s: float = 30.0) -> tuple[
    BalanceTracker, FakeClock
]:
    clock = FakeClock()
    return BalanceTracker(conventions, clock, stale_after_s=stale_after_s), clock


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
        src = FakeBalanceSource({"balance_dollars": "5000.00"})
        got = await bt.refresh(src)
        assert got == CC(50_000_000)
        assert not bt.is_stale
        assert bt.bankroll_cc == CC(50_000_000)
        assert bt.bankroll_cc_or_none() == CC(50_000_000)

    async def test_goes_stale_after_window(self) -> None:
        bt, clock = tracker(stale_after_s=30.0)
        await bt.refresh(FakeBalanceSource({"balance": 500000}))
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
        await bt.refresh(FakeBalanceSource({"balance": 500000}))
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
        # (parlay MISSED) -> pays $1.00 -> realized +$0.50, exact.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement(
                position_id="demo",
                our_side=Side.NO,
                contracts=Q(100),          # 1.00 contract
                entry_price_cc=CC(5_000),  # $0.50 premium
                settled_yes=False,         # parlay MISSED -> NO settles NO
            )
        )
        assert realized == 5_000            # +$0.50 to the cent
        assert bt.realized_pnl_cc == 5_000
        assert bt.cumulative_loss_cc == 0   # a win adds nothing to losses
        assert bt.settled_count == 1

    def test_no_settle_hit_debits_the_premium(self) -> None:
        # LONG NO, parlay HIT (settles YES) -> NO worthless -> lose the premium.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement(
                position_id="p",
                our_side=Side.NO,
                contracts=Q(100),
                entry_price_cc=CC(5_000),
                settled_yes=True,
            )
        )
        assert realized == -5_000
        assert bt.realized_pnl_cc == -5_000
        assert bt.cumulative_loss_cc == 5_000

    def test_ledger_accumulates_wins_and_losses(self) -> None:
        bt, _clock = tracker()
        # 2.00 ct NO @ $0.40 miss: (contracts*$1 - premium) = 20000 - 8000 = +12000
        bt.apply_settlement(
            Settlement("win", Side.NO, Q(200), CC(4_000), settled_yes=False)
        )
        # 1.00 ct NO @ $0.30 hit: -3000
        bt.apply_settlement(
            Settlement("loss", Side.NO, Q(100), CC(3_000), settled_yes=True)
        )
        assert bt.realized_pnl_cc == 12_000 - 3_000
        assert bt.cumulative_loss_cc == 3_000
        assert bt.settled_count == 2

    def test_settlement_is_idempotent_per_position(self) -> None:
        bt, _clock = tracker()
        s = Settlement("dup", Side.NO, Q(100), CC(5_000), settled_yes=False)
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
                Settlement("x", Side.NO, Q(100), CC(5_000), settled_yes=False)
            )

    def test_long_yes_mirror_hit_wins(self) -> None:
        # Defensive (not sell-only): LONG YES, parlay HITS -> YES pays $1.
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("y", Side.YES, Q(100), CC(5_000), settled_yes=True)
        )
        assert realized == 5_000

    def test_long_yes_mirror_miss_loses(self) -> None:
        bt, _clock = tracker()
        realized = bt.apply_settlement(
            Settlement("y", Side.YES, Q(100), CC(5_000), settled_yes=False)
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
            Settlement("w", Side.NO, Q(100), CC(5_000), settled_yes=False)
        )
        assert bt.realized_pnl_cc == 5_000
        assert bt.bankroll_cc_or_none() is None  # ledger did not fabricate a bankroll
