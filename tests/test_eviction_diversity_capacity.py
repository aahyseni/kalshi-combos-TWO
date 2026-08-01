"""DIVERSITY-AWARE EVICTION KEY + DERIVED OPEN-QUOTE CAPACITY (2026-07-31).

Operator ruling: "we can bump the 200 up, but 5 $1 EV quotes shouldn't lose
to 1 $5 EV quote, especially if the 5 quotes are diverse."

Covers, in order:
  * the pure primitives (``rfq/eviction_value.py``): size buckets, the exact
    Clopper-Pearson lower bound (including the underflow regime the duality
    form silently lied in), the table's derived discrimination criterion, the
    dES99 allocation, and the capacity derivation with every fail-closed arm;
  * the lifecycle slot axis: "off" byte-identical, "shadow" changes nothing
    but logs, "on" HOLDS the diverse small book against a bigger-EV candidate
    (the operator's sentence, as a test), falls back to absolute-EV when the
    tape is thin (fail closed), and anti-thrashes on its own ledger;
  * the derived capacity proves it can actually QUOTE at live-like inputs
    (the 2026-07-23 rule: a cap must produce a non-zero allowance before it
    may go live).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.rfq.eviction_value import (
    N_BUCKETS,
    SIZE_BUCKET_EDGES_CC,
    AcceptanceCounters,
    allocate_des99_cc,
    binom_tail_gt,
    clopper_pearson_lower,
    clopper_pearson_upper,
    derive_open_quote_capacity,
    size_bucket,
)
from combomaker.rfq.lifecycle import LifecycleConfig
from combomaker.risk.limits import Breach, LimitChecker, RiskLimits
from tests.test_det_hedge_credit_and_value_ranking import _cand_quote, _resting
from tests.test_pricing_engine import combo
from tests.test_renege_fixes import _lifecycle

ALPHA = 0.02  # the ratified portfolio_kill_tail_prob anchor


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


class TestSizeBucket:
    def test_edges_partition_the_line(self) -> None:
        assert size_bucket(0) == 0
        assert size_bucket(49_999) == 0  # $4.9999
        assert size_bucket(50_000) == 1  # $5 exactly starts bucket 1
        assert size_bucket(149_999) == 1
        assert size_bucket(150_000) == 2
        assert size_bucket(499_999) == 2
        assert size_bucket(500_000) == 3
        assert size_bucket(1_499_999) == 3
        assert size_bucket(1_500_000) == 4
        assert size_bucket(10**9) == N_BUCKETS - 1

    def test_partition_matches_the_flow_study(self) -> None:
        # $5 / $15 / $50 / $150 in cc — the measured 2026-07-31 buckets.
        assert SIZE_BUCKET_EDGES_CC == (50_000, 150_000, 500_000, 1_500_000)


class TestClopperPearsonLower:
    def test_exact_at_the_boundary(self) -> None:
        """The lower bound L satisfies P(X >= x; n, L) == alpha exactly."""
        lo = clopper_pearson_lower(3, 1000, ALPHA)
        assert 0.0 < lo < 3 / 1000
        assert abs(binom_tail_gt(1000, lo, 2) - ALPHA) < 1e-9

    def test_large_n_no_underflow(self) -> None:
        """The duality form ``1 - upper(n-x, n)`` underflows at large n and
        returns exactly x/n (zero-width — a lie in the ANTI-conservative
        direction). The direct bisection must stay strictly below x/n."""
        lo = clopper_pearson_lower(5, 50_000, ALPHA)
        assert 0.0 < lo < 5 / 50_000
        assert abs(binom_tail_gt(50_000, lo, 4) - ALPHA) < 1e-9

    def test_degenerate_inputs_fail_closed_to_zero(self) -> None:
        assert clopper_pearson_lower(0, 100, ALPHA) == 0.0
        assert clopper_pearson_lower(5, 0, ALPHA) == 0.0
        assert clopper_pearson_lower(-1, 100, ALPHA) == 0.0
        assert clopper_pearson_lower(101, 100, ALPHA) == 0.0
        assert clopper_pearson_lower(5, 100, 0.0) == 0.0
        assert clopper_pearson_lower(5, 100, 1.0) == 0.0

    def test_bracket_orders_correctly(self) -> None:
        lo = clopper_pearson_lower(8, 5_000, ALPHA)
        hi = clopper_pearson_upper(8, 5_000, ALPHA)
        assert lo < 8 / 5_000 < hi

    def test_underflow_regime_stays_conservative(self) -> None:
        """G3 boundary (gate note, 2026-08-01): once ``(1-p)^n`` underflows
        inside the bisection bracket (n*p > ~745 — multi-day counter scales,
        e.g. n=50k at p_hat=2%), the LOWER bound clips DOWN (candidate
        under-credited) and the UPPER converges to p_hat from above (its
        bisection lo starts at x/n, so an incumbent is never credited below
        its point estimate). Both directions conservative; inversion
        impossible (lower <= p_hat <= upper by bracket construction)."""
        x, n = 1_000, 50_000  # p_hat = 2%, n*p = 1000 >> 745
        p_hat = x / n
        lo = clopper_pearson_lower(x, n, ALPHA)
        hi = clopper_pearson_upper(x, n, ALPHA)
        # The invariants that make the regime SAFE (no inversion, both
        # failure directions conservative):
        assert 0.0 <= lo <= p_hat <= hi <= 1.0
        # The exact regime's lower bound would sit near ~0.0185; the
        # underflow clips it DOWN to the representability boundary
        # (~745/n) — strictly conservative, never inflating:
        assert lo <= 745 / n + 1e-6
        # The upper collapses onto the point estimate (documented G3
        # behaviour — the incumbent keeps at least p_hat credit):
        assert abs(hi - p_hat) < 1e-9


class TestAcceptanceCounters:
    def test_empty_table_is_not_discriminating(self) -> None:
        assert not AcceptanceCounters().discriminating(ALPHA)

    def test_one_carrying_bucket_is_not_discriminating(self) -> None:
        t = AcceptanceCounters()
        for _ in range(1000):
            t.record_quoted(10_000)
        t.record_accepted(10_000)
        assert not t.discriminating(ALPHA)

    def test_wide_intervals_are_not_discriminating(self) -> None:
        """Two buckets whose CP ratio exceeds the between-bucket spread: any
        ranking would be noise, so the table refuses to rank."""
        t = AcceptanceCounters()
        for _ in range(30):
            t.record_quoted(10_000)
            t.record_quoted(600_000)
        t.record_accepted(10_000)
        t.record_accepted(600_000)  # p_hats equal -> spread 1.0 < any ratio
        assert not t.discriminating(ALPHA)

    def test_grown_tape_discriminates(self) -> None:
        t = _measured_tape()
        assert t.discriminating(ALPHA)

    def test_bounds_none_without_denominator(self) -> None:
        t = AcceptanceCounters()
        assert t.bounds(0, ALPHA) is None

    def test_accept_recording_is_bucketed(self) -> None:
        t = AcceptanceCounters()
        t.record_quoted(10_000)
        t.record_accepted(10_000)
        b = t.bounds(0, ALPHA)
        assert b is not None and b.quoted == 1 and b.accepted == 1
        assert t.bounds(1, ALPHA) is None


def _measured_tape() -> AcceptanceCounters:
    """A tape shaped like the live 7/26-28 flow measurement: small quotes
    accept an order of magnitude more often than large ones."""
    t = AcceptanceCounters()
    for _ in range(2_000):
        t.record_quoted(10_000)  # bucket 0, <$5
        t.record_quoted(900_000)  # bucket 3, $50-150
    for _ in range(40):
        t.record_accepted(10_000)  # 20 per 1k
    for _ in range(2):
        t.record_accepted(900_000)  # 1 per 1k
    return t


class TestDes99Allocation:
    def test_diversifier_on_untouched_game_is_zero(self) -> None:
        assert (
            allocate_des99_cc({"G-NEW"}, 100_000, {"G-HOT": 50_000.0}, {"G-HOT": 1e6})
            == 0.0
        )

    def test_concentration_pays_its_share(self) -> None:
        got = allocate_des99_cc(
            {"G-HOT"}, 250_000, {"G-HOT": 100_000.0}, {"G-HOT": 1_000_000.0}
        )
        assert got == 100_000.0 * 0.25

    def test_hedge_credit_is_negative(self) -> None:
        got = allocate_des99_cc(
            {"G-H"}, 500_000, {"G-H": -40_000.0}, {"G-H": 1_000_000.0}
        )
        assert got == -40_000.0 * 0.5

    def test_missing_denominator_never_credits(self) -> None:
        assert allocate_des99_cc({"G"}, 100_000, {"G": 50_000.0}, {}) == 0.0
        assert allocate_des99_cc({"G"}, 100_000, {"G": 50_000.0}, {"G": 0.0}) == 0.0

    def test_share_is_capped_at_one(self) -> None:
        got = allocate_des99_cc(
            {"G"}, 2_000_000, {"G": 10_000.0}, {"G": 1_000_000.0}
        )
        assert got == 10_000.0


def _derive(**overrides):
    """Live-like defaults (Advanced tier 300 t/s observed; the tier-clamped
    supervisor withdraw budget 200 tok/10s = 20 t/s; measured ~0.44 sent/s
    on 2026-07-31 -> ~1.8 flow tokens/s; TTL 20s; documented create 2 +
    delete 2)."""
    kwargs = dict(
        tier_write_rate_per_s=300.0,
        reserve_rate_per_s=20.0,
        withdraw_rate_per_s=20.0,
        measured_flow_tokens_per_s=1.8,
        ttl_s=20.0,
        refresh_cost_tokens=4,
        delete_cost_tokens=2,
        fallback=200,
    )
    kwargs.update(overrides)
    return derive_open_quote_capacity(**kwargs)


class TestDerivedCapacity:
    def test_live_like_inputs_produce_a_non_zero_capacity(self) -> None:
        """The 2026-07-23 rule: a cap must be PROVEN to produce a non-zero
        allowance against real inputs BEFORE going live."""
        d = _derive()
        assert d.usable and d.reason == "derived"
        assert d.capacity >= 1

    def test_g1_withdraw_form_binds_at_todays_budget(self) -> None:
        """G1 REGRESSION (adversarial gate 2026-08-01): the tier form alone
        claimed 1,198-1,272 slots, but every delete flows through the bot's
        own 20 t/s withdraw budget — sustainable churn is ~200 refreshes per
        TTL, exactly the hand cap the manual bumps stalled at. The derived
        capacity must be the MIN of both bucket forms."""
        d = _derive()
        assert d.tier_capacity == int((300.0 - 20.0 - 1.8) * 20.0 / 4)  # 1391
        # withdraw form: (20 - 1.8 * 2/4) * 20 / 2 = 191
        assert d.withdraw_capacity == int((20.0 - 0.9) * 20.0 / 2)  # 191
        assert d.capacity == min(d.tier_capacity, d.withdraw_capacity) == 191
        # Self-consistent with the hand-bumped 200 the withdraw budget was
        # sized around — never ~6x over the delete path again:
        assert d.capacity <= 200

    def test_g1_capacity_is_sustainable_by_the_delete_path(self) -> None:
        """A derived capacity must be refreshable by the delete path's OWN
        budget: capacity quotes deleting once per TTL may never demand more
        than the withdraw bucket's sustained rate (with the measured new-flow
        delete share carved out on top)."""
        for flow in (0.0, 0.9, 1.8, 6.0, 20.0):
            d = _derive(measured_flow_tokens_per_s=flow)
            if not d.usable:
                continue
            delete_demand_per_s = (
                d.capacity * d.delete_cost_tokens / d.ttl_s
                + flow * d.delete_cost_tokens / d.refresh_cost_tokens
            )
            assert delete_demand_per_s <= d.withdraw_rate_per_s + 1e-9

    def test_g1_raised_withdraw_budget_scales_automatically(self) -> None:
        """The NORTH STAR point of the min-form: if the operator ever raises
        the withdraw budget, capacity scales by DERIVATION (no knob to move)
        until the tier form takes over as the binding bucket."""
        d = _derive(withdraw_rate_per_s=260.0)
        assert d.usable
        assert d.withdraw_capacity == int((260.0 - 0.9) * 20.0 / 2)  # 2591
        assert d.capacity == d.tier_capacity == 1391  # tier now binds

    def test_basic_tier_still_quotes(self) -> None:
        """Even the un-upgraded 100 t/s tier derives a workable book."""
        d = _derive(tier_write_rate_per_s=100.0)
        assert d.usable and d.capacity >= 1

    def test_no_measured_window_fails_closed_to_fallback(self) -> None:
        d = _derive(measured_flow_tokens_per_s=None)
        assert not d.usable and d.capacity == 200
        assert d.reason == "no_measured_flow_window"

    def test_no_headroom_fails_closed(self) -> None:
        d = _derive(tier_write_rate_per_s=25.0, measured_flow_tokens_per_s=6.0)
        assert not d.usable and d.capacity == 200 and d.reason == "no_headroom"

    def test_no_withdraw_headroom_fails_closed(self) -> None:
        """The withdraw form can be the bucket with no headroom too: measured
        new-quote deletes already eat the whole withdraw budget."""
        d = _derive(withdraw_rate_per_s=0.4, measured_flow_tokens_per_s=1.8)
        assert not d.usable and d.capacity == 200 and d.reason == "no_headroom"

    def test_bad_tier_ttl_or_withdraw_fails_closed(self) -> None:
        assert not _derive(tier_write_rate_per_s=0.0).usable
        assert not _derive(ttl_s=0.0).usable
        assert not _derive(refresh_cost_tokens=0).usable
        assert not _derive(delete_cost_tokens=0).usable
        assert (
            _derive(withdraw_rate_per_s=0.0).reason == "withdraw_rate_unusable"
        )


# --------------------------------------------------------------------------
# lifecycle slot axis
# --------------------------------------------------------------------------


def _slot_breach() -> list[Breach]:
    return [Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "at cap", shadow=False)]


async def _rig(tmp_path: Path, diversity_mode: str):
    lc = await _lifecycle(
        tmp_path,
        LifecycleConfig(
            open_quote_ev_eviction=True,
            det_budget_value_ranking="off",
            eviction_diversity_key=diversity_mode,
        ),
    )
    lc._limits = LimitChecker(  # noqa: SLF001 - test rig wiring
        RiskLimits(caps_shadow_mode=True, per_combo_loss_frac=Fraction(99, 100))
    )
    lc._marginals = lambda t: 0.5  # noqa: SLF001
    return lc


async def _attempt(lc, *, no_bid_cc: int, fair_cc: int, contracts: int = 10_000):
    rfq = combo(
        [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
        id="rc", market_ticker="KXMVE-C", contracts_fp="100.00",
    )
    result = _cand_quote(no_bid_cc, fair_cc)
    qty = CentiContracts(contracts)
    qrisk = lc._quote_risk(rfq, result, quote_id="pending", qty=qty)  # noqa: SLF001
    return await lc._try_slot_eviction(  # noqa: SLF001
        rfq, result, qty, qrisk, _slot_breach()
    )


def _five_small(lc) -> None:
    """Five diverse small resting quotes: EV 10_000cc ($1) each, det $10
    (bucket 1... no — no_bid 1_000 x 10_000 contracts // 100 = 100_000cc =
    $10, bucket 1). Each on its OWN game (the ``_resting`` helper keys the
    game off the qid)."""
    for i in range(5):
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting(f"small{i}", 10_000, no_bid_cc=1_000)
        )


def _tape_for(lc) -> None:
    """A measured tape whose buckets cover both the small ($10 -> bucket 1)
    and fat ($90 -> bucket 3) quotes, small ~20x likelier to be taken."""
    t = lc._accept_tape  # noqa: SLF001
    for _ in range(2_000):
        t.record_quoted(100_000)  # bucket 1
        t.record_quoted(900_000)  # bucket 3
    for _ in range(40):
        t.record_accepted(100_000)
    for _ in range(2):
        t.record_accepted(900_000)


class TestDiversityKeyOnLifecycle:
    async def test_operator_sentence_small_diverse_book_holds(
        self, tmp_path: Path
    ) -> None:
        """5 x $1-EV diverse quotes do NOT lose to 1 x $5-EV quote: measured
        acceptance makes the small book's realizable EV larger, and the
        verdict is a MEASURED HOLD (never a fall-through to absolute-EV)."""
        lc = await _rig(tmp_path, "on")
        _five_small(lc)
        _tape_for(lc)
        # Candidate: NO at 9_000 on fair 4_000 -> edge (9-4)x100 = 50_000cc
        # ($5 EV), det 900_000cc ($90, bucket 3).
        out = await _attempt(lc, no_bid_cc=9_000, fair_cc=500)
        assert all(
            f"small{i}" in lc._exposure.open_quotes for i in range(5)  # noqa: SLF001
        ), "a diverse small quote was evicted for the fat candidate"
        assert [b.reason for b in out] == [ReasonCode.SKIP_MAX_OPEN_QUOTES]
        assert lc._metrics.counter("quote.eviction_diversity_hold") == 1  # noqa: SLF001

    async def test_absolute_ev_would_have_evicted_the_small_quote(
        self, tmp_path: Path
    ) -> None:
        """The control: exactly the same book/candidate under the absolute-EV
        key (diversity off) evicts a small quote — proving the hold above is
        the diversity key's doing."""
        lc = await _rig(tmp_path, "off")
        _five_small(lc)
        _tape_for(lc)
        await _attempt(lc, no_bid_cc=9_000, fair_cc=500)
        assert (
            sum(
                f"small{i}" in lc._exposure.open_quotes  # noqa: SLF001
                for i in range(5)
            )
            == 4
        ), "absolute-EV should evict exactly one small quote"

    async def test_thin_tape_falls_back_to_absolute_ev(
        self, tmp_path: Path
    ) -> None:
        """'on' with an unmeasured tape behaves EXACTLY like today (fail
        closed): the small quote is evicted on absolute EV."""
        lc = await _rig(tmp_path, "on")
        _five_small(lc)
        # No tape recorded at all.
        await _attempt(lc, no_bid_cc=9_000, fair_cc=500)
        assert (
            sum(
                f"small{i}" in lc._exposure.open_quotes  # noqa: SLF001
                for i in range(5)
            )
            == 4
        )
        assert lc._metrics.counter("quote.eviction_diversity_thin") >= 1  # noqa: SLF001

    async def test_shadow_changes_nothing_but_counts(
        self, tmp_path: Path
    ) -> None:
        """Shadow: the absolute-EV decision rules exactly as today AND the
        diversity verdict is measured (metric present)."""
        lc = await _rig(tmp_path, "shadow")
        _five_small(lc)
        _tape_for(lc)
        await _attempt(lc, no_bid_cc=9_000, fair_cc=500)
        # Absolute-EV outcome, unchanged by the shadow verdict:
        assert (
            sum(
                f"small{i}" in lc._exposure.open_quotes  # noqa: SLF001
                for i in range(5)
            )
            == 4
        )
        assert lc._metrics.counter("quote.eviction_diversity_shadow") == 1  # noqa: SLF001

    async def test_diversity_evicts_the_concentrated_low_value_quote(
        self, tmp_path: Path
    ) -> None:
        """The other direction: a fat low-realizable-EV incumbent LOSES to a
        small diverse candidate under the diversity key."""
        lc = await _rig(tmp_path, "on")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 30_000, no_bid_cc=9_000)
        )
        _tape_for(lc)
        # Small candidate: NO at 1_000 on fair 0 -> lower-EV in absolute
        # terms (10_000cc, $1) but 20x the measured acceptance.
        await _attempt(lc, no_bid_cc=1_000, fair_cc=8_900)
        assert "fat" not in lc._exposure.open_quotes  # noqa: SLF001
        assert lc._metrics.counter("quote.evictions.slot") == 1  # noqa: SLF001

    async def test_slot_thrash_ledger_blocks_the_bounce_back(
        self, tmp_path: Path
    ) -> None:
        lc = await _rig(tmp_path, "on")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 30_000, no_bid_cc=9_000)
        )
        _tape_for(lc)
        await _attempt(lc, no_bid_cc=1_000, fair_cc=8_900)
        assert "fat" not in lc._exposure.open_quotes  # noqa: SLF001
        # The evicted combo (C-fat) may not evict its way back at a key at
        # or below the one that displaced it.
        assert "C-fat" in lc._evicted_slot_key  # noqa: SLF001
        prior = lc._evicted_slot_key["C-fat"]  # noqa: SLF001
        assert lc._slot_thrash_blocked("C-fat", prior)  # noqa: SLF001
        assert not lc._slot_thrash_blocked("C-fat", prior + 1.0)  # noqa: SLF001

    async def test_off_mode_records_no_diversity_metrics(
        self, tmp_path: Path
    ) -> None:
        lc = await _rig(tmp_path, "off")
        _five_small(lc)
        _tape_for(lc)
        await _attempt(lc, no_bid_cc=9_000, fair_cc=500)
        for m in (
            "quote.eviction_diversity_hold",
            "quote.eviction_diversity_shadow",
            "quote.eviction_diversity_thin",
        ):
            assert lc._metrics.counter(m) == 0  # noqa: SLF001


class TestConfigPlumbing:
    def test_risk_config_defaults_are_off(self) -> None:
        from combomaker.ops.config import RiskConfig

        cfg = RiskConfig()
        assert cfg.eviction_diversity_key == "off"
        assert cfg.open_quote_capacity_derived == "off"

    def test_modes_validate(self) -> None:
        import pytest

        from combomaker.ops.config import RiskConfig

        RiskConfig(eviction_diversity_key="shadow")
        RiskConfig(open_quote_capacity_derived="on")
        with pytest.raises(ValueError):
            RiskConfig(eviction_diversity_key="live")
        with pytest.raises(ValueError):
            RiskConfig(open_quote_capacity_derived="yes")

    def test_build_lifecycle_config_plumbs_the_knob(self) -> None:
        from combomaker.ops.config import RiskConfig
        from combomaker.ops.quote_app import build_lifecycle_config

        cfg = build_lifecycle_config(RiskConfig(eviction_diversity_key="shadow"))
        assert cfg.eviction_diversity_key == "shadow"
