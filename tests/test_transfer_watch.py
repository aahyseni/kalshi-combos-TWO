"""External-transfer awareness — REWRITTEN after the 2026-07-21 adversarial
review (CRITICAL family: the raw peak is a high-water mark fed by the 10s
balance poll, so a naive ``peak += Δ`` double-counts every mid-session deposit
and manufactures a phantom give-back KILL).

Under test:

1. ``new_external_transfer_deltas`` — STATUS-TRANSITION classifier
   (doc-verified enums 2026-07-21: pending|applied|failed|returned, both
   directions): INTO-applied applies once; applied→returned applies the
   reversing delta (ACH clawback, review F5); pending never records a delta;
   ``baseline_before_ms`` suppresses transitions already inside the anchored
   readings (review F6); unreadable rows are skipped without poisoning later
   readable ones.
2. P&L-SPACE give-back (``pnl_equity`` / ``peak_pnl`` + ordering-aware
   ``apply_external_transfer``): the PRODUCTION ordering — the balance poll
   absorbs the cash FIRST, the watcher detects later — must yield ZERO
   phantom give-back (the old test suite encoded the reverse order and passed
   green while production double-counted; review F1). Withdrawals: the
   transient lag reading self-corrects at detection and a withdrawal is never
   a drawdown. Day-boundary: a transfer finalized before the anchors formed
   must not shift them (review F3).
"""

from __future__ import annotations

from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.ops.quote_app import new_external_transfer_deltas
from combomaker.risk.balance import BalanceTracker
from tests.test_balance import VERIFIED, FakeBalanceSource


def _row(
    id_: str, status: str, amount: int, fee: int = 0, finalized_ms: int | None = 1_000
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": id_, "status": status, "amount_cents": amount, "fee_cents": fee,
    }
    if finalized_ms is not None:
        row["finalized_ts"] = finalized_ms
    return row


class TestTransferDeltas:
    def test_applied_deposit_net_of_fee(self) -> None:
        statuses: dict[str, str] = {}
        out = new_external_transfer_deltas(
            statuses, [_row("d1", "applied", 200_000, 500)], []
        )
        assert out == [("deposit", "dep:d1", (200_000 - 500) * 100, 1_000)]

    def test_each_transition_applies_exactly_once(self) -> None:
        statuses: dict[str, str] = {}
        rows = [_row("d1", "applied", 200_000)]
        assert len(new_external_transfer_deltas(statuses, rows, [])) == 1
        assert new_external_transfer_deltas(statuses, rows, []) == []

    def test_pending_then_applied_is_picked_up(self) -> None:
        statuses: dict[str, str] = {}
        assert new_external_transfer_deltas(
            statuses, [_row("d1", "pending", 50_000)], []
        ) == []
        out = new_external_transfer_deltas(
            statuses, [_row("d1", "applied", 50_000)], []
        )
        assert out == [("deposit", "dep:d1", 5_000_000, 1_000)]

    def test_applied_then_returned_reverses(self) -> None:
        # ACH clawback (review F5): the anchors must not stay shifted by a
        # deposit whose cash the exchange took back.
        statuses: dict[str, str] = {}
        new_external_transfer_deltas(statuses, [_row("d1", "applied", 50_000)], [])
        out = new_external_transfer_deltas(
            statuses, [_row("d1", "returned", 50_000, finalized_ms=2_000)], []
        )
        assert out == [("deposit-returned", "dep:d1", -5_000_000, 2_000)]

    def test_withdrawal_debits_amount_plus_fee(self) -> None:
        statuses: dict[str, str] = {}
        out = new_external_transfer_deltas(
            statuses, [], [_row("w1", "applied", 100_000, 200)]
        )
        assert out == [("withdrawal", "wd:w1", -(100_000 + 200) * 100, 1_000)]

    def test_baseline_suppresses_pre_anchor_transitions(self) -> None:
        # First pass: a transfer finalized BEFORE the anchors formed is inside
        # the anchored balance — status recorded, no delta (review F6); one
        # finalized AFTER the anchor instant applies even on the first pass.
        statuses: dict[str, str] = {}
        rows = [
            _row("old", "applied", 200_000, finalized_ms=500),
            _row("new", "applied", 50_000, finalized_ms=2_000),
        ]
        out = new_external_transfer_deltas(
            statuses, rows, [], baseline_before_ms=1_500
        )
        assert out == [("deposit", "dep:new", 5_000_000, 2_000)]
        # And the baselined one never re-fires later.
        assert new_external_transfer_deltas(statuses, rows, []) == []

    def test_failed_and_returned_without_applied_never_apply(self) -> None:
        statuses: dict[str, str] = {}
        rows = [
            _row("d1", "failed", 50_000),
            _row("d2", "returned", 50_000),  # clawed back before we watched
        ]
        assert new_external_transfer_deltas(statuses, rows, []) == []

    def test_unreadable_amount_skipped_without_poisoning(self) -> None:
        statuses: dict[str, str] = {}
        bad = {"id": "d1", "status": "applied", "amount_cents": "2000.00"}
        assert new_external_transfer_deltas(statuses, [bad], []) == []
        # A later READABLE row for the same id still applies.
        out = new_external_transfer_deltas(
            statuses, [_row("d1", "applied", 200_000)], []
        )
        assert out == [("deposit", "dep:d1", 20_000_000, 1_000)]

    def test_missing_id_skipped(self) -> None:
        statuses: dict[str, str] = {}
        assert new_external_transfer_deltas(
            statuses, [{"status": "applied", "amount_cents": 1}], []
        ) == []


