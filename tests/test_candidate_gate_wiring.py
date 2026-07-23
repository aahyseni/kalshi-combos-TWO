"""P0-1: the candidate-aware portfolio-risk gate WIRED into the live confirm path.

``evaluate_candidate_book_risk`` is exhaustively unit-tested in
``test_candidate_book_risk.py`` (its concentrating/balancing/hedge/unknown/CRN
semantics). THIS file proves the LIFECYCLE wiring around it (the part that was
CALLED NOWHERE before): after the existing analytic/gross/burst gates ADMIT a
confirm (``decide_confirm().confirm == True``), the candidate gate runs as an
ADDITIONAL check that can only flip ADMIT→DECLINE, never DECLINE→ADMIT, and:

  * a CONCENTRATING candidate (gate ``confirm=False``) ⇒ DECLINE_CANDIDATE_RISK,
    NO confirm sent, state cleaned up (executed_states popped, pending_fill
    cleared, quote dropped);
  * a BALANCING candidate (gate ``confirm=True``) ⇒ proceeds to the confirm flow
    unchanged;
  * an UNKNOWN merged marginal ⇒ DECLINE (fail-closed), via the REAL inline eval;
  * ``candidate_gate_enabled=False`` ⇒ gate skipped, prior behaviour;
  * ADDITIVITY: when ``decide_confirm`` already DECLINES, the candidate gate is
    never consulted (never flips a decline to a confirm);
  * an off-loop EXCEPTION ⇒ DECLINE (fail-closed, never confirm on an errored
    gate);
  * the gate runs OFF-LOOP: the confirm awaits ``BookRiskPool.run_candidate``, not
    an inline compute (asserted via the stub pool's call count).

The stub pool returns a chosen ``CandidateBookRisk`` so the wiring is tested
independently of the MC's numeric verdict (which its own unit tests own).
"""

from __future__ import annotations

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


def _verdict(
    *, confirm: bool, unknown: bool = False, reason: str = ""
) -> CandidateBookRisk:
    """A minimal CandidateBookRisk with the chosen gate outcome (the numbers are
    the eval's own unit-test concern; here only ``confirm``/``unknown`` matter)."""
    return CandidateBookRisk(
        unknown=unknown,
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


class StubBookRiskPool:
    """A stub ``BookRiskPool`` recording every off-loop candidate eval and returning
    a scripted verdict (or raising, for the errored-gate path). Only
    ``run_candidate`` is exercised by the gate — mirrors the FakeSender pattern."""

    def __init__(
        self, verdict: CandidateBookRisk | None = None, *, raise_exc: bool = False
    ) -> None:
        self.verdict = verdict
        self.raise_exc = raise_exc
        self.calls: list[CandidateBookRiskInputs] = []

    async def run_candidate(
        self, inputs: CandidateBookRiskInputs
    ) -> CandidateBookRisk:
        self.calls.append(inputs)
        if self.raise_exc:
            raise RuntimeError("candidate pool boom")
        assert self.verdict is not None
        return self.verdict


class GateRig:
    def __init__(
        self,
        h: Harness,
        store: Store,
        *,
        pool: StubBookRiskPool | None = None,
        gate_enabled: bool = True,
        limits: RiskLimits | None = None,
    ) -> None:
        self.h = h
        self.sender = FakeSender()
        self.killswitch = h.killswitch
        self.exposure = ExposureBook(TEST_CONVENTIONS)
        self.metrics = Metrics()
        self.pool = pool
        engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=engine,
            rfq_filter=RfqFilter(
                FiltersConfig(min_time_to_close_s=0.0).model_copy(
                    update={"allowed_leg_series_prefixes": None}
                ),
                h.feed, h.metadata, h.killswitch, h.clock,
            ),
            limits=LimitChecker(limits if limits is not None else RiskLimits()),
            exposure=self.exposure,
            feed=h.feed,
            metadata=h.metadata,
            inplay=InPlayDetector(h.clock),
            killswitch=h.killswitch,
            conventions=TEST_CONVENTIONS,
            store=store,
            metrics=self.metrics,
            lastlook_policy=LastLookPolicy(),
            config=LifecycleConfig(
                quote_ttl_s=30.0,
                reprice_threshold_cc=100,
                candidate_gate_enabled=gate_enabled,
            ),
            book_risk_pool=pool,  # type: ignore[arg-type]
        )


async def _make_rig(
    tmp_path: Path,
    *,
    pool: StubBookRiskPool | None = None,
    gate_enabled: bool = True,
    db: str = "gate.sqlite3",
    limits: RiskLimits | None = None,
) -> GateRig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    return GateRig(h, store, pool=pool, gate_enabled=gate_enabled, limits=limits)


async def _accept(rig: GateRig, side: str = "yes") -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", side))


