"""P1-7 — Audit mutex metadata; retain explicit-True-only netting; add
settlement tripwires (RISK_ENGINE_AUDIT_ACTION_PLAN.txt P1 item 7).

Three parts, one per concern of the plan line:

  (A) EXPLICIT-TRUE-ONLY netting is RETAINED. The loss/directional netting nets a
      per-game RESULT event's opposing outcomes ONLY when the metadata answers
      ``is_me_event(e) is True``. A None (UNKNOWN) or False flag must NEVER net —
      it fails closed to the comonotone / summed bound. Pinned here against
      regression (the netting credit is what the settlement tripwire below
      backstops, so loosening the gate silently would defeat the tripwire).

  (B) The mutex-metadata SETTLEMENT TRIPWIRE. Metadata (even explicit-True) is not
      ground truth; the exchange settlement is. If ≥2 distinct outcome markets of
      one netted (explicit-True) ME event both settle YES, the exclusivity our
      netting credited was FALSE and we UNDER-stated risk → HALT
      HALT_RECONCILIATION_MISMATCH (never a log), mirroring the farmed settle-YES
      tripwire. Audited BEFORE settled positions are removed, and ONLY on events
      we actually netted (None/False → summed comonotone → never a false trip).

Real BalanceTracker + ExposureBook + KillSwitch; fakes only for the REST-shaped
lifecycle/source slices (no live credentials). No live module edited beyond the
audited risk engine itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    _mutex_directional_game_cc,
    _mutex_game_worst_cc,
    mutex_exclusivity_violations,
)
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.settlement import SettlementHandler

CC = CentiCents
Q = CentiContracts

CONV = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# One advance event (mutually exclusive: exactly one of ARG/ENG advances).
ADV = "KXWCADVANCE-26JUL15ENGARG"
ARG_MKT = "KXWCADVANCE-26JUL15ENGARG-ARG"
ENG_MKT = "KXWCADVANCE-26JUL15ENGARG-ENG"
ARG = LegRef(ARG_MKT, ADV, "yes")
ENG = LegRef(ENG_MKT, ADV, "yes")
# A totals market on the same game — NOT a mutually-exclusive RESULT event.
TOT_MKT = "KXWCTOTAL-26JUL15ENGARG-3"
TOT = LegRef(TOT_MKT, "KXWCTOTAL-26JUL15ENGARG", "yes")


def ME_ADV(e: str) -> bool | None:
    """Only the advance event is flagged mutually exclusive; everything else is
    UNKNOWN (None) — the real metadata cache's peek semantics."""
    return True if e == ADV else None


def ME_FALSE(e: str) -> bool:
    """Metadata that explicitly says NOT mutually exclusive."""
    return False


# --------------------------- (A) explicit-True-only netting ------------------


class TestExplicitTrueOnlyNettingRetained:
    def _entry(self, leg: LegRef, loss: int, requires: bool = True):
        return ((leg,), loss, requires)

    def test_true_flag_nets_opposing_outcomes_loss_axis(self) -> None:
        # ARG-advance ⊥ ENG-advance under explicit True → nets to max, not sum.
        entries = [self._entry(ARG, 10), self._entry(ENG, 10)]
        assert _mutex_game_worst_cc(entries, ME_ADV) == 10

    def test_none_flag_never_nets_loss_axis(self) -> None:
        # UNKNOWN (None) is NOT True → fail closed to the comonotone sum.
        entries = [self._entry(ARG, 10), self._entry(ENG, 10)]
        assert _mutex_game_worst_cc(entries, lambda e: None) == 20

    def test_false_flag_never_nets_loss_axis(self) -> None:
        # Explicit False is NOT True → fail closed to the comonotone sum.
        entries = [self._entry(ARG, 10), self._entry(ENG, 10)]
        assert _mutex_game_worst_cc(entries, ME_FALSE) == 20

    def test_true_flag_nets_directional_axis(self) -> None:
        d = [((ARG,), 10.0, True), ((ENG,), 10.0, True)]
        assert _mutex_directional_game_cc(d, ME_ADV) == 10

    def test_none_and_false_never_net_directional_axis(self) -> None:
        d = [((ARG,), 10.0, True), ((ENG,), 10.0, True)]
        assert _mutex_directional_game_cc(d, lambda e: None) == 20
        assert _mutex_directional_game_cc(d, ME_FALSE) == 20


