"""FIX 2 (settled-leg det-max) + FIX 5 (stale-snapshot book-growth decay).

Both are pure HEADROOM RECOVERY: they remove a charge the risk engine was
levying for something that cannot happen (an outcome the exchange has already
determined) or for something it never measured (a book that grew by one
reservation). Neither moves a threshold, and both are armed behind a flag that
defaults to shadow.

WHAT THESE TESTS PIN, per the build brief:

FIX 2
  * a FULLY-resolved-won combo contributes 0 to det-max;
  * a PARTIALLY-resolved combo (the operator's stated common case: legs from
    yesterday's slate plus one or two live legs today) contributes only its
    residual — 0 when a settled leg already broke the parlay, FULL while the
    parlay is still alive;
  * an UNKNOWN settlement is charged in FULL, in both directions;
  * a settlement is NEVER inferred from a marginal — a leg whose FEED price is
    pinned at 0.0 or 1.0 but which the exchange has not determined stays charged;
  * unarmed / no facts ⇒ byte-identical numbers.

FIX 5
  * a generation-stale snapshot no longer discards;
  * its decayed use is provably CONSERVATIVE (every loss axis strictly above the
    measurement, by exactly the premium added since it was taken);
  * a genuinely over-limit book still REFUSES under decay;
  * the abstain band and the time guard both still fail closed.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import FiltersConfig, PricingConfig, RiskConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import (
    LifecycleConfig,
    QuoteLifecycle,
    _decay_book_risk,
    _DecayedBookRisk,
    _StaleBookRisk,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.sim.book_model import BookModel, build_book_model
from combomaker.sim.book_risk import (
    _det_units_from_model,
    _deterministic_all_hit_loss_cc,
    compute_book_risk,
    open_position_settled_cannot_lose,
    position_settled_cannot_lose,
    settled_det_max_credit_cc,
)
from combomaker.sim.engine import ComboPosition, LegModel
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, rfq
from tests.test_pricing_engine import seed_event
from tests.test_risk_shadow_mode import _FixedBankroll

# --------------------------------------------------------------------------- #
# FIX 2 — unit level: the resolution rule itself.
# --------------------------------------------------------------------------- #


def _model(
    positions: list[ComboPosition], settled: dict[int, float], n_legs: int = 4
) -> BookModel:
    eye = np.eye(n_legs)
    return BookModel(
        legs=tuple(LegModel(p=0.5) for _ in range(n_legs)),
        positions=tuple(positions),
        corr_location_point=eye,
        corr_tail_stress_point=eye.copy(),
        corr_tail_stress_low=eye.copy(),
        corr_tail_stress_high=eye.copy(),
        leg_index={f"T{i}": i for i in range(n_legs)},
        event_by_index={i: f"E{i}" for i in range(n_legs)},
        unknown=False,
        settled_leg_values=settled,
    )


def _no_combo(indices: tuple[int, ...], price_cc: int, sides: tuple[str, ...]):
    return ComboPosition(
        leg_indices=indices,
        side="no",
        contracts=10.0,
        price_cc=price_cc,
        leg_sides=sides,  # type: ignore[arg-type]
    )


def test_fully_resolved_won_combo_contributes_zero() -> None:
    """Every leg settled, and one of them broke the parlay ⇒ our NO is WON ⇒ the
    forward max-loss contribution is exactly 0."""
    pos = _no_combo((0, 1), 5_000, ("yes", "yes"))
    # leg0 graded NO (0.0) ⇒ the parlay MISSED ⇒ nothing left to lose.
    m = _model([pos], {0: 0.0, 1: 1.0})
    assert position_settled_cannot_lose(pos, m.settled_leg_values) is True
    assert _deterministic_all_hit_loss_cc(m, settlement_aware=True) == 0.0
    assert settled_det_max_credit_cc(m) == 5_000 * 10.0
    # And it leaves the mutex fold entirely (dropped, not zeroed).
    assert _det_units_from_model(m, settlement_aware=True) == []


def test_partially_resolved_combo_contributes_only_its_residual() -> None:
    """The operator's stated common case: some legs settled from yesterday's
    slate, one or two still live today.

    Leg 0 settled AGAINST the parlay ⇒ the combo is already won even though
    leg 3 has not played — the live leg is left completely unconstrained.
    The sibling combo whose settled leg went the parlay's WAY is still alive and
    keeps its FULL charge, because its remaining live leg can still hit.
    """
    won_on_settled_leg = _no_combo((0, 3), 3_000, ("yes", "yes"))
    still_alive = _no_combo((1, 2), 2_000, ("yes", "yes"))
    m = _model([won_on_settled_leg, still_alive], {0: 0.0, 1: 1.0})

    assert position_settled_cannot_lose(won_on_settled_leg, m.settled_leg_values)
    assert not position_settled_cannot_lose(still_alive, m.settled_leg_values)
    # Residual = ONLY the still-live combo's premium.
    assert _deterministic_all_hit_loss_cc(m, settlement_aware=True) == 2_000 * 10.0
    assert _deterministic_all_hit_loss_cc(m) == (2_000 + 3_000) * 10.0
    units = _det_units_from_model(m, settlement_aware=True)
    assert [u.entry_price_cc for u in units] == [2_000]


def test_unknown_settlement_is_charged_in_full() -> None:
    """No fact for a leg ⇒ that leg proves nothing ⇒ full charge. Applies with
    NO facts at all and with a partial fact set."""
    a = _no_combo((0, 1), 4_000, ("yes", "yes"))
    b = _no_combo((2, 3), 6_000, ("yes", "yes"))
    # No determinations at all.
    m_none = _model([a, b], {})
    assert _deterministic_all_hit_loss_cc(m_none, settlement_aware=True) == (
        _deterministic_all_hit_loss_cc(m_none)
    )
    assert settled_det_max_credit_cc(m_none) == 0.0
    # A fact that does NOT break either parlay changes nothing.
    m_alive = _model([a, b], {0: 1.0, 2: 1.0})
    assert _deterministic_all_hit_loss_cc(m_alive, settlement_aware=True) == (
        (4_000 + 6_000) * 10.0
    )
    assert settled_det_max_credit_cc(m_alive) == 0.0


def test_no_side_selection_is_respected_not_the_raw_value() -> None:
    """A NO-SELECTED leg contributes ``1 − value``. A leg graded 1.0 that the
    combo selected on its NO side BREAKS the parlay just as a 0.0 yes-leg does —
    resolution must read the SELECTED side, never the raw grade."""
    pos = _no_combo((0, 1), 7_000, ("no", "yes"))
    m = _model([pos], {0: 1.0})  # selected side = 1 − 1.0 = 0 ⇒ parlay missed
    assert position_settled_cannot_lose(pos, m.settled_leg_values) is True
    assert _deterministic_all_hit_loss_cc(m, settlement_aware=True) == 0.0
    # The mirror: graded 0.0 on a NO-selected leg keeps the parlay ALIVE.
    m2 = _model([pos], {0: 0.0})
    assert position_settled_cannot_lose(pos, m2.settled_leg_values) is False
    assert _deterministic_all_hit_loss_cc(m2, settlement_aware=True) == 7_000 * 10.0


def test_long_yes_position_needs_every_leg_proven() -> None:
    """A long YES loses when the parlay MISSES, so it is safe only when EVERY leg
    is proven to have gone the parlay's way. One unknown leg keeps it charged."""
    yes_pos = ComboPosition(
        leg_indices=(0, 1),
        side="yes",
        contracts=10.0,
        price_cc=5_000,
        leg_sides=("yes", "yes"),
    )
    assert position_settled_cannot_lose(yes_pos, {0: 1.0, 1: 1.0}) is True
    assert position_settled_cannot_lose(yes_pos, {0: 1.0}) is False  # unknown leg
    assert position_settled_cannot_lose(yes_pos, {0: 1.0, 1: 0.0}) is False


