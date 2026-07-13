"""Settlement poller + handler: the live wiring that makes the realized-P&L
ledger + exchange-first settlement reconciliation ACTIVE.

Fakes only — a FakeClock, a real BalanceTracker + ExposureBook + KillSwitch, and
a fake settlement source returning canned /portfolio/settlements rows. No live
credentials. Anchored to the 2026-07-10 demo ground truth (LONG NO 1.00 ct @
$0.50 settles NO → pays $1.00 → realized +$0.50).
"""

from __future__ import annotations

from typing import Any

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.settlement import (
    SettlementHandler,
    SettlementPoller,
    SettlementReconcileError,
    parse_settlement,
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

NO_UNVERIFIED = Conventions(
    verified=True,
    source="test-no-unverified",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=None,
)


class FakeLifecycle:
    """Records realized-P&L deltas + reconciles the farm tripwire + to-the-cent
    revenue check — the exact slice ``SettlementHandler`` drives. Mirrors the real
    ``QuoteLifecycle.reconcile_combo_settlement`` reconcile so the handler wiring
    is exercised without the full pricing stack (the real path is covered
    separately in test_lifecycle)."""

    def __init__(self, exposure: ExposureBook, killswitch: KillSwitch) -> None:
        self._exposure = exposure
        self._killswitch = killswitch
        self.realized_deltas: list[int] = []

    def record_realized_pnl(self, delta_cc: int) -> None:
        self.realized_deltas.append(delta_cc)

    async def reconcile_combo_settlement(
        self,
        combo_ticker: str,
        *,
        settled_yes: bool,
        settled_value: float | None = None,
        expected_revenue_cc: int | None = None,
    ) -> None:
        on_ticker = [
            p for p in self._exposure.positions.values() if p.combo_ticker == combo_ticker
        ]
        if any(p.farmed for p in on_ticker) and settled_yes:
            await self._killswitch.halt(
                ReasonCode.HALT_RECONCILIATION_MISMATCH, "farmed settled yes"
            )
            return
        if expected_revenue_cc is None or settled_value is None or not on_ticker:
            return
        predicted = 0
        for p in on_ticker:
            v_cc = round(settled_value * 10_000)
            per_ct = (10_000 - v_cc) if p.our_side is Side.NO else v_cc
            predicted += int(p.contracts) * per_ct // 100
        # Reconcile to the exchange's whole-cent grid (mirrors the real
        # QuoteLifecycle.reconcile_combo_settlement): a fractional-contract scalar
        # settlement makes `predicted` sub-cent, which the integer-cent revenue can
        # never equal — only a ≥1¢ residual is a genuine mismatch (defense #3).
        if abs(predicted - expected_revenue_cc) >= 100:  # 100 cc = 1 cent
            await self._killswitch.halt(
                ReasonCode.HALT_RECONCILIATION_MISMATCH,
                f"predicted {predicted} != revenue {expected_revenue_cc}",
            )


class FakeSettlementSource:
    """Canned /portfolio/settlements pages (list of {settlements, cursor})."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self._pages = list(pages)
        self.calls = 0

    async def get_settlements(self, **params: Any) -> dict[str, Any]:
        cursor = str(params.get("cursor") or "")
        idx = 0 if not cursor else int(cursor)
        self.calls += 1
        if idx >= len(self._pages):
            return {"settlements": [], "cursor": ""}
        return self._pages[idx]


def _rig(
    conventions: Conventions = VERIFIED,
) -> tuple[ExposureBook, BalanceTracker, FakeLifecycle, KillSwitch, SettlementHandler]:
    clock = FakeClock()
    exposure = ExposureBook(conventions)
    balance = BalanceTracker(conventions, clock, stale_after_s=1e9)
    killswitch = KillSwitch(clock)
    lifecycle = FakeLifecycle(exposure, killswitch)
    handler = SettlementHandler(
        exposure=exposure,
        balance_tracker=balance,
        lifecycle=lifecycle,
        killswitch=killswitch,
    )
    return exposure, balance, lifecycle, killswitch, handler


def _position(
    combo_ticker: str = "KXMVE-C1",
    *,
    contracts: int = 100,
    entry_price: int = 5_000,
    our_side: Side = Side.NO,
    position_id: str = "fill:q1",
    farmed: bool = False,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=combo_ticker,
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=(LegRef("M1", "E1", "yes"),),
        farmed=farmed,
    )


def _settlement_row(
    ticker: str = "KXMVE-C1",
    *,
    market_result: str = "no",
    value: int | None = None,
    revenue: int,
    fee_cost: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": ticker,
        "market_result": market_result,
        "revenue": revenue,
    }
    if value is not None:
        row["value"] = value
    if fee_cost is not None:
        row["fee_cost"] = fee_cost
    return row


# --- row parsing (fail-closed) ------------------------------------------------


class TestParseSettlement:
    def test_no_result_binary_v0(self) -> None:
        parsed = parse_settlement(_settlement_row(market_result="no", revenue=100))
        assert parsed.settled_value == 0.0
        assert int(parsed.revenue_cc) == 10_000  # 100¢ ×100

    def test_yes_result_binary_v1(self) -> None:
        parsed = parse_settlement(_settlement_row(market_result="yes", revenue=0))
        assert parsed.settled_value == 1.0

    def test_scalar_result_uses_value_field(self) -> None:
        parsed = parse_settlement(
            _settlement_row(market_result="scalar", value=43, revenue=57)
        )
        assert parsed.settled_value == 0.43

    def test_scalar_without_value_raises(self) -> None:
        with pytest.raises(SettlementReconcileError, match="scalar"):
            parse_settlement(_settlement_row(market_result="scalar", revenue=0))

    def test_unreadable_market_result_raises(self) -> None:
        with pytest.raises(SettlementReconcileError, match="market_result"):
            parse_settlement(_settlement_row(market_result="maybe", revenue=0))

    def test_value_inconsistent_with_binary_result_raises(self) -> None:
        with pytest.raises(SettlementReconcileError, match="inconsistent"):
            parse_settlement(_settlement_row(market_result="no", value=100, revenue=0))

    def test_value_out_of_range_raises(self) -> None:
        with pytest.raises(SettlementReconcileError, match="out of"):
            parse_settlement(_settlement_row(market_result="scalar", value=150, revenue=0))

    def test_fee_cost_parsed_from_dollars(self) -> None:
        parsed = parse_settlement(
            _settlement_row(market_result="no", revenue=100, fee_cost="0.0175")
        )
        assert int(parsed.fee_cc) == 175  # $0.0175 = 175 cc


# --- booking + realized P&L ---------------------------------------------------


class TestBooking:
    async def test_no_miss_credits_one_dollar_minus_premium(self) -> None:
        # THE ground truth: LONG NO 1.00 ct @ $0.50, settles NO (V=0) → +$0.50.
        exposure, balance, lifecycle, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        # revenue = contracts × $1 payout = 1 ct × 100¢ = 100¢.
        rows = [_settlement_row(market_result="no", revenue=100)]
        results = await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert len(results) == 1 and results[0].booked is True
        assert results[0].realized_cc == 5_000            # +$0.50
        assert balance.realized_pnl_cc == 5_000
        assert lifecycle.realized_deltas == [5_000]       # fed the daily-loss cap

    async def test_no_hit_debits_the_premium(self) -> None:
        # LONG NO, combo HIT (settles YES, V=1) → NO worthless, lose premium.
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        # YES settle: NO holder revenue is 0.
        rows = [_settlement_row(market_result="yes", revenue=0)]
        results = await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert results[0].realized_cc == -5_000
        assert balance.realized_pnl_cc == -5_000

    async def test_fee_is_subtracted_from_realized(self) -> None:
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        rows = [_settlement_row(market_result="no", revenue=100, fee_cost="0.0175")]
        results = await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert results[0].realized_cc == 5_000 - 175      # win minus the fee
        assert balance.accrued_fees_cc == 175

    async def test_scalar_partial_payout(self) -> None:
        # V=0.7 → NO pays $0.30/ct. 1 ct @ $0.50 → realized -$0.20.
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        # revenue = 1 ct × (100−70)¢ = 30¢.
        rows = [_settlement_row(market_result="scalar", value=70, revenue=30)]
        results = await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert results[0].realized_cc == -2_000
        assert balance.realized_pnl_cc == -2_000

    async def test_fractional_contract_scalar_does_not_false_halt(self) -> None:
        # A target-cost RFQ leaves 0.90 ct (= 90 centi-contracts). On a SCALAR
        # settlement V=0.43, NO pays $0.57/ct → predicted 90 × 57¢ // 100 = 5130
        # cc = 51.3¢, which the integer-cent exchange revenue can NEVER equal. The
        # reconcile (real + FakeLifecycle mirror) reconciles to the cent, so a
        # legitimate fractional-contract scalar must NOT HALT. Exchange books the
        # true 51.3¢ as int cents (floor 51¢).
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=90, entry_price=5_000))
        rows = [_settlement_row(market_result="scalar", value=43, revenue=51)]
        results = await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert len(results) == 1 and results[0].booked is True
        assert balance.settled_count == 1
        # Realized = 51.3¢ credit − 45¢ premium (0.90 ct @ $0.50) = +6.30¢.
        assert results[0].realized_cc == 5_130 - 4_500

    async def test_fractional_contract_scalar_still_halts_on_real_mismatch(self) -> None:
        # The sub-cent tolerance does NOT weaken defense #3 through the handler:
        # a genuine ≥1¢ mismatch on the fractional-contract scalar STILL HALTs.
        exposure, _bal, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=90, entry_price=5_000))
        # Predicted 5130 cc; exchange 49¢ (4900 cc) is 230 cc ≥ 1¢ away → HALT.
        rows = [_settlement_row(market_result="scalar", value=43, revenue=49)]
        await handler.handle_settlements(rows)
        assert killswitch.halted
        assert killswitch.halt_event is not None
        assert killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH

    async def test_settlement_for_unheld_ticker_is_ignored(self) -> None:
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position("KXMVE-C1"))
        rows = [_settlement_row("KXMVE-OTHER", market_result="no", revenue=100)]
        results = await handler.handle_settlements(rows)
        assert results == []            # not ours — no book
        assert balance.realized_pnl_cc == 0
        assert not killswitch.halted


# --- idempotency --------------------------------------------------------------


class TestIdempotency:
    async def test_double_poll_no_ops(self) -> None:
        exposure, balance, lifecycle, _ks, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        rows = [_settlement_row(market_result="no", revenue=100)]
        first = await handler.handle_settlements(rows)
        assert first[0].realized_cc == 5_000
        assert first[0].booked is True
        # Re-poll the SAME settlement — no double-book. The settled position was
        # pruned from the exposure book on the first pass, so the re-poll finds
        # nothing held on the ticker and IGNORES the row (returns []). The core
        # invariant — booked exactly once — holds either way.
        second = await handler.handle_settlements(rows)
        assert second == []                                # pruned → not ours → ignored
        assert balance.realized_pnl_cc == 5_000            # still just once
        assert balance.settled_count == 1
        assert lifecycle.realized_deltas == [5_000]        # fed exactly once
        assert exposure.positions == {}                    # pruned after booking


# --- reconciliation HALTs -----------------------------------------------------


class TestReconcileHalts:
    async def test_to_the_cent_mismatch_halts(self) -> None:
        # Exchange revenue disagrees with our predicted credit → HALT.
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        # We predict 100¢ for a NO settle (1 ct × $1); the exchange booked 99¢.
        rows = [_settlement_row(market_result="no", revenue=99)]
        await handler.handle_settlements(rows)
        assert killswitch.halted
        assert killswitch.halt_event is not None
        assert killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH
        assert balance.realized_pnl_cc == 0                # nothing booked on mismatch

    async def test_unreadable_row_halts(self) -> None:
        _exp, _bal, _lc, killswitch, handler = _rig()
        rows = [_settlement_row(market_result="???", revenue=0)]
        await handler.handle_settlements(rows)
        assert killswitch.halted
        assert killswitch.halt_event is not None
        assert killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH

    async def test_farmed_settling_yes_halts(self) -> None:
        exposure, _bal, _lc, killswitch, handler = _rig()
        exposure.add_position(_position("KXMVE-C1", farmed=True, our_side=Side.NO))
        rows = [_settlement_row(market_result="yes", revenue=0)]
        await handler.handle_settlements(rows)
        assert killswitch.halted
        assert killswitch.halt_event is not None
        assert killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH

    async def test_no_credit_unverified_convention_halts_never_books_zero(self) -> None:
        # A NO credit with combo_no_pays_complement UNVERIFIED must HALT, never
        # silently book 0 (defense #2/#3).
        exposure, balance, _lc, killswitch, handler = _rig(NO_UNVERIFIED)
        exposure.add_position(_position(contracts=100, entry_price=5_000, our_side=Side.NO))
        # NO_UNVERIFIED's reconcile still predicts 100¢ (mirrors real lifecycle),
        # so it passes the revenue check, then apply_settlement refuses → HALT.
        rows = [_settlement_row(market_result="no", revenue=100)]
        await handler.handle_settlements(rows)
        assert killswitch.halted
        assert killswitch.halt_event is not None
        assert killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH
        assert balance.realized_pnl_cc == 0


# --- settled positions leave the exposure book --------------------------------


class TestSettledPositionRemoval:
    async def test_settled_position_removed_from_exposure_book(self) -> None:
        # A settled position no longer carries live risk → it must be dropped from
        # the exposure book (else settled exposure accumulates forever and keeps
        # counting toward the enforced caps + the daily-P&L mark).
        exposure, _bal, _lc, killswitch, handler = _rig()
        exposure.add_position(_position(contracts=100, entry_price=5_000))
        assert len(exposure.positions) == 1
        rows = [_settlement_row(market_result="no", revenue=100)]
        await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert exposure.positions == {}       # pruned after booking

    async def test_requote_same_ticker_after_settlement_does_not_false_halt(self) -> None:
        # Re-quote + re-fill the SAME combo ticker AFTER a prior settlement. The
        # new settlement's revenue reflects only the NEW contracts; the reconcile
        # must NOT re-sum the already-settled (removed) position → no false
        # HALT_RECONCILIATION_MISMATCH.
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(
            _position(contracts=100, entry_price=5_000, position_id="fill:q1")
        )
        # First settlement of the original 1 ct (NO, V=0 → revenue 100¢).
        await handler.handle_settlements([_settlement_row(market_result="no", revenue=100)])
        assert not killswitch.halted
        assert exposure.positions == {}

        # Re-quote + re-fill on the same ticker: a brand-new 2 ct position.
        exposure.add_position(
            _position(contracts=200, entry_price=4_000, position_id="fill:q2")
        )
        # New settlement: only the NEW 2 ct → revenue = 2 × $1 = 200¢. Without the
        # prune the reconcile would sum the OLD 1 ct + NEW 2 ct = 300¢ ≠ 200¢ and
        # HALT. With the prune it reconciles cleanly.
        await handler.handle_settlements([_settlement_row(market_result="no", revenue=200)])
        assert not killswitch.halted
        assert exposure.positions == {}
        # Both settlements booked: +$0.50 (1 ct miss) + +$1.20 (2 ct @ $0.40 miss).
        assert balance.realized_pnl_cc == 5_000 + 12_000

    async def test_multi_position_fee_split_still_exact_after_prune(self) -> None:
        # Two positions on one ticker with a nonzero settlement fee: the exact fee
        # split (by contract weight over the WHOLE ticker) must survive the mid-
        # loop prune — settlements are built against the full book first.
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(
            _position(contracts=100, entry_price=5_000, position_id="fill:q1")
        )
        exposure.add_position(
            _position(contracts=300, entry_price=5_000, position_id="fill:q2")
        )
        # 1 ct + 3 ct = 4 ct total; NO miss → revenue 400¢. Fee $0.04 = 400 cc,
        # split 1:3 → 100 cc + 300 cc = 400 cc (exact).
        rows = [_settlement_row(market_result="no", revenue=400, fee_cost="0.04")]
        await handler.handle_settlements(rows)
        assert not killswitch.halted
        assert exposure.positions == {}
        assert balance.accrued_fees_cc == 400        # fees sum exactly to $0.04


# --- poller paging ------------------------------------------------------------


class TestPoller:
    async def test_poll_once_pages_to_exhaustion_and_books(self) -> None:
        exposure, balance, _lc, killswitch, handler = _rig()
        exposure.add_position(_position("KXMVE-C1", contracts=100, entry_price=5_000))
        exposure.add_position(
            _position("KXMVE-C2", contracts=100, entry_price=4_000, position_id="fill:q2")
        )
        source = FakeSettlementSource(
            {"settlements": [_settlement_row("KXMVE-C1", market_result="no", revenue=100)],
             "cursor": "1"},
            {"settlements": [_settlement_row("KXMVE-C2", market_result="no", revenue=100)],
             "cursor": ""},
        )
        poller = SettlementPoller(source=source, handler=handler, poll_interval_s=1.0)
        results = await poller.poll_once()
        assert not killswitch.halted
        assert {r.combo_ticker for r in results} == {"KXMVE-C1", "KXMVE-C2"}
        # C1 miss +$0.50, C2 miss +$0.60 → +$1.10 total.
        assert balance.realized_pnl_cc == 5_000 + 6_000

    async def test_empty_poll_is_a_noop(self) -> None:
        _exp, balance, _lc, killswitch, handler = _rig()
        poller = SettlementPoller(
            source=FakeSettlementSource(), handler=handler, poll_interval_s=1.0
        )
        assert await poller.poll_once() == []
        assert balance.realized_pnl_cc == 0
        assert not killswitch.halted