# --------------------------------------------------------------------------- #
# CONCENTRATING candidate at confirm ⇒ DECLINE, no confirm, state cleaned up.  #
# --------------------------------------------------------------------------- #


async def test_concentrating_candidate_declines_and_cleans_up(tmp_path: Path) -> None:
    pool = StubBookRiskPool(
        _verdict(confirm=False, reason="post_ruin_prob_over_budget")
    )
    rig = await _make_rig(tmp_path, pool=pool)
    await _accept(rig)

    # The existing gates ADMITTED (decide_confirm confirmed), so the candidate gate
    # was consulted — and it DECLINED, so NO confirm was sent.
    assert pool.calls, "candidate gate must be consulted after decide_confirm admits"
    assert rig.sender.confirmed == []
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 1
    )
    # State fully cleaned up: no executed_state parked, no pending fill, quote gone,
    # nothing booked (we never confirmed).
    assert rig.lifecycle._executed_states == {}  # noqa: SLF001
    assert rig.lifecycle.open_quote_count == 0
    assert len(rig.exposure.positions) == 0


async def test_unknown_verdict_declines(tmp_path: Path) -> None:
    # A gate result flagged UNKNOWN (a missing merged marginal) declines even though
    # its ``confirm`` field is False by construction — fail-closed on UNKNOWN.
    pool = StubBookRiskPool(
        _verdict(confirm=False, unknown=True, reason="unknown_marginal")
    )
    rig = await _make_rig(tmp_path, pool=pool)
    await _accept(rig)
    assert rig.sender.confirmed == []
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 1
    )


# --------------------------------------------------------------------------- #
# BALANCING candidate ⇒ gate passes ⇒ proceeds to the confirm flow unchanged.  #
# --------------------------------------------------------------------------- #


async def test_balancing_candidate_passes_gate_and_confirms(tmp_path: Path) -> None:
    pool = StubBookRiskPool(_verdict(confirm=True))
    rig = await _make_rig(tmp_path, pool=pool)
    await _accept(rig)

    assert pool.calls  # the gate ran
    # It passed, so the confirm flow proceeded unchanged: confirm sent, and on
    # execution the position books.
    assert rig.sender.confirmed == ["q1"]
    await rig.lifecycle.on_quote_executed({"quote_id": "q1", "order_id": "o1"})
    assert len(rig.exposure.positions) == 1
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 0
    )


# --------------------------------------------------------------------------- #
# OFF-LOOP: the gate awaits the pool, not an inline compute.                    #
# --------------------------------------------------------------------------- #


async def test_gate_runs_off_loop_via_pool(tmp_path: Path) -> None:
    pool = StubBookRiskPool(_verdict(confirm=True))
    rig = await _make_rig(tmp_path, pool=pool)
    await _accept(rig)
    # Exactly one off-loop candidate eval was shipped to the pool for this confirm
    # (proving the OFF-LOOP path, not an inline compute), carrying the picklable
    # candidate + committed inputs.
    assert len(pool.calls) == 1
    shipped = pool.calls[0]
    assert isinstance(shipped, CandidateBookRiskInputs)
    assert shipped.candidate.position_id == "fill:q1"
    assert shipped.committed == ()  # empty book at the first confirm


async def test_off_loop_exception_declines_fail_closed(tmp_path: Path) -> None:
    # Any exception in the off-loop eval DECLINES (never confirm on an errored gate).
    pool = StubBookRiskPool(raise_exc=True)
    rig = await _make_rig(tmp_path, pool=pool)
    await _accept(rig)
    assert pool.calls  # the gate was consulted (and raised)
    assert rig.sender.confirmed == []
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 1
    )
    assert rig.lifecycle._executed_states == {}  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Kill switch: candidate_gate_enabled=False ⇒ gate skipped, prior behaviour.   #
# --------------------------------------------------------------------------- #


async def test_gate_disabled_skips_gate_entirely(tmp_path: Path) -> None:
    # A pool scripted to DECLINE — but the gate is DISABLED, so it is never consulted
    # and the confirm proceeds exactly as before (prior behaviour preserved).
    pool = StubBookRiskPool(_verdict(confirm=False, reason="would_have_declined"))
    rig = await _make_rig(tmp_path, pool=pool, gate_enabled=False)
    await _accept(rig)
    assert pool.calls == []  # gate skipped entirely
    assert rig.sender.confirmed == ["q1"]
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 0
    )


# --------------------------------------------------------------------------- #
# ADDITIVITY: an existing decline is never flipped to a confirm by the gate.    #
# --------------------------------------------------------------------------- #