class TestPnlSpaceGiveBack:
    """The production ordering, end to end on the REAL tracker."""

    async def _tracker(self) -> tuple[BalanceTracker, FakeClock]:
        clock = FakeClock()
        tracker = BalanceTracker(VERIFIED, clock, stale_after_s=1e9)
        await tracker.refresh(
            FakeBalanceSource({"balance": 200_000, "portfolio_value": 0})
        )
        return tracker, clock

    @staticmethod
    def _give_back(tracker: BalanceTracker) -> int:
        peak = tracker.peak_pnl_cc_or_none()
        cur = tracker.pnl_equity_cc_or_none()
        assert peak is not None and cur is not None
        return max(0, peak - cur)

    def _wall_ms(self, clock: FakeClock) -> int:
        return int(clock.now().timestamp() * 1000)

    async def test_deposit_production_order_no_phantom_giveback(self) -> None:
        # THE review-F1 scenario, in the order production experiences it:
        # cash lands → 10s poll absorbs it (raw high-water rises) → the 60s
        # watcher detects and applies. The old code read give-back = D for the
        # rest of the day; the P&L-space fix must read exactly 0.
        tracker, clock = await self._tracker()
        clock.advance(10.0)
        finalized = self._wall_ms(clock)  # deposit finalizes now
        clock.advance(10.0)
        await tracker.refresh(  # poll ABSORBS the $500 deposit first
            FakeBalanceSource({"balance": 250_000, "portfolio_value": 0})
        )
        clock.advance(50.0)  # watcher detects up to a minute later
        tracker.apply_external_transfer(
            5_000_000, kind="deposit", ref="dep:d1", finalized_wall_ms=finalized
        )
        assert self._give_back(tracker) == 0

    async def test_real_loss_after_deposit_still_measures(self) -> None:
        tracker, clock = await self._tracker()
        clock.advance(10.0)
        finalized = self._wall_ms(clock)
        clock.advance(10.0)
        await tracker.refresh(
            FakeBalanceSource({"balance": 250_000, "portfolio_value": 0})
        )
        clock.advance(50.0)
        tracker.apply_external_transfer(
            5_000_000, kind="deposit", ref="dep:d1", finalized_wall_ms=finalized
        )
        clock.advance(10.0)
        await tracker.refresh(  # $300 of REAL trading losses
            FakeBalanceSource({"balance": 220_000, "portfolio_value": 0})
        )
        assert self._give_back(tracker) == 3_000_000

    async def test_deposit_detected_before_poll_no_phantom_either(self) -> None:
        # The reverse (rare) ordering: watcher applies BEFORE any poll saw the
        # cash — the peak was set before the transfer finalized, so no peak
        # correction fires; the next poll's A includes cash − K = unchanged.
        tracker, clock = await self._tracker()
        clock.advance(10.0)
        finalized = self._wall_ms(clock)
        tracker.apply_external_transfer(
            5_000_000, kind="deposit", ref="dep:d1", finalized_wall_ms=finalized
        )
        clock.advance(10.0)
        await tracker.refresh(
            FakeBalanceSource({"balance": 250_000, "portfolio_value": 0})
        )
        assert self._give_back(tracker) == 0

    async def test_withdrawal_transient_corrects_at_detection(self) -> None:
        # Cash leaves → polls read a $500 P&L dip until the watcher detects
        # (the bounded transient the 60s cadence exists for) → detection
        # corrects the measurement to 0: a withdrawal is never a drawdown.
        tracker, clock = await self._tracker()
        clock.advance(10.0)
        finalized = self._wall_ms(clock)
        clock.advance(10.0)
        await tracker.refresh(
            FakeBalanceSource({"balance": 150_000, "portfolio_value": 0})
        )
        assert self._give_back(tracker) == 5_000_000  # transient, pre-detection
        clock.advance(50.0)
        tracker.apply_external_transfer(
            -5_000_000, kind="withdrawal", ref="wd:w1", finalized_wall_ms=finalized
        )
        assert self._give_back(tracker) == 0

    async def test_day_boundary_transfer_before_anchor_shifts_nothing(self) -> None:
        # Review F3: a deposit finalized BEFORE the current anchors formed is
        # already inside them — applying it must not shift SOD, and the peak
        # correction must leave give-back at 0, not −D headroom or +D phantom.
        tracker, clock = await self._tracker()  # anchors formed at t0
        anchor_sod = int(tracker.start_of_day_equity_cc)
        finalized = self._wall_ms(clock) - 5_000  # finalized before the anchor
        clock.advance(30.0)
        tracker.apply_external_transfer(
            5_000_000, kind="deposit", ref="dep:d1", finalized_wall_ms=finalized
        )
        assert int(tracker.start_of_day_equity_cc) == anchor_sod  # no double shift
        clock.advance(10.0)
        await tracker.refresh(
            FakeBalanceSource({"balance": 200_000, "portfolio_value": 0})
        )
        assert self._give_back(tracker) == 0

    async def test_sod_shifts_for_post_anchor_deposit(self) -> None:
        # The auto-scaling contract: a genuine mid-day deposit DOES lift the
        # SOD anchor (caps scale the same day), on top of the pnl-space fix.
        tracker, clock = await self._tracker()
        sod0 = int(tracker.start_of_day_equity_cc)
        clock.advance(10.0)
        finalized = self._wall_ms(clock)
        clock.advance(10.0)
        tracker.apply_external_transfer(
            5_000_000, kind="deposit", ref="dep:d1", finalized_wall_ms=finalized
        )
        assert int(tracker.start_of_day_equity_cc) == sod0 + 5_000_000

    async def test_raw_peak_untouched_for_reports(self) -> None:
        # The RAW high-water accessor keeps exchange-truth semantics for
        # reports; only the give-back inputs moved to P&L space.
        tracker, clock = await self._tracker()
        clock.advance(10.0)
        await tracker.refresh(
            FakeBalanceSource({"balance": 250_000, "portfolio_value": 0})
        )
        assert int(tracker.peak_equity_cc) == 25_000_000