def test_settlement_is_never_inferred_from_a_pinned_marginal() -> None:
    """THE FAIL-CLOSED INVARIANT THAT MATTERS MOST.

    ``build_book_model`` must resolve det-max ONLY from the exchange-determination
    provider, never from the leg marginal — the live marginal provider walks the
    FEED first, so a market pinned at 0 or 100 presents p = 0.0/1.0 while being
    entirely unsettled. Here every marginal is a hard 0.0 and NO determination
    exists: nothing may be retired.
    """
    positions = [
        OpenPosition(
            position_id="p1",
            combo_ticker="C1",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(1_000),
            entry_price_cc=CentiCents(5_000),
            legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "yes")),
        )
    ]
    # Marginals pinned at 0.0 (a real, live, 0-bid market) and NO settled facts.
    pinned = build_book_model(positions, marginals=lambda t: 0.0, settled_facts=None)
    assert pinned.settled_leg_values == {}
    assert settled_det_max_credit_cc(pinned) == 0.0
    assert _deterministic_all_hit_loss_cc(pinned, settlement_aware=True) == (
        _deterministic_all_hit_loss_cc(pinned)
    )
    # Only an actual DETERMINATION retires it.
    determined = build_book_model(
        positions,
        marginals=lambda t: 0.0,
        settled_facts=lambda t: 0.0 if t == "M1" else None,
    )
    assert determined.settled_leg_values == {0: 0.0}
    assert _deterministic_all_hit_loss_cc(determined, settlement_aware=True) == 0.0


