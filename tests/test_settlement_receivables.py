"""SETTLEMENT RECEIVABLES — the give-back cascade shield (2026-07-19 live kill).

The failure this pins: during the ESPARG settlement cascade the exchange removed
settled positions from ``portfolio_value`` BEFORE crediting ``balance``, so
exchange equity transiently dipped by exactly the in-flight settlement value.
The give-back hard-trip read the trough as a $430.69 drawdown (≥ 3/25 bankroll,
human-only KILL) whose REAL settlement losers were $29.51 — a verified false
positive that held the bot down through the night.

The fix under test, end to end:

1. ``BalanceTracker`` receivable ledger: ``note_receivable`` (fact sweep) →
   ``confirm_receivable`` (settlement reconciler) → dropped at the first
   successful poll whose request STARTED after the confirm (that reading
   provably contains the credited cash) → TTL backstop expires a
   never-confirmed receivable loudly.
2. ``limits.check`` give-back halts measure ``max(0, peak − current − pending)``
   — receivables only REDUCE the measured give-back; peak/current stay raw so a
   receivable can never inflate a peak, and a LOSING position notes no
   receivable so a genuine loss cascade still measures in full.
3. ``QuoteLifecycle._refresh_settlement_receivables``: notes a receivable ONLY
   when EVERY leg of a held position carries an exchange-graded FACT (doubt ⇒
   no receivable — the shield fails closed toward halting).
4. ``SettlementHandler`` confirms the receivable when it books the exchange row.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.persistence import Store
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.risk.limits import Breach, DailyPnl, HaltInputs
from combomaker.risk.settlement import SettlementHandler
from tests.test_balance import VERIFIED, FakeBalanceSource
from tests.test_filters import Harness
from tests.test_limits_caps import (
    BANKROLL_2K,
    LOOSE,
    MARG,
    checker,
    empty_book,
    r2_reasons,
)
from tests.test_settled_marginals import (
    FRA_WIN,
    FRAENG_EVENT,
    FRAENG_O5,
    FakeMarketSource,
    _build,
    _resolver,
    harness,  # noqa: F401 — pytest fixture import
    market_payload,
)
from tests.test_settlement import FakeLifecycle as SettlementFakeLifecycle
from tests.test_settlement import _position, _settlement_row

CC = CentiCents
Q = CentiContracts

PID = "fill:q1"


def _payload(cash_cents: int, portfolio_cents: int) -> dict[str, Any]:
    return {"balance": cash_cents, "portfolio_value": portfolio_cents}


def _tracker(
    *, ttl_s: float = 1800.0, stale_after_s: float = 1e9
) -> tuple[BalanceTracker, FakeClock]:
    clock = FakeClock()
    return (
        BalanceTracker(
            VERIFIED, clock, stale_after_s=stale_after_s, receivable_ttl_s=ttl_s
        ),
        clock,
    )


# --------------------------------------------------------------------------- #
# 1. BalanceTracker receivable ledger.                                         #
# --------------------------------------------------------------------------- #


class TestReceivableLedger:
    def test_note_and_pending_sum(self) -> None:
        tracker, _clock = _tracker()
        tracker.note_receivable("p1", 10_000)
        tracker.note_receivable("p2", 2_500)
        assert tracker.pending_receivables_cc() == 12_500

    def test_zero_or_negative_amount_ignored(self) -> None:
        # A LOSING position predicts credit 0 — it must never note a receivable,
        # so a genuine loss cascade is never shielded.
        tracker, _clock = _tracker()
        tracker.note_receivable("loser", 0)
        tracker.note_receivable("bad", -5)
        assert tracker.pending_receivables_cc() == 0

    def test_renote_updates_amount_without_duplication(self) -> None:
        tracker, _clock = _tracker()
        tracker.note_receivable("p1", 10_000)
        tracker.note_receivable("p1", 10_000)  # sweep runs every tick
        assert tracker.pending_receivables_cc() == 10_000

    async def test_confirm_then_later_poll_drops(self) -> None:
        # The drop rule: a poll whose request STARTED after the confirm instant
        # provably contains the credited cash — the shield lifts exactly then.
        tracker, clock = _tracker()
        tracker.note_receivable("p1", 10_000)
        tracker.confirm_receivable("p1")
        clock.advance(1.0)
        await tracker.refresh(FakeBalanceSource(_payload(200_000, 0)))
        assert tracker.pending_receivables_cc() == 0

    async def test_poll_started_before_confirm_does_not_drop(self) -> None:
        # Ordering safety: a refresh at t0 does not drop a receivable confirmed
        # AFTER t0 — only the NEXT poll (request started post-confirm) does.
        tracker, clock = _tracker()
        tracker.note_receivable("p1", 10_000)
        await tracker.refresh(FakeBalanceSource(_payload(200_000, 0)))
        tracker.confirm_receivable("p1")
        assert tracker.pending_receivables_cc() == 10_000  # still shielding
        clock.advance(1.0)
        await tracker.refresh(FakeBalanceSource(_payload(210_000, 0)))
        assert tracker.pending_receivables_cc() == 0

    async def test_unconfirmed_receivable_survives_polls(self) -> None:
        # Cash not yet reconciled ⇒ the shield holds through any number of polls.
        tracker, clock = _tracker()
        tracker.note_receivable("p1", 10_000)
        for _ in range(3):
            clock.advance(15.0)
            await tracker.refresh(FakeBalanceSource(_payload(200_000, 0)))
        assert tracker.pending_receivables_cc() == 10_000

    def test_ttl_expiry_restores_raw_measurement(self) -> None:
        # Backstop: a receivable the reconciler never confirms stops shielding
        # after the TTL — if the cash truly is not coming the give-back must
        # become visible again (fail-closed).
        tracker, clock = _tracker(ttl_s=60.0)
        tracker.note_receivable("p1", 10_000)
        clock.advance(59.0)
        assert tracker.pending_receivables_cc() == 10_000
        clock.advance(2.0)
        assert tracker.pending_receivables_cc() == 0

    def test_confirmed_receivable_is_ttl_immune(self) -> None:
        # Confirmed = the exchange row is booked; only the poll drop rule may
        # remove it (the cash is verifiably en route — TTL is for the
        # never-confirmed case).
        tracker, clock = _tracker(ttl_s=60.0)
        tracker.note_receivable("p1", 10_000)
        tracker.confirm_receivable("p1")
        clock.advance(120.0)
        assert tracker.pending_receivables_cc() == 10_000

    async def test_confirmed_and_dropped_never_resurrects(self) -> None:
        # The fact sweep races the reconciler: re-noting after confirm/drop must
        # not resurrect the receivable (the cash is already in the reading).
        tracker, clock = _tracker()
        tracker.note_receivable("p1", 10_000)
        tracker.confirm_receivable("p1")
        tracker.note_receivable("p1", 10_000)  # sweep tick between confirm+poll
        clock.advance(1.0)
        await tracker.refresh(FakeBalanceSource(_payload(200_000, 0)))
        assert tracker.pending_receivables_cc() == 0
        # After the drop a re-note re-enters: production can only reach this if
        # the position is genuinely still held (the handler removes settled
        # positions before any later sweep tick), so it is a legitimate note.
        tracker.note_receivable("p1", 10_000)
        assert tracker.pending_receivables_cc() == 10_000

    def test_confirm_unknown_position_is_noop(self) -> None:
        # The reconciler confirms everything it books; not everything was
        # fact-resolved first.
        tracker, _clock = _tracker()
        tracker.confirm_receivable("never-noted")
        assert tracker.pending_receivables_cc() == 0

    async def test_receivables_never_touch_equity_or_peak(self) -> None:
        # The shield must not fabricate equity: peak and current equity read
        # EXACTLY the exchange payload regardless of pending receivables.
        tracker, clock = _tracker()
        await tracker.refresh(FakeBalanceSource(_payload(100_000, 100_000)))
        tracker.note_receivable("p1", 500_000)
        clock.advance(1.0)
        await tracker.refresh(FakeBalanceSource(_payload(100_000, 100_000)))
        assert int(tracker.exchange_equity_cc) == 20_000_000
        assert int(tracker.peak_equity_cc) == 20_000_000


# --------------------------------------------------------------------------- #
# 2. Give-back halts measure max(0, peak − current − pending).                 #
# --------------------------------------------------------------------------- #


class TestGiveBackWithReceivables:
    def _run(self, halt: HaltInputs) -> list[Breach]:
        overrides = dict(LOOSE)
        overrides.update(
            drawdown_frac=Fraction(10, 100), hard_trip_frac=Fraction(12, 100)
        )
        return checker(**overrides).check(
            empty_book(), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K, halt_inputs=halt,
        )

    def test_cascade_trough_fully_shielded(self) -> None:
        # The live shape: raw give-back 12% of bankroll (KILL territory) is
        # exactly in-flight KNOWN-outcome settlement credit ⇒ measured 0.
        halt = HaltInputs(
            peak_equity_cc=20_000_000,
            current_equity_cc=17_600_000,
            pending_settlement_credit_cc=2_400_000,
        )
        rs = r2_reasons(self._run(halt))
        assert ReasonCode.HALT_HARD_TRIP not in rs
        assert ReasonCode.HALT_DRAWDOWN not in rs

    def test_losses_beyond_receivables_still_trip(self) -> None:
        # Real loss riding the cascade: raw give-back 24%, receivables cover
        # half ⇒ measured 12% still hard-trips (losers carry no receivable).
        halt = HaltInputs(
            peak_equity_cc=20_000_000,
            current_equity_cc=15_200_000,
            pending_settlement_credit_cc=2_400_000,
        )
        rs = r2_reasons(self._run(halt))
        assert ReasonCode.HALT_HARD_TRIP in rs
        assert ReasonCode.HALT_DRAWDOWN in rs

    def test_zero_pending_is_exact_prefix_behaviour(self) -> None:
        # Default 0 ⇒ the raw measurement, byte-identical thresholds.
        halt = HaltInputs(peak_equity_cc=20_000_000, current_equity_cc=17_600_000)
        rs = r2_reasons(self._run(halt))
        assert ReasonCode.HALT_HARD_TRIP in rs

    def test_receivables_exceeding_giveback_floor_at_zero(self) -> None:
        # Over-shield floors at 0 — never a negative give-back that could mask
        # a LATER real drawdown by carrying a credit balance.
        halt = HaltInputs(
            peak_equity_cc=20_000_000,
            current_equity_cc=19_000_000,
            pending_settlement_credit_cc=5_000_000,
        )
        rs = r2_reasons(self._run(halt))
        assert ReasonCode.HALT_HARD_TRIP not in rs
        assert ReasonCode.HALT_DRAWDOWN not in rs

    def test_breach_detail_reports_raw_and_receivables(self) -> None:
        # Telemetry: a shielded-but-still-tripping breach names both figures so
        # the operator sees the decomposition in the halt line itself.
        halt = HaltInputs(
            peak_equity_cc=20_000_000,
            current_equity_cc=15_200_000,
            pending_settlement_credit_cc=2_400_000,
        )
        hard = [
            b for b in self._run(halt) if b.reason is ReasonCode.HALT_HARD_TRIP
        ]
        assert hard and "raw 4800000cc" in hard[0].detail
        assert "receivables 2400000cc" in hard[0].detail


# --------------------------------------------------------------------------- #
# 3. Lifecycle fact sweep: facts-only, losers note nothing, doubt notes        #
#    nothing; pending threads into HaltInputs.                                 #
# --------------------------------------------------------------------------- #


class _RecordingBankroll:
    """The _FixedBankroll seam + a recording receivable ledger."""

    def __init__(self, pending_cc: int = 0) -> None:
        self.noted: list[tuple[str, int]] = []
        self._pending = pending_cc

    def risk_bankroll_cc_or_none(self) -> int | None:
        return 10**11

    def peak_equity_cc_or_none(self) -> int | None:
        return None

    def exchange_equity_cc_or_none(self) -> int | None:
        return None

    def available_cash_cc_or_none(self) -> int | None:
        return None

    def pending_receivables_cc(self) -> int:
        return self._pending

    def note_receivable(self, position_id: str, amount_cc: int) -> None:
        self.noted.append((position_id, amount_cc))


def _two_leg_position(
    pid: str, *, fra_side: str = "yes", o5_side: str = "no"
) -> OpenPosition:
    """Both legs on the SETTLED game — outcome fully known once facts land."""
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=Q(100),
        entry_price_cc=CC(5_000),
        legs=(
            LegRef(FRA_WIN, FRAENG_EVENT, fra_side),
            LegRef(FRAENG_O5, FRAENG_EVENT, o5_side),
        ),
    )


class TestLifecycleFactSweep:
    async def _facted_resolver(
        self, h: Harness, *, fra_result: str, o5_result: str
    ) -> Any:
        source = FakeMarketSource()
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="determined", result=fra_result
        )
        source.payloads[FRAENG_O5] = market_payload(
            FRAENG_O5, status="finalized", result=o5_result
        )
        settled = _resolver(source, h.clock)
        settled.note_missing(FRA_WIN)
        settled.note_missing(FRAENG_O5)
        await settled.resolve_pending()
        return settled

    async def test_winner_notes_predicted_credit(
        self, harness: tuple[Harness, Store]  # noqa: F811
    ) -> None:
        # Combo misses (V=0): LONG NO pays the full $1 per contract — the
        # predicted credit 100 ct × 10_000 cc // 100 = 10_000 cc is noted.
        h, store = harness
        settled = await self._facted_resolver(h, fra_result="no", o5_result="no")
        lifecycle, _s, exposure = _build(h, store, bankroll_cc=10**11, settled=settled)
        bank = _RecordingBankroll()
        lifecycle._balance = bank  # noqa: SLF001 — recorder seam
        exposure.add_position(_two_leg_position("pw"))
        lifecycle._refresh_settlement_receivables()  # noqa: SLF001
        assert bank.noted == [("pw", 10_000)]

    async def test_loser_notes_nothing(
        self, harness: tuple[Harness, Store]  # noqa: F811
    ) -> None:
        # Combo hits (V=1): LONG NO forfeits the premium, credit 0 — no
        # receivable, so a loss cascade is never shielded.
        h, store = harness
        settled = await self._facted_resolver(h, fra_result="yes", o5_result="no")
        lifecycle, _s, exposure = _build(h, store, bankroll_cc=10**11, settled=settled)
        bank = _RecordingBankroll()
        lifecycle._balance = bank  # noqa: SLF001
        # yes:FRA(fact 1) → 1; no:O5(fact 0) → 1 ⇒ V_combo = 1 (full hit).
        exposure.add_position(_two_leg_position("pl"))
        lifecycle._refresh_settlement_receivables()  # noqa: SLF001
        assert bank.noted == []

    async def test_missing_fact_notes_nothing(
        self, harness: tuple[Harness, Store]  # noqa: F811
    ) -> None:
        # One leg still live (M1 has no graded fact): doubt ⇒ NO receivable —
        # the shield fails closed toward halting.
        h, store = harness
        settled = await self._facted_resolver(h, fra_result="no", o5_result="no")
        lifecycle, _s, exposure = _build(h, store, bankroll_cc=10**11, settled=settled)
        bank = _RecordingBankroll()
        lifecycle._balance = bank  # noqa: SLF001
        exposure.add_position(
            OpenPosition(
                position_id="px",
                combo_ticker="COMBO-px",
                collection=None,
                our_side=Side.NO,
                contracts=Q(100),
                entry_price_cc=CC(5_000),
                legs=(
                    LegRef(FRA_WIN, FRAENG_EVENT, "yes"),
                    LegRef("M1", "E1", "no"),  # live leg — no fact
                ),
            )
        )
        lifecycle._refresh_settlement_receivables()  # noqa: SLF001
        assert bank.noted == []

    async def test_no_resolver_is_noop(
        self, harness: tuple[Harness, Store]  # noqa: F811
    ) -> None:
        h, store = harness
        lifecycle, _s, exposure = _build(h, store, bankroll_cc=10**11, settled=None)
        bank = _RecordingBankroll()
        lifecycle._balance = bank  # noqa: SLF001
        exposure.add_position(_two_leg_position("pn"))
        lifecycle._refresh_settlement_receivables()  # noqa: SLF001
        assert bank.noted == []

    async def test_halt_inputs_carry_pending(
        self, harness: tuple[Harness, Store]  # noqa: F811
    ) -> None:
        h, store = harness
        lifecycle, _s, _e = _build(h, store, bankroll_cc=10**11, settled=None)
        lifecycle._balance = _RecordingBankroll(pending_cc=1_234)  # noqa: SLF001
        assert lifecycle._halt_inputs().pending_settlement_credit_cc == 1_234  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 4. SettlementHandler confirms the receivable when it books the row.          #
# --------------------------------------------------------------------------- #


class TestHandlerConfirms:
    async def test_booked_settlement_confirms_receivable(self, tmp_path: Path) -> None:
        from combomaker.risk.exposure import ExposureBook
        from combomaker.risk.killswitch import KillSwitch

        clock = FakeClock()
        exposure = ExposureBook(VERIFIED)
        balance = BalanceTracker(VERIFIED, clock, stale_after_s=1e9)
        killswitch = KillSwitch(clock)
        handler = SettlementHandler(
            exposure=exposure,
            balance_tracker=balance,
            lifecycle=SettlementFakeLifecycle(exposure, killswitch),
            killswitch=killswitch,
        )
        exposure.add_position(_position(position_id=PID))
        # Fact sweep noted the receivable while the cash was in flight.
        balance.note_receivable(PID, 10_000)
        # Settles NO (V=0): our 1.00-ct LONG NO pays $1.00 → revenue 100¢.
        await handler.handle_settlements(
            [_settlement_row(market_result="no", revenue=100)]
        )
        assert not killswitch.halted
        # Confirmed but not yet observed in a poll: still shielding.
        assert balance.pending_receivables_cc() == 10_000
        clock.advance(1.0)
        await balance.refresh(FakeBalanceSource(_payload(200_000, 0)))
        assert balance.pending_receivables_cc() == 0
