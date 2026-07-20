"""External-transfer awareness (2026-07-21, operator: the bot must 100% know
its balance/standing with NO manual anchor updates).

Under test:

1. ``new_external_transfer_deltas`` — the pure transfer classifier: a NEWLY
   applied deposit yields +net(amount − fee), a terminal withdrawal yields
   −(amount + fee); a pending row is never seeded (so pending→applied IS
   picked up later); each terminal transfer applies exactly once; unreadable
   amounts are skipped (never guess money).
2. ``BalanceTracker.apply_external_transfer`` — BOTH anchors (start-of-day
   equity + intraday peak) shift by exactly the delta, so a deposit is never
   profit, a withdrawal is never a give-back, and the give-back halts stay
   pure-drawdown through any transfer. No anchors yet ⇒ no-op (the first poll
   anchors on a balance that already contains the transfer).
"""

from __future__ import annotations

from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.ops.quote_app import new_external_transfer_deltas
from combomaker.risk.balance import BalanceTracker
from tests.test_balance import VERIFIED, FakeBalanceSource


def _dep(id_: str, status: str, amount: int, fee: int = 0) -> dict[str, Any]:
    return {"id": id_, "status": status, "amount_cents": amount, "fee_cents": fee}


class TestTransferDeltas:
    def test_applied_deposit_net_of_fee(self) -> None:
        seen: set[str] = set()
        out = new_external_transfer_deltas(seen, [_dep("d1", "applied", 200_000, 500)], [])
        assert out == [("deposit", "dep:d1", (200_000 - 500) * 100)]

    def test_each_transfer_applies_exactly_once(self) -> None:
        seen: set[str] = set()
        rows = [_dep("d1", "applied", 200_000)]
        assert len(new_external_transfer_deltas(seen, rows, [])) == 1
        assert new_external_transfer_deltas(seen, rows, []) == []

    def test_pending_not_seeded_then_applied_is_picked_up(self) -> None:
        # The baseline pass sees a PENDING deposit — it must NOT enter the seen
        # set, so the later pending→applied transition applies its delta.
        seen: set[str] = set()
        assert new_external_transfer_deltas(seen, [_dep("d1", "pending", 50_000)], []) == []
        out = new_external_transfer_deltas(seen, [_dep("d1", "applied", 50_000)], [])
        assert out == [("deposit", "dep:d1", 5_000_000)]

    def test_failed_and_returned_never_apply(self) -> None:
        seen: set[str] = set()
        rows = [_dep("d1", "failed", 50_000), _dep("d2", "returned", 50_000)]
        assert new_external_transfer_deltas(seen, rows, []) == []

    def test_withdrawal_debits_amount_plus_fee(self) -> None:
        seen: set[str] = set()
        out = new_external_transfer_deltas(
            seen, [], [_dep("w1", "complete", 100_000, 200)]
        )
        assert out == [("withdrawal", "wd:w1", -(100_000 + 200) * 100)]

    def test_unreadable_amount_is_skipped(self) -> None:
        seen: set[str] = set()
        rows = [{"id": "d1", "status": "applied", "amount_cents": "2000.00"}]
        assert new_external_transfer_deltas(seen, rows, []) == []
        assert seen == set()  # not seeded either — a later readable row applies


class TestAnchorAdjustment:
    async def _anchored_tracker(self) -> tuple[BalanceTracker, FakeClock]:
        clock = FakeClock()
        tracker = BalanceTracker(VERIFIED, clock, stale_after_s=1e9)
        # First poll: $2,000 cash, no positions → SOD = peak = 20_000_000cc.
        await tracker.refresh(
            FakeBalanceSource({"balance": 200_000, "portfolio_value": 0})
        )
        return tracker, clock

    async def test_deposit_shifts_both_anchors_up(self) -> None:
        tracker, clock = await self._anchored_tracker()
        tracker.apply_external_transfer(5_000_000, kind="deposit", ref="dep:d1")
        assert int(tracker.start_of_day_equity_cc) == 25_000_000
        assert int(tracker.peak_equity_cc) == 25_000_000

    async def test_deposit_is_not_headroom_under_the_peak(self) -> None:
        # Without the adjustment a $500 deposit would lift equity ABOVE the old
        # peak and grant $500 of free give-back headroom; with it, a subsequent
        # trading loss measures from the deposit-adjusted peak.
        tracker, clock = await self._anchored_tracker()
        tracker.apply_external_transfer(5_000_000, kind="deposit", ref="dep:d1")
        clock.advance(1.0)
        # Deposit landed, then $300 of trading losses: equity $2,200.
        await tracker.refresh(
            FakeBalanceSource({"balance": 220_000, "portfolio_value": 0})
        )
        give_back = int(tracker.peak_equity_cc) - int(tracker.exchange_equity_cc)
        assert give_back == 3_000_000  # the REAL $300 loss — not masked to $0

    async def test_withdrawal_is_not_a_give_back(self) -> None:
        # A $500 withdrawal drops equity $500; the adjusted peak drops with it,
        # so the give-back halts read $0 — a withdrawal is never a drawdown.
        tracker, clock = await self._anchored_tracker()
        tracker.apply_external_transfer(-5_000_000, kind="withdrawal", ref="wd:w1")
        clock.advance(1.0)
        await tracker.refresh(
            FakeBalanceSource({"balance": 150_000, "portfolio_value": 0})
        )
        give_back = int(tracker.peak_equity_cc) - int(tracker.exchange_equity_cc)
        assert give_back == 0

    async def test_no_anchor_yet_is_noop(self) -> None:
        clock = FakeClock()
        tracker = BalanceTracker(VERIFIED, clock, stale_after_s=1e9)
        tracker.apply_external_transfer(5_000_000, kind="deposit", ref="dep:d1")
        # First poll AFTER the transfer: anchors form on the balance that
        # already contains it — no double count.
        await tracker.refresh(
            FakeBalanceSource({"balance": 250_000, "portfolio_value": 0})
        )
        assert int(tracker.start_of_day_equity_cc) == 25_000_000
        assert int(tracker.peak_equity_cc) == 25_000_000