def test_open_position_shape_matches_the_model_shape() -> None:
    """The candidate gate resolves from leg TICKERS and the MC from latent
    indices; both must reach the identical verdict (one shared rule)."""
    pos = OpenPosition(
        position_id="p1",
        combo_ticker="C1",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(1_000),
        entry_price_cc=CentiCents(5_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "yes")),
    )
    facts = {"M1": 0.0}
    model = build_book_model(
        [pos], marginals=lambda t: 0.5, settled_facts=facts.get
    )
    assert open_position_settled_cannot_lose(pos, facts.get) is True
    assert position_settled_cannot_lose(
        model.positions[0], model.settled_leg_values
    ) is True
    # A non-binary "fact" is not a fact — treated as UNKNOWN by both.
    weird = {"M1": 0.5}
    assert open_position_settled_cannot_lose(pos, weird.get) is False
    weird_model = build_book_model(
        [pos], marginals=lambda t: 0.5, settled_facts=weird.get
    )
    assert weird_model.settled_leg_values == {}


def test_unarmed_compute_book_risk_is_byte_identical() -> None:
    """Shadow default: the credit is MEASURED but the enforced axes do not move."""
    pos = OpenPosition(
        position_id="p1",
        combo_ticker="C1",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(1_000),
        entry_price_cc=CentiCents(5_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "yes")),
    )
    model = build_book_model(
        [pos],
        marginals=lambda t: 0.5,
        settled_facts=lambda t: 0.0 if t == "M1" else None,
    )
    shadow = compute_book_risk(model, n_samples=2_000, seed=1)
    armed = compute_book_risk(
        model, n_samples=2_000, seed=1, det_max_settlement_aware=True
    )
    # The credit is visible in BOTH (shadow observability is the whole point).
    assert shadow.det_max_settled_credit_cc == pytest.approx(50_000.0)
    assert armed.det_max_settled_credit_cc == pytest.approx(50_000.0)
    # But only the armed run removes it from the enforced axes.
    assert shadow.deterministic_max_loss_cc == pytest.approx(50_000.0)
    assert armed.deterministic_max_loss_cc == pytest.approx(0.0)
    assert armed.mutex_aware_det_max_cc == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# FIX 5 — the decay is conservative, and a dangerous book still refuses.
# --------------------------------------------------------------------------- #


def _snapshot(premium_cc: int) -> object:
    """A real snapshot over a one-position book of the given premium."""
    pos = OpenPosition(
        position_id="held",
        combo_ticker="C1",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=CentiCents(premium_cc),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "yes")),
    )
    model = build_book_model([pos], marginals=lambda t: 0.5)
    # Equity + bankroll supplied so the snapshot carries a real ruin threshold
    # (that is what the decay shifts); without them the ruin cap does not
    # evaluate at all and there is nothing to charge.
    return compute_book_risk(
        model,
        n_samples=4_000,
        seed=3,
        bankroll_cc=10_000_000,
        current_equity_cc=10_000_000,
        ruin_floor_frac=0.70,
    )


def test_decay_is_strictly_more_conservative_than_the_measurement() -> None:
    """Every LOSS axis rises by exactly the premium added since the snapshot, and
    the loss-quantile envelope shifts with it. Nothing may come out lower."""
    snap = _snapshot(5_000)
    added = 250_000
    decayed = _decay_book_risk(snap, added)  # type: ignore[arg-type]
    assert isinstance(decayed, _DecayedBookRisk)
    assert decayed.usable is True
    assert decayed.deterministic_max_loss_cc == pytest.approx(
        snap.deterministic_max_loss_cc + added  # type: ignore[attr-defined]
    )
    assert decayed.governing_model_es_99_cc == pytest.approx(
        snap.governing_model_es_99_cc + added  # type: ignore[attr-defined]
    )
    assert decayed.mutex_aware_det_max_cc == pytest.approx(
        snap.mutex_aware_det_max_cc + added  # type: ignore[attr-defined]
    )
    # Every quantile shifted up by exactly the charge ⇒ P(loss >= t) can only rise.
    assert decayed.loss_quantiles_cc == tuple(
        q + added for q in snap.loss_quantiles_cc  # type: ignore[attr-defined]
    )
    # Ruin can only rise, never fall.
    assert decayed.p_ruin >= snap.p_ruin  # type: ignore[attr-defined]
    assert decayed.p_ruin_upper >= snap.p_ruin_upper  # type: ignore[attr-defined]
    # A CREDIT is never carried across staleness.
    assert decayed.det_max_hedge_credit_cc == 0.0