# --------------------------- (B) pure violation classifier -------------------


class TestMutexExclusivityViolations:
    def test_single_yes_is_fine(self) -> None:
        assert mutex_exclusivity_violations({ADV: {ARG_MKT}}) == {}

    def test_two_yes_same_event_is_a_violation(self) -> None:
        v = mutex_exclusivity_violations({ADV: {ARG_MKT, ENG_MKT}})
        assert v == {ADV: {ARG_MKT, ENG_MKT}}

    def test_empty_is_fine(self) -> None:
        assert mutex_exclusivity_violations({}) == {}

    def test_only_multi_yes_events_reported(self) -> None:
        other = "KXWCADVANCE-26JUL15BRAFRA"
        v = mutex_exclusivity_violations(
            {ADV: {ARG_MKT, ENG_MKT}, other: {"KXWCADVANCE-26JUL15BRAFRA-BRA"}}
        )
        assert set(v) == {ADV}


# --------------------------- (B) book-level audit ----------------------------


def _pos(pid: str, leg: LegRef, combo: str) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=combo,
        collection=None,
        our_side=Side.NO,
        contracts=Q(100),
        entry_price_cc=CC(5_000),
        legs=(leg,),
    )


class TestBookAuditMutexSettlements:
    def test_two_netted_outcomes_settle_yes_is_a_violation(self) -> None:
        # We held (and NETTED) long-NO on ARG-advance and long-NO on ENG-advance.
        # If BOTH advance markets settle YES, the exclusivity we credited is false.
        book = ExposureBook(CONV, is_me_event=ME_ADV)
        book.add_position(_pos("p1", ARG, "C-arg"))
        book.add_position(_pos("p2", ENG, "C-eng"))
        v = book.audit_mutex_settlements([ARG_MKT, ENG_MKT])
        assert v == {ADV: {ARG_MKT, ENG_MKT}}

    def test_one_outcome_settles_yes_is_fine(self) -> None:
        book = ExposureBook(CONV, is_me_event=ME_ADV)
        book.add_position(_pos("p1", ARG, "C-arg"))
        book.add_position(_pos("p2", ENG, "C-eng"))
        assert book.audit_mutex_settlements([ARG_MKT]) == {}

    def test_non_me_event_multi_yes_never_trips(self) -> None:
        # A totals market is UNKNOWN (None) → never netted → multi-YES there is not
        # a tripwire (it was already priced comonotone). Two DIFFERENT totals
        # markets on one event settling YES must NOT trip.
        tot_b = LegRef("KXWCTOTAL-26JUL15ENGARG-4", "KXWCTOTAL-26JUL15ENGARG", "yes")
        book = ExposureBook(CONV, is_me_event=ME_ADV)
        book.add_position(_pos("p1", TOT, "C-t3"))
        book.add_position(_pos("p2", tot_b, "C-t4"))
        assert book.audit_mutex_settlements([TOT_MKT, tot_b.market_ticker]) == {}

    def test_no_metadata_provider_never_trips(self) -> None:
        # A fresh/paper book with no is_me_event never netted → never trips.
        book = ExposureBook(CONV)  # no is_me_event
        book.add_position(_pos("p1", ARG, "C-arg"))
        book.add_position(_pos("p2", ENG, "C-eng"))
        assert book.audit_mutex_settlements([ARG_MKT, ENG_MKT]) == {}

    def test_false_flag_never_trips(self) -> None:
        # Explicit False → never netted → multi-YES is not a tripwire.
        book = ExposureBook(CONV, is_me_event=ME_FALSE)
        book.add_position(_pos("p1", ARG, "C-arg"))
        book.add_position(_pos("p2", ENG, "C-eng"))
        assert book.audit_mutex_settlements([ARG_MKT, ENG_MKT]) == {}


# --------------------------- (B) settlement handler HALT ---------------------


