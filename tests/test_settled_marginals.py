"""SETTLED-LEG MARGINAL RESOLUTION (live outage 2026-07-18 evening).

The outage shape this pins: FRAENG finished and its leg markets settled — their
order books left the feed — while the book still held cross-game combos with
FRAENG legs (open until the ESPARG legs resolve). The book-risk model demanded
a marginal for every risk-modeled leg ⇒ ``BookModel.unknown`` ⇒ every snapshot
unusable ⇒ the portfolio-CVaR cap failed closed on EVERY quote
(``book_risk_unusable``) until the last leg would have settled.

The fix: a settled leg's marginal is an exchange-GRADED FACT (GET
/markets/{ticker} ``result``: yes ⇒ 1.0 / no ⇒ 0.0, accepted only under status
``determined``/``finalized`` — live openapi.yaml, verified 2026-07-18), fetched
off the hot path and cached permanently; the lifecycle's marginal provider goes
feed first, settled-cache second, else UNKNOWN (unchanged fail-closed).

Covered here:
1. won settled leg ⇒ snapshot usable, conditional risk = full exposure on the
   remaining legs;
2. lost settled leg ⇒ the combo contributes zero further risk;
3. unresolved-but-closed market ⇒ stays UNKNOWN / unusable (fail-closed);
4. feed-alive leg unaffected (feed takes precedence over the cache);
5. cache permanence: one fetch, reused forever; fetch failure retried, never
   crashes the tick;
6. knob False ⇒ no resolver wired ⇒ the pre-fix behaviour;
7. end-to-end outage shape: 9-position book, settled FRAENG legs + live legs ⇒
   the CVaR gate passes a quote again through the public path.
Plus: degenerate (0/1) marginals never enter the structural inversion, and the
structural challenger never shocks a settled fact.
"""

from __future__ import annotations