def test_zero_growth_decay_equals_the_measurement_on_every_loss_axis() -> None:
    """A generation bump that added NO premium (e.g. a position was removed) is
    charged nothing — the measurement is already an upper bound."""
    snap = _snapshot(5_000)
    decayed = _decay_book_risk(snap, 0)  # type: ignore[arg-type]
    assert decayed is not None
    assert decayed.deterministic_max_loss_cc == pytest.approx(
        snap.deterministic_max_loss_cc  # type: ignore[attr-defined]
    )
    assert decayed.governing_model_es_99_cc == pytest.approx(
        snap.governing_model_es_99_cc  # type: ignore[attr-defined]
    )


def test_decayed_ruin_rises_when_growth_pushes_loss_past_the_floor() -> None:
    """The ruin axis is charged by re-reading the SHIFTED loss envelope at the
    snapshot's own ruin threshold — not by guessing where that threshold is.

    A book whose sampled losses all sit below the ruin threshold reports
    p_ruin = 0; once the growth charge pushes the envelope past the threshold the
    charged probability rises. This is the axis that made a naive inversion
    unusable (it manufactured 34.7% ruin on a safe book), so it is pinned here.
    """
    snap = _snapshot(5_000)
    assert snap.p_ruin == 0.0  # type: ignore[attr-defined]
    threshold = snap.ruin_loss_threshold_cc  # type: ignore[attr-defined]
    assert threshold is not None

    # A charge far short of the threshold must NOT invent ruin risk.
    small = _decay_book_risk(snap, 1_000)  # type: ignore[arg-type]
    assert small is not None
    assert small.p_ruin == 0.0

    # A charge that carries the whole envelope past the threshold must.
    huge = _decay_book_risk(snap, int(threshold) + 10_000)  # type: ignore[arg-type]
    assert huge is not None
    assert huge.p_ruin == pytest.approx(1.0)
    assert huge.p_ruin_upper == pytest.approx(1.0)


def test_tail_axes_take_the_larger_unsampled_charge() -> None:
    """REGRESSION for a gap the live tape exposed.

    A position the snapshot held only as a flat RESERVE (unpriceable leg) sits
    OUTSIDE the sampled model ES but INSIDE the deterministic reserve. When its
    leg book comes online it becomes sampled and the ES rises with NO change in
    total premium — measured 2026-07-28 08:46:45 → 08:47:00: governing ES
    +$16.44 with det-max flat at $777.66. So the SAMPLED axes must be charged for
    every position the snapshot did not SAMPLE, while the DETERMINISTIC axes are
    charged only for positions it did not HOLD.
    """
    snap = _snapshot(5_000)
    added, unsampled = 10_000, 90_000
    d = _decay_book_risk(snap, added, unsampled)  # type: ignore[arg-type]
    assert d is not None
    # Deterministic axes: the exact absent-position charge.
    assert d.deterministic_max_loss_cc == pytest.approx(
        snap.deterministic_max_loss_cc + added  # type: ignore[attr-defined]
    )
    # Sampled tail axes + the envelope: the larger unsampled charge.
    assert d.governing_model_es_99_cc == pytest.approx(
        snap.governing_model_es_99_cc + unsampled  # type: ignore[attr-defined]
    )
    assert d.loss_quantiles_cc[0] == pytest.approx(
        snap.loss_quantiles_cc[0] + unsampled  # type: ignore[attr-defined]
    )
    assert d.unsampled_premium_cc == unsampled
    # And the tail charge is NEVER allowed below the deterministic one.
    d2 = _decay_book_risk(snap, 90_000, 10_000)  # type: ignore[arg-type]
    assert d2 is not None
    assert d2.governing_model_es_99_cc == pytest.approx(
        snap.governing_model_es_99_cc + 90_000  # type: ignore[attr-defined]
    )


