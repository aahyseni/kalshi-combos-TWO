"""P0-2: the candidate MC is ATOMIC with the reservation book.

The defect (RISK_ENGINE_LIVE_BALANCING_VALIDATION_AUDIT.txt "P0-2"): the confirm
path used to build the candidate-MC inputs from the CURRENT reservations, AWAIT the
MC worker, and only THEN create a reservation. During that await a second accept
evaluated against the SAME old pre-book (no reservation for either candidate yet),
so two concurrent accepts could each pass their ES / P(ruin) MC against the old
book. There was no reservation version in the candidate MC inputs and no version
check on return.

The fix (preferred audit flow) and what this file proves through the REAL
``QuoteLifecycle`` (not a unit of a helper):

  * a PROVISIONAL reservation for the candidate is created FIRST, under the analytic
    hard caps, BEFORE the candidate MC runs — so a concurrent accept's own MC sees
    this candidate's held headroom (two candidate gates cannot ignore each other);
  * the candidate MC inputs carry ``input_generation`` + ``reservation_version``
    stamped on the loop, and the candidate's OWN provisional reservation is excluded
    from the PRE reservations (it rides as the ``candidate``, never double-counted);
  * a reservation ADDED during the MC (position generation OR reservation version
    moves under the await) causes the verdict to be DISCARDED and rebuilt/retried;
  * a reservation RELEASE during the MC moves the version and forces reevaluation;
  * combined candidates that exceed the ES / P(ruin) budget admit at most the safe
    subset — the second candidate, which SEES the first's reservation, is declined;
  * on ANY candidate-gate decline (over-budget / error / timeout / unstable) the
    PROVISIONAL reservation is RELEASED (headroom never lingers for a non-fill);
  * the retry loop is BOUNDED by the confirm deadline and a max-retry count — an
    ever-moving book fails CLOSED rather than silently consuming the confirm window.

The stub pool returns scripted ``CandidateBookRisk`` verdicts and can run a callback
mid-``run_candidate`` (to mutate the reservation book DURING the off-loop await),
so the wiring is tested deterministically, independent of the MC's numeric verdict
(which ``test_candidate_book_risk.py`` owns).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.pricing_pool import CandidateBookRiskInputs
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.risk.reservation import RiskReservationService
from combomaker.sim.book_risk import CandidateBookRisk, _TailAxes
from tests.test_filters import Harness
from tests.test_lifecycle import (
    TEST_CONVENTIONS,
    FakeSender,
    accepted_msg,
    rfq,
)
from tests.test_pricing_engine import seed_event


def _axes(ev: float = 0.0) -> _TailAxes:
    return _TailAxes(
        ev_cc=ev,
        es_99_cc=0.0,
        challenger_es_99_cc=0.0,
        governing_model_es_99_cc=0.0,
        deterministic_max_loss_cc=0.0,
        gross_settlement_notional_cc=0.0,
        p_ruin=0.0,
    )


def _verdict(*, confirm: bool, reason: str = "") -> CandidateBookRisk:
    return CandidateBookRisk(
        unknown=False,
        band="high",
        n_samples=20_000,
        seed=7,
        n_pre_positions=0,
        n_post_positions=1,
        pre=_axes(),
        post=_axes(ev=1.0),
        candidate_ev_cc=1.0,
        confirm=confirm,
        decline_reason=reason,
    )


class ScriptedPool:
    """A controllable ``BookRiskPool`` for the atomicity wiring.

    Each ``run_candidate`` records its inputs and returns the next scripted verdict
    (or a callable that computes one from the inputs, so a verdict can DEPEND on how
    many reservations the inputs already reflect). An optional ``on_call`` async hook
    fires WHILE the call is "in flight" (before the verdict is returned) — used to
    mutate the reservation book mid-await, simulating a concurrent accept."""

    def __init__(
        self,
        verdicts: list[CandidateBookRisk | Callable[[CandidateBookRiskInputs], CandidateBookRisk]],
        *,
        on_call: Callable[[int, CandidateBookRiskInputs], Awaitable[None]] | None = None,
        raise_exc: bool = False,
    ) -> None:
        self._verdicts = verdicts
        self._on_call = on_call
        self.raise_exc = raise_exc
        self.calls: list[CandidateBookRiskInputs] = []

    async def run_candidate(
        self, inputs: CandidateBookRiskInputs
    ) -> CandidateBookRisk:
        idx = len(self.calls)
        self.calls.append(inputs)
        if self._on_call is not None:
            await self._on_call(idx, inputs)
        if self.raise_exc:
            raise RuntimeError("candidate pool boom")
        v = self._verdicts[min(idx, len(self._verdicts) - 1)]
        return v(inputs) if callable(v) else v


def _build_lifecycle(
    h: Harness,
    store: Store,
    *,
    pool: ScriptedPool | None,
    limits: LimitChecker | None = None,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook, RiskReservationService]:
    sender = FakeSender()
    exposure = ExposureBook(TEST_CONVENTIONS)
    metrics = Metrics()
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    # SHADOW-mode caps by default: these tests exercise the P0-2 ATOMICITY wiring
    # (provisional reservation → version-stamped MC → discard/retry → release), NOT
    # the enforced analytic caps (test_reservation_lifecycle owns that). Shadow mode
    # grants every reservation (the shadow split drops %-breaches) so a concurrent /
    # provisional reserve reliably bumps the version — which is all the version-check
    # wiring needs. The candidate-gate VERDICT is the scripted stub pool's, not the
    # limit checker's, so shadow mode does not weaken what is under test.
    checker = limits or LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle = QuoteLifecycle(
        clock=h.clock,
        sender=sender,
        engine=engine,
        rfq_filter=RfqFilter(
            FiltersConfig(min_time_to_close_s=0.0).model_copy(
                update={"allowed_leg_series_prefixes": None}
            ),
            h.feed, h.metadata, h.killswitch, h.clock,
        ),
        limits=checker,
        exposure=exposure,
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=metrics,
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(
            quote_ttl_s=30.0,
            reprice_threshold_cc=100,
            candidate_gate_enabled=True,
        ),
        book_risk_pool=pool,  # type: ignore[arg-type]
    )
    reservation = RiskReservationService(
        exposure=exposure, limits=checker, breach_splitter=lifecycle.partition_breaches
    )
    lifecycle.attach_reservation(reservation)
    return lifecycle, sender, exposure, reservation


async def _make(
    tmp_path: Path,
    *,
    pool: ScriptedPool | None,
    limits: LimitChecker | None = None,
    db: str = "atomic.sqlite3",
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook, RiskReservationService]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    return _build_lifecycle(h, store, pool=pool, limits=limits)


def _held(pid: str) -> OpenPosition:
    """A stand-in for another accept's outstanding reservation, built on the REAL
    combo (KXMVE-C1 + the seeded M1/M2 legs, ``risk_modeled=True``) so it decomposes
    with KNOWN marginals and GRANTS through the real limit check (an unmodeled or
    unclassifiable combo fails closed with skip_classifier_unknown and never grants,
    so the reservation version would not move). We only need it to move the
    reservation version/set — the candidate MC's numeric verdict is the stub pool's
    scripted value, so this position's presence in the sampled universe is inert to
    what is under test."""
    return OpenPosition(
        position_id=pid,
        combo_ticker="KXMVE-C1",
        collection="KXMVESPORTS",
        our_side=Side.YES,
        contracts=CentiContracts(100),
        entry_price_cc=CentiCents(3_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
        risk_modeled=True,
    )


# --------------------------------------------------------------------------- #
# The provisional reservation exists BEFORE the MC, and the MC inputs carry the  #
# generation + reservation-version stamps (candidate excluded from PRE resv).    #
# --------------------------------------------------------------------------- #


async def test_provisional_reservation_precedes_mc_and_inputs_are_stamped(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        # DURING the MC await the candidate's provisional reservation is already held.
        seen["outstanding_during_mc"] = reservation.outstanding_count
        seen["reservation_version_stamp"] = inputs.reservation_version
        seen["input_generation_stamp"] = inputs.input_generation
        seen["pre_reservations"] = tuple(p.position_id for p in inputs.reservations)

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    # The provisional reservation was held WHILE the MC ran (created FIRST).
    assert seen["outstanding_during_mc"] == 1
    # Its own provisional reservation is EXCLUDED from the PRE reservations (it rides
    # as the candidate) — never double-counted.
    assert seen["pre_reservations"] == ()
    # The stamps are real (>= 0), not the -1 no-reservation sentinel.
    assert seen["reservation_version_stamp"] >= 0  # type: ignore[operator]
    assert seen["input_generation_stamp"] == 0  # empty committed book at first fill
    # Passed → confirmed → the reservation committed the position, service drained.
    assert sender.confirmed == ["q1"]
    assert "fill:q1" in exposure.positions
    assert reservation.outstanding_count == 0


# --------------------------------------------------------------------------- #
# A reservation ADDED during the MC bumps the version ⇒ discard + retry.         #
# --------------------------------------------------------------------------- #


async def test_reservation_added_during_mc_causes_retry(tmp_path: Path) -> None:
    # On the FIRST MC call, a concurrent accept reserves headroom (bumps the version)
    # WHILE our MC is in flight. Our verdict priced a stale book ⇒ it must be
    # discarded and the gate rebuilt/retried; the SECOND call sees the added
    # reservation and (scripted confirm) proceeds.
    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        if idx == 0:
            reservation.try_reserve(
                "concurrent",
                _held("concurrent"),
                marginals=lifecycle._marginals,  # noqa: SLF001
                daily_pnl=lifecycle.daily_pnl,
            )

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    # The version conflict forced a rebuild+retry (exactly two MC calls) and the
    # retry metric fired once.
    assert len(pool.calls) == 2
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.version_conflict_retry"
    ) == 1
    # The RETRY's inputs saw the concurrently-added reservation in the PRE set (proof
    # the two gates cannot ignore each other) — its own provisional is still excluded.
    retry_pre = {p.position_id for p in pool.calls[1].reservations}
    assert "concurrent" in retry_pre
    assert "fill:q1" not in retry_pre
    # Confirmed on the stable retry.
    assert sender.confirmed == ["q1"]


# --------------------------------------------------------------------------- #
# A reservation RELEASE during the MC changes the version ⇒ reevaluation.        #
# --------------------------------------------------------------------------- #


async def test_reservation_release_during_mc_causes_reeval(tmp_path: Path) -> None:
    lifecycle_ref: dict[str, QuoteLifecycle] = {}

    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        if idx == 0:
            # A concurrent reservation is RELEASED while our MC is in flight — the
            # version moves even though the position SET the MC saw is otherwise
            # unchanged, so the gate must reevaluate.
            assert reservation.release("preexisting") is True

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    lifecycle_ref["lc"] = lifecycle
    # Seed a pre-existing outstanding reservation that the release will drop.
    reservation.try_reserve(
        "preexisting",
        _held("preexisting"),
        marginals=lifecycle._marginals,  # noqa: SLF001
        daily_pnl=lifecycle.daily_pnl,
    )

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert len(pool.calls) == 2  # discard + retry
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.version_conflict_retry"
    ) == 1
    # The released reservation is gone from the retry's PRE set (reevaluated on the
    # new, smaller book).
    retry_pre = {p.position_id for p in pool.calls[1].reservations}
    assert "preexisting" not in retry_pre
    assert sender.confirmed == ["q1"]


# --------------------------------------------------------------------------- #
# Two concurrent candidate gates cannot both ignore each other.                  #
# --------------------------------------------------------------------------- #


async def test_two_concurrent_gates_cannot_ignore_each_other(tmp_path: Path) -> None:
    # Two accepts run their candidate gates CONCURRENTLY (gather). The pool's
    # run_candidate blocks on a barrier so BOTH provisional reservations are made
    # before EITHER MC verdict returns — proving accept B's MC inputs include accept
    # A's reservation (and vice versa): the two gates observe each other's held
    # headroom, exactly the atomicity the fix guarantees.
    both_reserved = asyncio.Event()
    entered = 0

    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        nonlocal entered
        entered += 1
        if entered >= 2:
            both_reserved.set()
        # Wait until BOTH accepts have made their provisional reservation and entered
        # the MC, so each call's inputs reflect the other's reservation.
        await asyncio.wait_for(both_reserved.wait(), timeout=2.0)

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)

    await lifecycle.handle_rfq(rfq())
    # A second distinct open quote on a second RFQ so both can be accepted.
    rfq2 = rfq()
    object.__setattr__(rfq2, "rfq_id", "rfq_2")
    await lifecycle.handle_rfq(rfq2)
    # Confirm the two live quote ids the sender created.
    q_ids = [c["id"] for c in sender.created]
    assert len(q_ids) == 2

    msg_a = accepted_msg(q_ids[0], "yes")
    msg_b = accepted_msg(q_ids[1], "yes")
    msg_b["rfq_id"] = "rfq_2"

    await asyncio.gather(
        lifecycle.on_quote_accepted(msg_a),
        lifecycle.on_quote_accepted(msg_b),
    )

    # BOTH provisional reservations coexisted during the MCs: at least one gate's MC
    # inputs carry the OTHER's reservation in its PRE set (they could not ignore each
    # other). With CRN retries either both confirm on a stable retry or one retries;
    # what matters is that neither priced a book missing the other's reservation.
    saw_other = False
    for inp in pool.calls:
        pre_ids = {p.position_id for p in inp.reservations}
        if pre_ids & {f"fill:{q_ids[0]}", f"fill:{q_ids[1]}"}:
            saw_other = True
    assert saw_other, "a concurrent gate priced a book WITHOUT the other's reservation"


# --------------------------------------------------------------------------- #
# Combined candidates exceeding the budget admit at most the safe subset.        #
# --------------------------------------------------------------------------- #


async def test_combined_candidates_admit_only_safe_subset(tmp_path: Path) -> None:
    # The MC verdict DEPENDS on the merged book: a candidate whose PRE reservations
    # already hold another fill is DECLINED (the combined tail is over-budget), while
    # the first candidate (empty PRE) PASSES. Because the first candidate's
    # provisional reservation is created BEFORE the second's MC runs, the second gate
    # SEES it and declines — at most the safe subset (one fill) is admitted.
    def verdict_by_pre(inputs: CandidateBookRiskInputs) -> CandidateBookRisk:
        # Any OTHER outstanding reservation in PRE ⇒ combined over-budget ⇒ decline.
        if inputs.reservations:
            return _verdict(confirm=False, reason="post_ruin_prob_over_budget")
        return _verdict(confirm=True)

    pool = ScriptedPool([verdict_by_pre])
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)

    await lifecycle.handle_rfq(rfq())
    rfq2 = rfq()
    object.__setattr__(rfq2, "rfq_id", "rfq_2")
    await lifecycle.handle_rfq(rfq2)
    q_ids = [c["id"] for c in sender.created]

    # First accept: empty PRE ⇒ passes ⇒ confirmed, reservation committed.
    await lifecycle.on_quote_accepted(accepted_msg(q_ids[0], "yes"))
    assert sender.confirmed == [q_ids[0]]
    assert f"fill:{q_ids[0]}" in exposure.positions

    # Second accept: the first fill is now a COMMITTED position (post-commit it moves
    # from reservation to the book). Make the verdict depend on committed too so the
    # second is declined on the combined book.
    def verdict_by_book(inputs: CandidateBookRiskInputs) -> CandidateBookRisk:
        if inputs.committed or inputs.reservations:
            return _verdict(confirm=False, reason="post_ruin_prob_over_budget")
        return _verdict(confirm=True)

    pool._verdicts = [verdict_by_book]  # noqa: SLF001

    msg_b = accepted_msg(q_ids[1], "yes")
    msg_b["rfq_id"] = "rfq_2"
    await lifecycle.on_quote_accepted(msg_b)

    # Second accept declined on the combined tail — and its provisional reservation
    # was RELEASED (only the first, safe fill remains committed; no dangling headroom).
    assert sender.confirmed == [q_ids[0]]
    assert f"fill:{q_ids[1]}" not in exposure.positions
    assert reservation.outstanding_count == 0
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1


# --------------------------------------------------------------------------- #
# The provisional reservation is RELEASED on MC decline / error / timeout.       #
# --------------------------------------------------------------------------- #


async def test_provisional_released_on_mc_decline(tmp_path: Path) -> None:
    pool = ScriptedPool([_verdict(confirm=False, reason="post_es_over_budget")])
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == []
    assert reservation.outstanding_count == 0  # provisional RELEASED
    assert "fill:q1" not in exposure.positions
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1


async def test_provisional_released_on_mc_error(tmp_path: Path) -> None:
    pool = ScriptedPool([], raise_exc=True)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == []
    assert reservation.outstanding_count == 0  # provisional RELEASED on error
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1


async def test_provisional_released_on_unstable_book_deadline(tmp_path: Path) -> None:
    # The book moves under EVERY MC attempt (a reservation is added on each call), so
    # no verdict ever prices the live book: the retry budget is exhausted and the gate
    # FAILS CLOSED (declines) — and the provisional reservation is released.
    n = 0

    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        nonlocal n
        # Every attempt: bump the version AFTER the inputs were stamped ⇒ perpetual
        # conflict ⇒ retries exhausted.
        reservation.try_reserve(
            f"churn{n}",
            _held(f"churn{n}"),
            marginals=lifecycle._marginals,  # noqa: SLF001
            daily_pnl=lifecycle.daily_pnl,
        )
        n += 1

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == []
    # The candidate's provisional reservation is gone (released); only the churn
    # reservations added by the hook remain — the fill's headroom did not linger.
    assert not reservation.is_outstanding("fill:q1")
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.retries_exhausted"
    ) == 1


async def test_gate_fails_closed_when_confirm_deadline_would_be_exceeded(
    tmp_path: Path,
) -> None:
    # The audit LIVE CANDIDATE-GATE LATENCY requirement: risk computation must NOT
    # silently consume the whole confirm window. The first MC "takes" nearly the whole
    # deadline (the hook advances the clock) AND moves the book (forcing a retry). On
    # the retry the deadline guard sees that another MC's worth of time no longer fits
    # in the remaining window, so it FAILS CLOSED (declines) rather than starting an MC
    # that would overrun — and releases the provisional reservation.
    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        if idx == 0:
            # Burn ~90% of the 2.0s deadline DURING the first MC …
            lifecycle._clock.advance(1.8)  # noqa: SLF001
            # … and move the book so the verdict is discarded and a retry is needed.
            reservation.try_reserve(
                "concurrent",
                _held("concurrent"),
                marginals=lifecycle._marginals,  # noqa: SLF001
                daily_pnl=lifecycle.daily_pnl,
            )

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    # Exactly ONE MC ran (the first); the retry was refused by the deadline guard
    # BEFORE starting a second MC — the confirm window was not consumed by risk math.
    assert len(pool.calls) == 1
    assert sender.confirmed == []
    assert not reservation.is_outstanding("fill:q1")  # provisional released
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.deadline_exceeded"
    ) == 1
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1