from fractions import Fraction as F
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.settled import SettledMarginalResolver
from combomaker.ops.config import FiltersConfig, PricingConfig, RiskConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import build_settled_resolver
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.breakers import (
    BreakerInputs,
    BreakerThresholds,
    CircuitBreakers,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import _shock_marginals
from combomaker.sim.structural_book import StructuralConfigView, build_game_plans
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, rfq
from tests.test_pricing_engine import seed_event
from tests.test_risk_shadow_mode import _FixedBankroll

JsonDict = dict[str, Any]

# The settled game's leg markets (books GONE from the feed) and the live legs
# (Harness books M1/M2). Mirrors the live shape "yes:FRAENG-FRA + no:FRAENG-5
# + no:ESPARG-…".
FRA_WIN = "KXWCGAME-26JUL18FRAENG-FRA"
FRAENG_O5 = "KXWCTOTAL-26JUL18FRAENG-5"
FRAENG_EVENT = "KXWCGAME-26JUL18FRAENG"


def market_payload(
    ticker: str,
    *,
    status: str,
    result: str,
    settlement_value_dollars: str | None = None,
) -> JsonDict:
    market: JsonDict = {"ticker": ticker, "status": status, "result": result}
    if settlement_value_dollars is not None:
        market["settlement_value_dollars"] = settlement_value_dollars
    return {"market": market}


class FakeMarketSource:
    """Programmable GET /markets/{ticker}: payload, or an exception to raise."""

    def __init__(self) -> None:
        self.payloads: dict[str, JsonDict | Exception] = {}
        self.calls: list[str] = []

    async def get_market(self, ticker: str) -> JsonDict:
        self.calls.append(ticker)
        entry = self.payloads.get(ticker)
        if entry is None:
            raise KeyError(f"no payload for {ticker}")
        if isinstance(entry, Exception):
            raise entry
        return entry


def _resolver(
    source: FakeMarketSource, clock: FakeClock, **kwargs: float | int
) -> SettledMarginalResolver:
    return SettledMarginalResolver(source, clock, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Resolver unit behaviour: cache permanence, retries, fail-closed statuses.    #
# --------------------------------------------------------------------------- #


class TestResolver:
    async def test_graded_results_resolve_and_cache_permanently(self) -> None:
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="determined", result="yes"
        )
        source.payloads[FRAENG_O5] = market_payload(
            FRAENG_O5, status="finalized", result="no"
        )
        r = _resolver(source, clock)
        r.note_missing(FRA_WIN)
        r.note_missing(FRAENG_O5)
        assert r.has_due_pending
        assert await r.resolve_pending() == 2
        assert r.resolved(FRA_WIN) == 1.0
        assert r.resolved(FRAENG_O5) == 0.0
        assert len(source.calls) == 2
        # Permanent: re-notes and later passes never refetch (a settlement
        # never changes) — reads reuse the cache across generations.
        r.note_missing(FRA_WIN)
        clock.advance(3600.0)
        assert not r.has_due_pending
        assert await r.resolve_pending() == 0
        for _ in range(100):
            assert r.resolved(FRA_WIN) == 1.0
        assert len(source.calls) == 2

    async def test_closed_unresolved_market_stays_unknown_and_retries(self) -> None:
        # Game over, Kalshi has not graded yet: status=closed, result="".
        # NEVER resolves from anything but the exchange result — retried on the
        # backoff until the exchange grades it.
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads[FRA_WIN] = market_payload(FRA_WIN, status="closed", result="")
        r = _resolver(source, clock, retry_after_s=30.0)
        r.note_missing(FRA_WIN)
        assert await r.resolve_pending() == 0
        assert r.resolved(FRA_WIN) is None
        assert not r.has_due_pending  # backoff armed
        clock.advance(31.0)
        assert r.has_due_pending
        # The exchange grades it ⇒ the retry resolves it.
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="determined", result="yes"
        )
        assert await r.resolve_pending() == 1
        assert r.resolved(FRA_WIN) == 1.0

    async def test_fetch_failure_never_raises_and_is_retried(self) -> None:
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads[FRA_WIN] = RuntimeError("rest boom")
        r = _resolver(source, clock, retry_after_s=30.0)
        r.note_missing(FRA_WIN)
        assert await r.resolve_pending() == 0  # swallowed, never crashes the tick
        assert r.resolved(FRA_WIN) is None
        clock.advance(31.0)
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="finalized", result="yes"
        )
        assert await r.resolve_pending() == 1
        assert r.resolved(FRA_WIN) == 1.0

    async def test_live_market_is_dropped_not_polled_forever(self) -> None:
        # A ticker that turns out to be a LIVE market belongs to the feed — it
        # is dropped from pending (no REST poll loop), and a book-flicker
        # re-note within the backoff window is deferred to the recheck floor
        # (at most one fetch per window). Past the floor, a re-note (its book
        # genuinely died while we hold it) starts resolution over.
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads["LIVE-M"] = market_payload(
            "LIVE-M", status="active", result=""
        )
        r = _resolver(source, clock, retry_after_s=30.0)
        r.note_missing("LIVE-M")
        assert await r.resolve_pending() == 0
        assert len(source.calls) == 1
        # Flicker: an immediate re-note is deferred, never refetched this pass.
        r.note_missing("LIVE-M")
        assert not r.has_due_pending
        assert await r.resolve_pending() == 0
        assert len(source.calls) == 1
        # Past the floor the re-noted ticker is due again (real book death).
        clock.advance(31.0)
        assert r.has_due_pending
        assert await r.resolve_pending() == 0
        assert len(source.calls) == 2

    async def test_disputed_result_is_not_a_fact(self) -> None:
        # A result under dispute may still change — fail-closed: UNKNOWN,
        # retried until the exchange finalizes.
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="disputed", result="yes"
        )
        r = _resolver(source, clock, retry_after_s=30.0)
        r.note_missing(FRA_WIN)
        assert await r.resolve_pending() == 0
        assert r.resolved(FRA_WIN) is None
        clock.advance(31.0)
        assert r.has_due_pending  # still retried, not dropped

    async def test_scalar_result_is_permanently_unresolvable(self) -> None:
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads["SCLR"] = market_payload(
            "SCLR", status="determined", result="scalar"
        )
        r = _resolver(source, clock)
        r.note_missing("SCLR")
        assert await r.resolve_pending() == 0
        assert r.resolved("SCLR") is None
        clock.advance(3600.0)
        r.note_missing("SCLR")  # no-op: known unresolvable
        assert not r.has_due_pending
        assert len(source.calls) == 1

    async def test_settlement_value_cross_check(self) -> None:
        # A present settlement_value_dollars must AGREE with the binary result
        # (yes ⇒ $1, no ⇒ $0) or the row is refused (never cached).
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="finalized", result="yes",
            settlement_value_dollars="1.0000",
        )
        source.payloads[FRAENG_O5] = market_payload(
            FRAENG_O5, status="finalized", result="yes",
            settlement_value_dollars="0.0000",  # contradicts result=yes
        )
        r = _resolver(source, clock)
        r.note_missing(FRA_WIN)
        r.note_missing(FRAENG_O5)
        assert await r.resolve_pending() == 1
        assert r.resolved(FRA_WIN) == 1.0
        assert r.resolved(FRAENG_O5) is None  # refused, fail-closed

    async def test_fetch_pass_is_bounded(self) -> None:
        source = FakeMarketSource()
        clock = FakeClock()
        for i in range(7):
            t = f"T{i}"
            source.payloads[t] = market_payload(t, status="finalized", result="yes")
        r = _resolver(source, clock, fetch_budget_per_pass=5)
        for i in range(7):
            r.note_missing(f"T{i}")
        assert await r.resolve_pending() == 5  # bounded per pass
        assert len(source.calls) == 5
        assert await r.resolve_pending() == 2  # the rest on the next pass


# --------------------------------------------------------------------------- #
# Degenerate (settled) marginals and the structural machinery.                 #
# --------------------------------------------------------------------------- #