def test_decay_refuses_an_unusable_or_envelopeless_snapshot() -> None:
    """Fail-closed refusals: an UNKNOWN book is not made knowable by charging it
    more, and a snapshot with no loss-quantile envelope has no sound ruin charge
    available — both keep today's discard."""

    class _Unusable:
        usable = False
        loss_quantiles_cc = (1.0, 2.0)

    class _NoEnvelope:
        usable = True
        loss_quantiles_cc: tuple[float, ...] = ()

    assert _decay_book_risk(_Unusable(), 10) is None  # type: ignore[arg-type]
    assert _decay_book_risk(_NoEnvelope(), 10) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# FIX 5 — through the real lifecycle hot path.
# --------------------------------------------------------------------------- #


def _build(
    h: Harness,
    store: Store,
    *,
    bankroll_cc: int,
    cvar_frac: str,
    stale_decay: bool,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook]:
    from fractions import Fraction as F

    sender = FakeSender()
    exposure = ExposureBook(TEST_CONVENTIONS)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed,
        h.metadata,
        h.killswitch,
        h.clock,
    )
    limits = LimitChecker(
        RiskLimits(
            caps_shadow_mode=False,
            game_loss_frac=F(99, 100),
            per_combo_loss_frac=F(99, 100),
            directional_frac=F(99, 100),
            slate_loss_frac=F(99, 100),
            daily_loss_frac=F(99, 100),
            drawdown_frac=F(99, 100),
            hard_trip_frac=F(99, 100),
            absolute_notional_multiple=999,
            portfolio_cvar_frac=F(int(float(cvar_frac) * 100), 100),
            portfolio_det_max_frac=F(99, 100),
        )
    )
    lifecycle = QuoteLifecycle(
        clock=h.clock,
        sender=sender,
        engine=engine,
        rfq_filter=rfq_filter,
        limits=limits,
        exposure=exposure,
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(book_risk_stale_decay=stale_decay),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
    )
    return lifecycle, sender, exposure


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _add_position(
    exposure: ExposureBook, pid: str, *, contracts: int, price_cc: int
) -> None:
    exposure.add_position(
        OpenPosition(
            position_id=pid,
            combo_ticker=f"COMBO-{pid}",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(contracts),
            entry_price_cc=CentiCents(price_cc),
            legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "yes")),
        )
    )


async def test_stale_snapshot_no_longer_discards_when_armed(
    harness: tuple[Harness, Store],
) -> None:
    """THE MEASURED DEFECT: one extra position bumps the generation and the whole
    tail axis went dark. Armed, the snapshot is charged and the quote survives."""
    h, store = harness
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", stale_decay=True
    )
    _add_position(exposure, "held", contracts=100, price_cc=5_000)
    lifecycle.recompute_book_risk()
    # A second position supersedes the generation — exactly the live signature
    # (gap of 1, from a single accept/reservation).
    _add_position(exposure, "second", contracts=100, price_cc=5_000)
    assert lifecycle._book_risk.input_generation != exposure.position_generation

    view = lifecycle._book_risk_for_check()
    assert isinstance(view, _DecayedBookRisk)
    assert view.usable is True
    # Charged for exactly the premium the snapshot never saw.
    assert view.added_premium_cc == 5_000 * 100 // 100
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1


async def test_stale_snapshot_still_discards_when_unarmed(
    harness: tuple[Harness, Store],
) -> None:
    """Shadow default is byte-identical: the decay is measured, never used."""
    h, store = harness
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", stale_decay=False
    )
    _add_position(exposure, "held", contracts=100, price_cc=5_000)
    lifecycle.recompute_book_risk()
    _add_position(exposure, "second", contracts=100, price_cc=5_000)
    assert isinstance(lifecycle._book_risk_for_check(), _StaleBookRisk)
    # ...but the shadow number IS available for the operator readout.
    assert lifecycle._book_risk_decayed() is not None


async def test_over_limit_book_still_refuses_under_decay(
    harness: tuple[Harness, Store],
) -> None:
    """THE SAFETY PROOF. A book whose tail genuinely breaches the CVaR ceiling
    must still be refused when the snapshot is generation-stale and decayed —
    the decay recovers throughput, never posture."""
    h, store = harness
    # Tiny bankroll ⇒ the held book's ES is far over the 15% ceiling.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000, cvar_frac="0.15", stale_decay=True
    )
    _add_position(exposure, "held", contracts=10_000, price_cc=9_000)
    lifecycle.recompute_book_risk()
    _add_position(exposure, "second", contracts=10_000, price_cc=9_000)
    view = lifecycle._book_risk_for_check()
    assert isinstance(view, _DecayedBookRisk)  # decayed, not discarded
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []  # and still REFUSED