async def test_existing_decline_never_consults_candidate_gate(tmp_path: Path) -> None:
    # decide_confirm DECLINES here (killswitch halted between accept and confirm),
    # so the candidate gate — which lives INSIDE `if decision.confirm` — is never
    # consulted. The gate can only ADD declines, never turn a decline into a
    # confirm: even a pool scripted to CONFIRM cannot rescue this fill.
    pool = StubBookRiskPool(_verdict(confirm=True))
    rig = await _make_rig(tmp_path, pool=pool)
    await rig.lifecycle.handle_rfq(rfq())
    await rig.killswitch.halt(ReasonCode.HALT_MANUAL)
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1"))
    assert pool.calls == []  # the gate was NEVER consulted on an existing decline
    assert rig.sender.confirmed == []
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_KILL_SWITCH}") == 1
    )


# --------------------------------------------------------------------------- #
# Fail-closed UNKNOWN marginal through the REAL inline eval (no pool).          #
# --------------------------------------------------------------------------- #


async def test_held_unpriceable_position_reserves_candidate_confirms(
    tmp_path: Path,
) -> None:
    # 2026-07-23 fix, real inline eval: a risk-modeled COMMITTED position on ticker
    # "GHOST" (no feed book ⇒ no marginal) no longer poisons the MERGED model to
    # UNKNOWN. It is RESERVED at its max loss (bounded, conservative), so the
    # priceable candidate (M1/M2 with books, +EV) is evaluated on its own merits and
    # CONFIRMS — one held combo with an unpriceable/in-play leg never darks the
    # confirm path (the CANDIDATE would still decline if ITS own leg were unpriceable;
    # that fail-closed is unit-tested in test_candidate_book_risk).
    rig = await _make_rig(tmp_path, pool=None)
    ghost = OpenPosition(
        position_id="held:ghost",
        combo_ticker="COMBO-GHOST",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=CentiCents(5_000),
        legs=(LegRef("GHOST", "E9", "yes"),),
        risk_modeled=True,  # unpriceable ⇒ RESERVED, not UNKNOWN
    )
    rig.exposure.add_position(ghost)
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == ["q1"]                # candidate evaluated + confirmed
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 0
    )


async def test_positive_ev_real_inline_eval_confirms(tmp_path: Path) -> None:
    # The mirror of the above with intact books: the REAL inline eval on an empty
    # committed book + a +EV single-fill candidate (no bankroll ⇒ only the EV gate
    # binds) CONFIRMS — proving the inline path is not a blanket decline.
    rig = await _make_rig(tmp_path, pool=None)
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == ["q1"]
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        == 0
    )


# --------------------------------------------------------------------------- #
# MUTEX-AWARE DET-MAX rollback knob: config -> inputs -> worker plumbing.      #
# The quote-time cap reads RiskLimits directly; the confirm gate runs in a     #
# worker and can only honor the knob if the lifecycle THREADS it through       #
# CandidateBookRiskInputs and the worker FORWARDS it (verify finding           #
# 2026-07-18: before the thread-through, knob=False was silently ignored on    #
# the one path that admits fills).                                             #
# --------------------------------------------------------------------------- #


async def test_lifecycle_threads_det_max_mutex_knob_into_gate_inputs(
    tmp_path: Path,
) -> None:
    for knob in (True, False):
        pool = StubBookRiskPool(_verdict(confirm=True))
        rig = await _make_rig(
            tmp_path,
            pool=pool,
            db=f"knob_{knob}.sqlite3",
            limits=RiskLimits(portfolio_det_max_mutex_aware=knob),
        )
        await _accept(rig)
        assert len(pool.calls) == 1
        assert pool.calls[0].det_max_mutex_aware is knob


async def test_worker_forwards_det_max_mutex_knob_to_eval(
    monkeypatch: object, tmp_path: Path
) -> None:
    # Pin the LAST hop: _worker_candidate_book_risk must forward
    # inputs.det_max_mutex_aware into evaluate_candidate_book_risk. Recorded via
    # a stub eval so the pin survives any future eval-signature growth.
    import pytest

    import combomaker.ops.pricing_pool as pool_mod

    pool = StubBookRiskPool(_verdict(confirm=True))
    rig = await _make_rig(
        tmp_path,
        pool=pool,
        db="knob_fwd.sqlite3",
        limits=RiskLimits(portfolio_det_max_mutex_aware=False),
    )
    await _accept(rig)
    inputs = pool.calls[0]
    assert inputs.det_max_mutex_aware is False

    seen: dict[str, object] = {}

    def _recording_eval(*args: object, **kwargs: object) -> CandidateBookRisk:
        seen.update(kwargs)
        return _verdict(confirm=True)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(pool_mod, "evaluate_candidate_book_risk", _recording_eval)
        pool_mod._worker_candidate_book_risk(inputs)
    finally:
        mp.undo()
    assert seen["det_max_mutex_aware"] is False