class TestStructuralExclusion:
    def test_degenerate_marginals_never_enter_structural_inversion(self) -> None:
        # Two parseable ADVANCE legs of one game normally identify a plan…
        game = "26JUL19ESPARG"
        adv_arg = f"KXWCADVANCE-{game}-ARG"
        adv_esp = f"KXWCADVANCE-{game}-ESP"
        event = f"KXWCADVANCE-{game}"
        cfg = StructuralConfigView()
        plans, copula = build_game_plans(
            [adv_arg, adv_esp], [event, event], [0.55, 0.45], cfg
        )
        assert len(plans) == 1 and copula == []
        # …but SETTLED (0/1) marginals are graded FACTS, not inversion targets:
        # they are excluded from the plan and ride the copula as constants.
        plans, copula = build_game_plans(
            [adv_arg, adv_esp], [event, event], [1.0, 0.0], cfg
        )
        assert plans == []
        assert sorted(copula) == [0, 1]
        # Mixed: the settled leg alone is excluded; a single live leg cannot
        # identify the model, so the whole game falls back to the copula
        # (fail-closed — never a plan inverted from a degenerate target).
        plans, copula = build_game_plans(
            [adv_arg, adv_esp], [event, event], [1.0, 0.45], cfg
        )
        assert all(0 not in p.global_indices for p in plans)

    def test_challenger_shock_never_touches_a_settled_fact(self) -> None:
        def marginals(ticker: str) -> float | None:
            return {"WON": 1.0, "LOST": 0.0, "LIVE": 0.80}[ticker]

        position = OpenPosition(
            position_id="p",
            combo_ticker="C",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(5_000),
            legs=(
                LegRef("WON", "E-A", "yes"),
                LegRef("LOST", "E-A", "no"),
                LegRef("LIVE", "E-B", "no"),
            ),
        )
        model = build_book_model([position], marginals=marginals)
        shocked = _shock_marginals(model, 0.05)
        assert shocked is not None
        by_ticker = {t: shocked[i] for t, i in model.leg_index.items()}
        assert by_ticker["WON"] == 1.0  # a fact is never shocked
        assert by_ticker["LOST"] == 0.0
        assert by_ticker["LIVE"] != 0.80  # a live mark still is


# --------------------------------------------------------------------------- #
# Lifecycle wiring: feed first, settled-cache second, else UNKNOWN.            #
# --------------------------------------------------------------------------- #


def _build(
    h: Harness,
    store: Store,
    *,
    bankroll_cc: int,
    settled: SettledMarginalResolver | None,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook]:
    """The test_book_risk_wiring rig + the settled resolver under test. Every
    %-cap is loose except the portfolio-CVaR one, so the CVaR gate (the live
    outage's failure point) is the deciding cap."""
    sender = FakeSender()
    exposure = ExposureBook(TEST_CONVENTIONS)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, h.clock,
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
            portfolio_cvar_frac=F(15, 100),
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
        config=LifecycleConfig(),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
        settled_marginals=settled,
    )
    return lifecycle, sender, exposure


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    for ticker in ("M1", "M2", "KXMVE-C1", FRA_WIN, FRAENG_O5):
        h.with_meta(ticker)
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _cross_game_position(
    pid: str, *, fra_side: str = "yes", o5_side: str = "no"
) -> OpenPosition:
    """The live outage combo shape: settled FRAENG legs + a live leg (M1)."""
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=CentiCents(5_000),
        legs=(
            LegRef(FRA_WIN, FRAENG_EVENT, fra_side),  # settled market
            LegRef(FRAENG_O5, FRAENG_EVENT, o5_side),  # settled market
            LegRef("M1", "E1", "no"),  # live market (Harness book)
        ),
    )


def _graded_source(
    *, fra_result: str = "yes", o5_result: str = "no"
) -> FakeMarketSource:
    source = FakeMarketSource()
    source.payloads[FRA_WIN] = market_payload(
        FRA_WIN, status="determined", result=fra_result
    )
    source.payloads[FRAENG_O5] = market_payload(
        FRAENG_O5, status="finalized", result=o5_result
    )
    return source


async def _resolve_via_maintenance(lifecycle: QuoteLifecycle) -> None:
    """Drive the REAL production sequence: the maintenance tick's recompute
    registers the missing committed legs, the tick launches the bounded fetch
    pass, and the results land in the permanent cache."""
    await lifecycle.maintenance_tick()
    task = lifecycle._settled_task  # noqa: SLF001 — single-flight task seam
    assert task is not None
    await task