async def test_decay_charge_is_monotone_in_book_growth(
    harness: tuple[Harness, Store],
) -> None:
    """Each added position raises the charged tail — the charge tracks measured
    growth rather than a fixed tolerance."""
    h, store = harness
    lifecycle, _sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", stale_decay=True
    )
    _add_position(exposure, "held", contracts=100, price_cc=5_000)
    lifecycle.recompute_book_risk()
    base = lifecycle._book_risk.deterministic_max_loss_cc

    seen: list[float] = []
    for i in range(3):
        _add_position(exposure, f"grow{i}", contracts=100, price_cc=1_000)
        view = lifecycle._book_risk_for_check()
        assert isinstance(view, _DecayedBookRisk)
        seen.append(view.deterministic_max_loss_cc)
    assert seen == sorted(seen)
    assert all(v > base for v in seen)


async def test_abstain_band_fails_closed_when_the_book_more_than_doubles(
    harness: tuple[Harness, Store],
) -> None:
    """The decay is a BETWEEN-SNAPSHOTS BRIDGE. Once the added premium exceeds
    the premium the snapshot was built on, the measurement no longer describes
    the book and the cap goes back to failing closed."""
    h, store = harness
    lifecycle, _sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", stale_decay=True
    )
    _add_position(exposure, "held", contracts=100, price_cc=1_000)
    lifecycle.recompute_book_risk()
    # Add far more premium than the snapshot ever measured.
    _add_position(exposure, "huge", contracts=10_000, price_cc=9_000)
    assert lifecycle._book_risk_decayed() is None
    assert isinstance(lifecycle._book_risk_for_check(), _StaleBookRisk)


async def test_time_staleness_still_fails_closed_under_decay(
    harness: tuple[Harness, Store],
) -> None:
    """The TIME guard is NOT decayed: an ancient measurement of a changed book is
    refused even when the growth charge itself would have been applicable."""
    h, store = harness
    lifecycle, _sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", stale_decay=True
    )
    _add_position(exposure, "held", contracts=100, price_cc=5_000)
    lifecycle.recompute_book_risk()
    _add_position(exposure, "second", contracts=100, price_cc=5_000)
    # Age the snapshot past the freshness window.
    h.clock.advance(
        int((lifecycle._config.book_risk_stale_after_s + 1) * 1e9)
    )
    assert isinstance(lifecycle._book_risk_for_check(), _StaleBookRisk)


async def test_fix2_reaches_the_real_det_max_cap_through_the_lifecycle(
    harness: tuple[Harness, Store],
) -> None:
    """END-TO-END. A held combo whose leg the EXCHANGE has graded against the
    parlay must stop consuming det-max headroom on the real quote path — armed —
    and must keep consuming it unarmed. This is the wiring proof: the resolver →
    ``_settled_fact`` → ``build_book_model`` → ``compute_book_risk`` chain, not
    the arithmetic (covered above).
    """
    h, store = harness

    class _Resolver:
        """Stands in for SettledMarginalResolver: M1 graded NO by the exchange."""

        def resolved(self, ticker: str) -> float | None:
            return 0.0 if ticker == "M1" else None

    for armed in (False, True):
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15", stale_decay=False
        )
        lifecycle._settled = _Resolver()  # type: ignore[assignment]
        lifecycle._limits.set_limits(
            RiskLimits(
                **{
                    **{
                        f.name: getattr(lifecycle._limits.limits, f.name)
                        for f in __import__("dataclasses").fields(RiskLimits)
                    },
                    "det_max_settlement_aware": armed,
                }
            )
        )
        _add_position(exposure, "held", contracts=100, price_cc=5_000)
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk
        assert snap is not None
        # The credit is MEASURED in both modes (shadow observability)...
        assert snap.det_max_settled_credit_cc == pytest.approx(5_000.0)
        # ...and only REMOVED from the enforced axes when armed.
        if armed:
            assert snap.deterministic_max_loss_cc == pytest.approx(0.0)
            assert snap.mutex_aware_det_max_cc == pytest.approx(0.0)
        else:
            assert snap.deterministic_max_loss_cc == pytest.approx(5_000.0)


def test_both_arming_flags_default_to_shadow() -> None:
    """Neither fix changes a live decision until the operator arms it."""
    cfg = RiskConfig()
    assert cfg.det_max_settlement_aware is False
    assert cfg.book_risk_stale_decay is False
    assert cfg.to_risk_limits().det_max_settlement_aware is False
    assert LifecycleConfig().book_risk_stale_decay is False