class _FakeLifecycle:
    """Minimal reconcile slice — records realized P&L; the mutex tripwire fires
    in the handler BEFORE any of this runs, so a plain no-op reconcile suffices."""

    def __init__(self) -> None:
        self.realized_deltas: list[int] = []

    def record_realized_pnl(self, delta_cc: int) -> None:
        self.realized_deltas.append(delta_cc)

    async def reconcile_combo_settlement(
        self,
        combo_ticker: str,
        *,
        settled_yes: bool = False,
        settled_value: float | None = None,
        expected_revenue_cc: int | None = None,
    ) -> None:
        return None


def _rig(is_me_event=ME_ADV):
    clock = FakeClock()
    exposure = ExposureBook(CONV, is_me_event=is_me_event)
    balance = BalanceTracker(CONV, clock, stale_after_s=1e9)
    killswitch = KillSwitch(clock)
    lifecycle = _FakeLifecycle()
    handler = SettlementHandler(
        exposure=exposure,
        balance_tracker=balance,
        lifecycle=lifecycle,
        killswitch=killswitch,
    )
    return exposure, killswitch, handler


def _yes_row(ticker: str) -> dict[str, Any]:
    return {"ticker": ticker, "market_result": "yes", "revenue": 0}


class TestSettlementHandlerMutexTripwire:
    @pytest.mark.asyncio
    async def test_two_me_outcomes_settle_yes_halts(self) -> None:
        exposure, killswitch, handler = _rig()
        # Two long-NO positions our netting treated as opposing ME outcomes.
        exposure.add_position(_pos("p1", ARG, "C-arg"))
        exposure.add_position(_pos("p2", ENG, "C-eng"))
        # The exchange settles BOTH advance markets YES — impossible if truly ME.
        await handler.handle_settlements([_yes_row(ARG_MKT), _yes_row(ENG_MKT)])
        assert killswitch.halted
        assert killswitch.halt_event is not None
        assert killswitch.halt_event.reason is ReasonCode.HALT_RECONCILIATION_MISMATCH

    @pytest.mark.asyncio
    async def test_single_me_outcome_yes_does_not_halt(self) -> None:
        exposure, killswitch, handler = _rig()
        exposure.add_position(_pos("p1", ARG, "C-arg"))
        exposure.add_position(_pos("p2", ENG, "C-eng"))
        # Exactly ONE advance market settles YES — exclusivity held; no trip.
        await handler.handle_settlements([_yes_row(ARG_MKT)])
        assert not killswitch.halted

    @pytest.mark.asyncio
    async def test_non_me_multi_yes_does_not_halt(self) -> None:
        # Two totals markets (UNKNOWN, never netted) settle YES → no trip.
        tot_b = LegRef("KXWCTOTAL-26JUL15ENGARG-4", "KXWCTOTAL-26JUL15ENGARG", "yes")
        exposure, killswitch, handler = _rig()
        exposure.add_position(_pos("p1", TOT, "C-t3"))
        exposure.add_position(_pos("p2", tot_b, "C-t4"))
        await handler.handle_settlements(
            [_yes_row(TOT_MKT), _yes_row(tot_b.market_ticker)]
        )
        assert not killswitch.halted

    @pytest.mark.asyncio
    async def test_tripwire_fires_before_positions_removed(self) -> None:
        # The audit must run while positions are still in the book (the map is
        # derived from held legs). Confirm the HALT lands even though a NO-settling
        # position on one of the tickers would otherwise be booked+removed.
        exposure, killswitch, handler = _rig()
        exposure.add_position(_pos("p1", ARG, "C-arg"))
        exposure.add_position(_pos("p2", ENG, "C-eng"))
        await handler.handle_settlements([_yes_row(ARG_MKT), _yes_row(ENG_MKT)])
        assert killswitch.halted
        # No realized P&L was booked — the batch halted before any reconcile.
        assert handler._reconciled == set()


def test_directional_bound_bytes_identical_unaffected() -> None:
    """Sanity: adding the audit did not perturb the netting numbers themselves —
    the mutex-aware directional bound still nets opposing advances to $1.00."""
    book = ExposureBook(CONV, is_me_event=ME_ADV)
    book.add_position(_pos("p1", ARG, "C-arg"))
    book.add_position(_pos("p2", ENG, "C-eng"))
    snap = book.snapshot(lambda t: 0.5, mass_acceptance=False)
    assert snap.directional_by_game_cc["26JUL15ENGARG"] == CC_PER_DOLLAR