class TestLifecycleWiring:
    async def test_settled_won_legs_full_conditional_exposure(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # Both settled legs WON (yes-leg settled YES; no-leg settled NO): the
        # parlay is alive on the remaining live leg — FULL conditional
        # exposure: the 0.99 tail is the entire premium at risk.
        h, store = harness
        settled = _resolver(_graded_source(), h.clock)
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        exposure.add_position(_cross_game_position("won"))
        await _resolve_via_maintenance(lifecycle)
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001 — snapshot seam
        assert snap is not None
        assert snap.unknown is False
        assert snap.usable
        # The live M1 leg is held NO with book P(yes)≈0.35 ⇒ the parlay hits
        # ~65% of the time ⇒ the 0.99 tail loss is the full 5_000cc premium.
        assert snap.es_99_cc == pytest.approx(5_000.0, abs=1.0)

    async def test_settled_lost_leg_zero_further_risk(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # The yes:FRA leg LOST (France lost): the parlay is deterministically
        # dead — as its seller we keep the premium in EVERY scenario: zero
        # further loss, P(profit) = 1.
        h, store = harness
        settled = _resolver(_graded_source(fra_result="no"), h.clock)
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        exposure.add_position(_cross_game_position("lost"))
        await _resolve_via_maintenance(lifecycle)
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None
        assert snap.usable
        assert snap.es_99_cc == 0.0
        assert snap.p_profit == 1.0

    async def test_unresolved_closed_market_stays_fail_closed(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # Game over but the exchange has NOT graded the result yet: no outcome
        # is inferred — the model stays UNKNOWN, the snapshot unusable, and
        # the CVaR cap keeps declining (fail-closed pinned).
        h, store = harness
        source = FakeMarketSource()
        for t in (FRA_WIN, FRAENG_O5):
            source.payloads[t] = market_payload(t, status="closed", result="")
        settled = _resolver(source, h.clock)
        lifecycle, sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        exposure.add_position(_cross_game_position("pending"))
        await _resolve_via_maintenance(lifecycle)
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None
        assert snap.unknown is True
        assert not snap.usable
        await lifecycle.handle_rfq(rfq())
        assert sender.created == []

    async def test_feed_alive_leg_unaffected_feed_takes_precedence(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # A (hypothetically stale/wrong) cached settled result for a ticker
        # whose book is LIVE on the feed must never shadow the feed: the
        # provider goes feed first, settled-cache second.
        h, store = harness
        source = FakeMarketSource()
        source.payloads["M1"] = market_payload(
            "M1", status="finalized", result="yes"
        )
        settled = _resolver(source, h.clock)
        settled.note_missing("M1")
        assert await settled.resolve_pending() == 1
        assert settled.resolved("M1") == 1.0
        lifecycle, _sender, _exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        p = lifecycle._marginals("M1")  # noqa: SLF001 — the provider seam
        assert p is not None
        assert p == h.feed.book("M1").top().microprice()  # the BOOK, not 1.0

    async def test_knob_false_no_resolver_old_behaviour(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # risk.settled_marginal_resolution=False ⇒ build_settled_resolver
        # wires NOTHING ⇒ the lifecycle behaves exactly as before the fix:
        # the settled-leg book stays UNKNOWN and no quote goes out.
        h, store = harness
        clock = FakeClock()
        assert (
            build_settled_resolver(
                RiskConfig(settled_marginal_resolution=False),
                FakeMarketSource(),
                clock,
            )
            is None
        )
        assert (
            build_settled_resolver(RiskConfig(), FakeMarketSource(), clock)
            is not None
        )  # default True
        lifecycle, sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=None
        )
        exposure.add_position(_cross_game_position("held"))
        await lifecycle.maintenance_tick()
        assert lifecycle._settled_task is None  # noqa: SLF001 — nothing launched
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None and snap.unknown and not snap.usable
        await lifecycle.handle_rfq(rfq())
        assert sender.created == []

    def test_retry_knob_validators(self) -> None:
        assert RiskConfig().settled_marginal_resolution is True
        assert RiskConfig().settled_resolution_retry_s == 30.0
        with pytest.raises(ValidationError):
            RiskConfig(settled_resolution_retry_s=0.0)
        with pytest.raises(ValidationError):
            RiskConfig(settled_resolution_retry_s=float("nan"))
        with pytest.raises(ValidationError):
            RiskConfig(settled_resolution_retry_s=float("inf"))

    async def test_e2e_outage_shape_cvar_gate_passes_again(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # The live outage, end to end: a 9-position book — cross-game combos
        # with settled FRAENG legs + live legs, plus pure-live positions.
        # BEFORE resolution: the snapshot is UNKNOWN and the CVaR cap fails
        # closed on every quote (book_risk_unusable — 366k audits, 0 quotes
        # live). AFTER the maintenance tick fetches the graded results: the
        # snapshot is usable, risk is CONDITIONAL on the settled facts, and
        # the SAME public path (_book_risk_for_check → limits.check inside
        # handle_rfq, the view the risk_audit reads) passes a quote again.
        h, store = harness
        settled = _resolver(_graded_source(), h.clock)
        lifecycle, sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        for i in range(6):  # six cross-game combos with settled FRAENG legs
            exposure.add_position(_cross_game_position(f"x{i}"))
        for i in range(3):  # three pure-live positions (books on the feed)
            exposure.add_position(
                OpenPosition(
                    position_id=f"live{i}",
                    combo_ticker=f"COMBO-live{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(100),
                    entry_price_cc=CentiCents(5_000),
                    legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
                )
            )
        assert len(exposure.positions) == 9

        # BEFORE: the outage — UNKNOWN snapshot, CVaR fails closed, no quote.
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None and snap.unknown and not snap.usable
        await lifecycle.handle_rfq(rfq())
        assert sender.created == []

        # AFTER: the maintenance tick registers + fetches the graded results…
        await _resolve_via_maintenance(lifecycle)
        assert settled.resolved(FRA_WIN) == 1.0
        assert settled.resolved(FRAENG_O5) == 0.0
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None
        assert snap.unknown is False
        assert snap.usable
        assert snap.n_positions == 9
        # …and the public risk path passes a quote again.
        risk = lifecycle._book_risk_for_check()  # noqa: SLF001 — audit's view
        assert risk is snap
        await lifecycle.handle_rfq(rfq())
        assert len(sender.created) == 1


# --------------------------------------------------------------------------- #
# Marginal-jump circuit breaker: settled-leg exemption (live halt 2026-07-18   #
# 02:17Z — "marginal became unreadable (had 1.000)" on a settled FRAENG leg    #
# hard-killed the bot 90s after preflight).                                    #
# --------------------------------------------------------------------------- #


def _breaker_rig(clock: FakeClock) -> tuple[CircuitBreakers, KillSwitch]:
    killswitch = KillSwitch(clock)
    return CircuitBreakers(killswitch, BreakerThresholds(), clock), killswitch


def _tick(
    marginals: dict[str, float | None],
    settled: frozenset[str] = frozenset(),
) -> BreakerInputs:
    """A healthy-everything-else breaker input carrying only the marginal watch
    under test (the breaker's PUBLIC check path consumes exactly this)."""
    return BreakerInputs(
        rx_age_s=0.1, marginals=marginals, settled_tickers=settled
    )


class TestBreakerSettledExemption:
    async def test_settled_fact_no_feed_book_never_trips(self) -> None:
        # (a) The fact is cached and the book is gone: marginal_of serves the
        # fact (1.0) forever. Held across ticks + past the 30s grace — never a
        # trip, never a halt, and the baseline is PURGED so nothing lingers.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        exempt = frozenset({FRA_WIN})
        for _ in range(4):
            verdict = await breakers.evaluate_and_halt(
                _tick({FRA_WIN: 1.0}, exempt)
            )
            assert verdict.tripped is False
            clock.advance(31.0)
        assert killswitch.halted is False
        assert FRA_WIN not in breakers._last_marginal  # noqa: SLF001 — purged

    async def test_live_outage_sequence_unreadable_settled_leg_no_halt(
        self,
    ) -> None:
        # (a, the exact 02:17Z shape) tick 1: a last feed echo reads 1.000 —
        # baseline seeds. Book then leaves the feed; the resolver's fetch finds
        # the market CLOSED (not yet graded, marginal still None) so the
        # sampler marks the leg exchange-confirmed non-live. The old breaker
        # tripped "became unreadable (had 1.000)" and hard-halted after the
        # 30s grace; now the exempt leg is skipped — sustained forever, no halt.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        v1 = await breakers.evaluate_and_halt(_tick({FRA_WIN: 1.000}))
        assert v1.tripped is False  # baseline seeded from the live echo
        clock.advance(15.0)
        exempt = frozenset({FRA_WIN})
        for _ in range(4):  # far past the 30s sustained-grace window
            verdict = await breakers.evaluate_and_halt(
                _tick({FRA_WIN: None}, exempt)
            )
            assert verdict.tripped is False
            clock.advance(31.0)
        assert killswitch.halted is False

    async def test_live_to_settled_grading_is_not_a_jump(self) -> None:
        # (b) live 0.60 -> graded fact 1.0 (delta 0.40 > max_jump 0.25): a
        # settlement, not a feed move — must NOT trip.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        assert not (await breakers.evaluate_and_halt(_tick({FRA_WIN: 0.60}))).tripped
        verdict = await breakers.evaluate_and_halt(
            _tick({FRA_WIN: 1.0}, frozenset({FRA_WIN}))
        )
        assert verdict.tripped is False
        assert killswitch.halted is False

    async def test_same_transition_without_exemption_would_trip(self) -> None:
        # The exemption is LOAD-BEARING: the identical 0.60 -> 1.0 sequence
        # WITHOUT the settled set still trips the jump detector (pins that the
        # fix is the exemption, not a loosened threshold).
        clock = FakeClock()
        breakers, _killswitch = _breaker_rig(clock)
        assert not (await breakers.evaluate_and_halt(_tick({FRA_WIN: 0.60}))).tripped
        verdict = await breakers.evaluate_and_halt(_tick({FRA_WIN: 1.0}))
        assert verdict.tripped is True
        assert verdict.reason is ReasonCode.HALT_MARGINAL_JUMP

    async def test_genuinely_unreadable_live_market_still_halts(self) -> None:
        # (c) regression: a LIVE market's leg that becomes unreadable (dead
        # feed, NOT exchange-confirmed non-live) trips, holds the 30s grace,
        # and hard-halts when sustained — exactly the pre-fix contract.
        clock = FakeClock()
        breakers, killswitch = _breaker_rig(clock)
        assert not (await breakers.evaluate_and_halt(_tick({"LIVE-M": 0.60}))).tripped
        v_hold = await breakers.evaluate_and_halt(_tick({"LIVE-M": None}))
        assert v_hold.tripped is True  # transient hold starts
        assert killswitch.halted is False  # grace window — not yet
        clock.advance(31.0)
        v_sustained = await breakers.evaluate_and_halt(_tick({"LIVE-M": None}))
        assert v_sustained.tripped is True
        assert killswitch.halted is True  # sustained past grace: hard halt
        assert v_sustained.reason is ReasonCode.HALT_MARGINAL_JUMP


# --------------------------------------------------------------------------- #
# Call-site wiring: resolver liveness knowledge -> lifecycle exemption ->      #
# breaker-input sampler.                                                       #
# --------------------------------------------------------------------------- #


class TestSettledWatchExemptWiring:
    async def test_resolver_market_no_longer_live_knowledge(self) -> None:
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="closed", result=""
        )
        source.payloads[FRAENG_O5] = market_payload(
            FRAENG_O5, status="finalized", result="no"
        )
        source.payloads["DISP"] = market_payload(
            "DISP", status="disputed", result="yes"
        )
        source.payloads["SCLR"] = market_payload(
            "SCLR", status="determined", result="scalar"
        )
        source.payloads["LIVE-M"] = market_payload(
            "LIVE-M", status="active", result=""
        )
        source.payloads["ERR"] = RuntimeError("rest boom")
        r = _resolver(source, clock, fetch_budget_per_pass=10)
        for t in (FRA_WIN, FRAENG_O5, "DISP", "SCLR", "LIVE-M", "ERR"):
            r.note_missing(t)
        await r.resolve_pending()
        # Closed-but-UNGRADED: no fact (marginal None) but exchange-confirmed
        # non-live — the exact 02:17Z gap the exemption must cover.
        assert r.resolved(FRA_WIN) is None
        assert r.market_no_longer_live(FRA_WIN) is True
        # A cached graded fact is non-live too.
        assert r.market_no_longer_live(FRAENG_O5) is True
        # Disputed / scalar: no 0/1 fact, but the market is over.
        assert r.market_no_longer_live("DISP") is True
        assert r.market_no_longer_live("SCLR") is True
        # LIVE market and a FAILED fetch: fail-closed — keep the full watch.
        assert r.market_no_longer_live("LIVE-M") is False
        assert r.market_no_longer_live("ERR") is False
        assert r.market_no_longer_live("NEVER-FETCHED") is False

    async def test_lifecycle_settled_watch_exempt(
        self, harness: tuple[Harness, Store]
    ) -> None:
        h, store = harness
        source = FakeMarketSource()
        source.payloads[FRA_WIN] = market_payload(
            FRA_WIN, status="closed", result=""
        )
        settled = _resolver(source, h.clock)
        settled.note_missing(FRA_WIN)
        await settled.resolve_pending()
        lifecycle, _sender, _exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        assert lifecycle.settled_watch_exempt(FRA_WIN) is True
        assert lifecycle.settled_watch_exempt("M1") is False  # live, feed-owned
        # No resolver wired: never exempt (the breaker keeps its full watch).
        bare, _s, _e = _build(h, store, bankroll_cc=10**11, settled=None)
        assert bare.settled_watch_exempt(FRA_WIN) is False

    def test_sampler_surfaces_settled_exemption(self, tmp_path: Path) -> None:
        # (d) The REAL sampler (`_sample_breaker_inputs`) carries the exempt
        # set for exactly the book legs the lifecycle reports non-live, and
        # still reads every leg's marginal from the provider.
        from tests.test_quote_app_phase6 import (
            FakeFeed as Phase6Feed,
        )
        from tests.test_quote_app_phase6 import (
            FakeMetadata as Phase6Metadata,
        )
        from tests.test_quote_app_phase6 import (
            _demo_app,
        )

        class _ExemptLifecycle:
            def __init__(
                self, marginals: dict[str, float | None], settled: set[str]
            ) -> None:
                self._m = marginals
                self._s = settled

            def marginal_of(self, market_ticker: str) -> float | None:
                return self._m.get(market_ticker)

            def settled_watch_exempt(self, market_ticker: str) -> bool:
                return market_ticker in self._s

        app = _demo_app(tmp_path)
        exposure = ExposureBook(TEST_CONVENTIONS)
        exposure.add_position(_cross_game_position("held"))
        lifecycle = _ExemptLifecycle(
            {FRA_WIN: 1.0, FRAENG_O5: 0.0, "M1": 0.35},
            {FRA_WIN, FRAENG_O5},
        )
        feed = Phase6Feed(rx_age_s=0.1, warm=True, seq_gap=False)
        inputs = app._sample_breaker_inputs(  # noqa: SLF001 — sampler seam
            feed,  # type: ignore[arg-type]
            lifecycle,  # type: ignore[arg-type]
            exposure,
            Phase6Metadata(),  # type: ignore[arg-type]
        )
        assert inputs.settled_tickers == frozenset({FRA_WIN, FRAENG_O5})
        assert inputs.marginals == {FRA_WIN: 1.0, FRAENG_O5: 0.0, "M1": 0.35}


# --------------------------------------------------------------------------- #
# RELIGHT2 STALL (2026-07-19 02:40Z, live_20260719_relight2.log): only 3 of    #
# ~12 graded, exchange-finalized FRAENG facts ever resolved in 25 minutes —    #
# registration rode the serial provider walks (one blocker-gated leg at a      #
# time) instead of a batch, so graded facts sat unfetched behind UNGRADED/     #
# active blockers. Fixes pinned here: (1) BATCH registration of every dark     #
# committed leg on every maintenance tick; (2) never-fetched priority over     #
# backoff retries in the per-pass budget; (4) the settled_resolution_pending   #
# observability line.                                                          #
# --------------------------------------------------------------------------- #


UNGRADED_BLOCKER = "KXWCTOTAL-26JUL18FRAENG-6"  # closed, not yet graded


def _graded_ticker(i: int) -> str:
    return f"KXWCGRD-26JUL18FRAENG-{i}"


class TestBatchRegistrationAndPriority:
    def test_batch_registrar_registers_all_dark_legs_at_once(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # Requirement 1: EVERY committed leg with no feed book registers
        # IMMEDIATELY — one call, no serial-walk gating, blockers irrelevant.
        h, store = harness
        settled = _resolver(FakeMarketSource(), h.clock)
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        for i in range(9):
            exposure.add_position(
                OpenPosition(
                    position_id=f"p{i}",
                    combo_ticker=f"COMBO-p{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(100),
                    entry_price_cc=CentiCents(5_000),
                    legs=(
                        # The UNGRADED blocker deliberately FIRST in leg order
                        # (the serial-walk stall shape).
                        LegRef(UNGRADED_BLOCKER, FRAENG_EVENT, "yes"),
                        LegRef(_graded_ticker(i), FRAENG_EVENT, "yes"),
                        LegRef("M1", "E1", "no"),
                    ),
                )
            )
        lifecycle._register_settled_candidates()  # noqa: SLF001 — the registrar
        pending = set(settled._pending)  # noqa: SLF001 — queue seam
        expected = {UNGRADED_BLOCKER} | {_graded_ticker(i) for i in range(9)}
        assert pending == expected  # all 10 dark legs, one call; M1 (live) not

    async def test_never_fetched_priority_over_backoff_retries(self) -> None:
        # Requirement 2: freshly-registered tickers beat backoff retries for
        # the per-pass budget — a graded fact lands within ~2 passes of
        # registration no matter how many ungraded tickers are cycling.
        source = FakeMarketSource()
        clock = FakeClock()
        cycling = [f"CYC-{i}" for i in range(6)]
        for t in cycling:
            source.payloads[t] = market_payload(t, status="closed", result="")
        r = _resolver(source, clock, retry_after_s=30.0, fetch_budget_per_pass=5)
        for t in cycling:
            r.note_missing(t)
        await r.resolve_pending()  # 5 fetched, on backoff
        await r.resolve_pending()  # 6th fetched
        clock.advance(31.0)  # every cycling ticker is now a DUE RETRY
        source.calls.clear()
        graded = [f"NEW-{i}" for i in range(5)]
        for t in graded:
            source.payloads[t] = market_payload(t, status="finalized", result="yes")
            r.note_missing(t)
        # One pass: the budget must go to the 5 NEVER-FETCHED tickers, not the
        # 6 due retries that were registered (and inserted) earlier.
        assert await r.resolve_pending() == 5
        assert source.calls == graded
        for t in graded:
            assert r.resolved(t) == 1.0

    async def test_relight2_shape_graded_resolve_within_two_passes(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # Requirement 3, end to end through the REAL maintenance path: a
        # 9-position book, every position ordering an UNGRADED leg before a
        # GRADED one. All 9 graded facts must land within TWO resolve passes;
        # the ungraded blocker stays pending; the snapshot stays UNUSABLE
        # while it blocks (fail-closed unchanged) and flips USABLE as soon as
        # the exchange grades it.
        h, store = harness
        source = FakeMarketSource()
        source.payloads[UNGRADED_BLOCKER] = market_payload(
            UNGRADED_BLOCKER, status="closed", result=""
        )
        for i in range(9):
            source.payloads[_graded_ticker(i)] = market_payload(
                _graded_ticker(i), status="finalized", result="yes"
            )
        settled = _resolver(source, h.clock)
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        for i in range(9):
            exposure.add_position(
                OpenPosition(
                    position_id=f"p{i}",
                    combo_ticker=f"COMBO-p{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(100),
                    entry_price_cc=CentiCents(5_000),
                    legs=(
                        LegRef(UNGRADED_BLOCKER, FRAENG_EVENT, "yes"),
                        LegRef(_graded_ticker(i), FRAENG_EVENT, "yes"),
                        LegRef("M1", "E1", "no"),
                    ),
                )
            )
        # TWO maintenance ticks = two bounded fetch passes (budget 5 each).
        for _ in range(2):
            await lifecycle.maintenance_tick()
            task = lifecycle._settled_task  # noqa: SLF001
            assert task is not None
            await task
        for i in range(9):
            assert settled.resolved(_graded_ticker(i)) == 1.0  # within 2 passes
        assert settled.resolved(UNGRADED_BLOCKER) is None
        assert UNGRADED_BLOCKER in settled._pending  # noqa: SLF001 — retried
        # Fail-closed: the truly-ungraded leg still blocks every position.
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None and snap.unknown and not snap.usable
        # The exchange grades the blocker ⇒ the next backoff retry resolves it
        # ⇒ the snapshot becomes usable (facts now suffice).
        source.payloads[UNGRADED_BLOCKER] = market_payload(
            UNGRADED_BLOCKER, status="finalized", result="no"
        )
        h.clock.advance(31.0)  # past the retry backoff
        await lifecycle.maintenance_tick()
        task = lifecycle._settled_task  # noqa: SLF001
        assert task is not None
        await task
        assert settled.resolved(UNGRADED_BLOCKER) == 0.0
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None
        assert snap.usable
        # Blocker settled NO on yes-side legs ⇒ every parlay dead ⇒ the whole
        # book is locked profit for its seller (conditional risk exact).
        assert snap.es_99_cc == 0.0
        assert snap.p_profit == 1.0

    async def test_pending_observability_line_emitted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Requirement 4: every pass over a non-empty pending set reports what
        # the resolver knows (n_pending / n_never_fetched / sample) — the
        # line that would have answered relight2's "what is it waiting on?"
        # instantly.
        source = FakeMarketSource()
        clock = FakeClock()
        source.payloads["T-A"] = market_payload("T-A", status="closed", result="")
        r = _resolver(source, clock)
        r.note_missing("T-A")
        r.note_missing("T-B")  # no payload ⇒ fetch fails ⇒ stays pending
        await r.resolve_pending()
        out = capsys.readouterr().out
        assert "settled_resolution_pending" in out
        assert "n_pending" in out and "n_never_fetched" in out
        assert "T-A" in out  # the sample names the tickers


# --------------------------------------------------------------------------- #
# RELIGHT3 (2026-07-19, live_20260719_batchfacts.log): 9 exchange-finalized    #
# FRAENG legs never registered — settled markets can retain VALID-but-EMPTY    #
# husk books in the feed, so the registrar's "has a valid book object" test    #
# and the provider's "can read a microprice" test DIVERGED (microprice() is    #
# None on an empty/one-sided book, so the provider returned None while the    #
# registrar skipped the leg as feed-owned). Fix: ONE shared predicate          #
# (`_feed_marginal`) consumed by both.                                         #
# --------------------------------------------------------------------------- #


async def _husk_books(h: Harness, empty: list[str], invalid: list[str]) -> None:
    """Create feed mirrors in the two unpriceable-but-present states: VALID
    with an EMPTY book (a settled market's lingering husk — the relight3
    shape) and INVALID (watched, no snapshot yet)."""
    from tests.test_feed import snapshot_env

    tickers = [*empty, *invalid]
    h.feed.watch(tickers)
    await h.ws.ack_subscription(len(h.ws.subscriptions) - 1, 90)
    for seq, ticker in enumerate(empty, start=1):
        env = snapshot_env(90, seq, ticker)
        env["msg"]["yes_dollars_fp"] = []
        env["msg"]["no_dollars_fp"] = []
        await h.ws.deliver(env)
    # ``invalid`` tickers: watched (mirror object EXISTS) but no snapshot ⇒
    # mirror stays invalid.


class TestSharedFeedReadabilityPredicate:
    async def test_husk_book_legs_register_resolve_and_snapshot_usable(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # The exact relight3 shape: 9 committed legs, exchange-finalized, but
        # their feed books still EXIST (valid-but-empty husks / invalid
        # mirrors). They must register on the next tick, resolve within TWO
        # passes, and the snapshot must become usable.
        h, store = harness
        husks = [f"HUSK-{i}" for i in range(9)]
        await _husk_books(
            h,
            empty=[t for i, t in enumerate(husks) if i % 2 == 0],
            invalid=[t for i, t in enumerate(husks) if i % 2 == 1],
        )
        source = FakeMarketSource()
        for t in husks:
            source.payloads[t] = market_payload(t, status="finalized", result="yes")
        settled = _resolver(source, h.clock)
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        for i, t in enumerate(husks):
            exposure.add_position(
                OpenPosition(
                    position_id=f"p{i}",
                    combo_ticker=f"COMBO-p{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(100),
                    entry_price_cc=CentiCents(5_000),
                    legs=(LegRef(t, FRAENG_EVENT, "yes"), LegRef("M1", "E1", "no")),
                )
            )
        # Pre-fix pin: every husk book EXISTS in the feed (the old registrar
        # predicate would have skipped all of them) yet the provider cannot
        # price a single one.
        for t in husks:
            assert h.feed.book(t) is not None  # book OBJECT present
            assert lifecycle._feed_marginal(t) is None  # noqa: SLF001
        # Two maintenance ticks = registration + two bounded passes (5+4).
        for _ in range(2):
            await lifecycle.maintenance_tick()
            task = lifecycle._settled_task  # noqa: SLF001
            assert task is not None
            await task
        for t in husks:
            assert settled.resolved(t) == 1.0  # resolved within 2 passes
        lifecycle.recompute_book_risk()
        snap = lifecycle._book_risk  # noqa: SLF001
        assert snap is not None
        assert snap.unknown is False
        assert snap.usable

    async def test_registrar_iff_provider_feed_none_for_every_feed_state(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # The shared-predicate property: with no fact cached, the registrar
        # registers a committed leg ⟺ the provider's FEED-path read returns
        # None — across every feed state: no book at all, invalid mirror,
        # valid-but-EMPTY book, valid one-sided book, and a full two-sided
        # book (the only feed-owned state).
        h, store = harness
        await _husk_books(h, empty=["ST-EMPTY"], invalid=["ST-INVALID"])
        from tests.test_feed import snapshot_env

        h.feed.watch(["ST-ONESIDED"])
        await h.ws.ack_subscription(len(h.ws.subscriptions) - 1, 91)
        env = snapshot_env(91, 1, "ST-ONESIDED")
        env["msg"]["no_dollars_fp"] = []  # yes side only ⇒ microprice None
        await h.ws.deliver(env)
        states = ["ST-NOBOOK", "ST-INVALID", "ST-EMPTY", "ST-ONESIDED", "M1"]
        settled = _resolver(FakeMarketSource(), h.clock)
        lifecycle, _sender, exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        exposure.add_position(
            OpenPosition(
                position_id="all-states",
                combo_ticker="COMBO-states",
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(100),
                entry_price_cc=CentiCents(5_000),
                legs=tuple(LegRef(t, "E1", "yes") for t in states),
            )
        )
        lifecycle._register_settled_candidates()  # noqa: SLF001
        for t in states:
            provider_feed_none = lifecycle._feed_marginal(t) is None  # noqa: SLF001
            registered = t in settled._pending  # noqa: SLF001 — queue seam
            assert registered == provider_feed_none, t
        # And concretely: only the priceable two-sided book is feed-owned.
        assert "M1" not in settled._pending  # noqa: SLF001
        for t in ("ST-NOBOOK", "ST-INVALID", "ST-EMPTY", "ST-ONESIDED"):
            assert t in settled._pending  # noqa: SLF001

    async def test_cached_fact_serves_through_a_husk_book(
        self, harness: tuple[Harness, Store]
    ) -> None:
        # Corollary of the shared predicate: once the fact is cached, the
        # provider serves it even though a valid-but-EMPTY husk book still
        # exists (the old code returned the husk's None microprice EARLY and
        # never consulted the cache).
        h, store = harness
        await _husk_books(h, empty=["HUSK-FACT"], invalid=[])
        source = FakeMarketSource()
        source.payloads["HUSK-FACT"] = market_payload(
            "HUSK-FACT", status="finalized", result="no"
        )
        settled = _resolver(source, h.clock)
        settled.note_missing("HUSK-FACT")
        assert await settled.resolve_pending() == 1
        lifecycle, _sender, _exposure = _build(
            h, store, bankroll_cc=10**11, settled=settled
        )
        assert h.feed.book("HUSK-FACT").valid  # husk still present + "valid"
        assert lifecycle._marginals("HUSK-FACT") == 0.0  # noqa: SLF001 — the fact
