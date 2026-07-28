"""FIX 3 (hedge ACCOUNTING in det-max) + FIX 4 (value-ranked allocation of the
fixed det-max budget) — 2026-07-28.

FIX 3 is the RISK LEDGER, deliberately distinct from the 2026-07-27 skew work
(which changed the PRICE we quote on offsetting flow). ``_mutex_aware_det_fold``
bucketed only long-NO units per game, so a COMPLEMENT position — the opposite
side of a combo we already hold, which provably cannot lose when that one does —
fell into the comonotone residual and was charged its FULL premium ON TOP of the
position it offsets. Certification is EXACT STATE ENUMERATION over leg-outcome
literals, never a leg-sign heuristic; anything ambiguous stays charged in full.

FIX 4 reuses the existing ``open_quote_ev_eviction`` mechanism (one eviction
path, not two) and extends it onto the det-max axis, ranking by DENSITY = EV per
unit of consumed det-max. It never admits anything the caps refuse — the SAME
``LimitChecker.check`` re-runs after any eviction.

Both default to SHADOW.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.pricing.quote import ConstructedQuote
from combomaker.rfq.lifecycle import LifecycleConfig
from combomaker.risk.exposure import LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.limits import Breach, DailyPnl, LimitChecker, RiskLimits
from combomaker.sim.book_risk import (
    DetMaxUnit,
    _certified_cannot_both_lose,
    _hedge_offset_credit_cc,
    _loss_literals,
    mutex_aware_det_max_and_credit,
    mutex_aware_det_max_from_units,
)
from tests.test_pricing_engine import combo
from tests.test_renege_fixes import _lifecycle

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _leg(ticker: str, event: str = "KX-G1", side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


def _unit(
    uid: str,
    legs: tuple[LegRef, ...],
    *,
    side: Side = Side.NO,
    price_cc: int = 5_000,
    contracts_centi: int = 10_000,
    risk_modeled: bool = True,
) -> DetMaxUnit:
    """One det-max unit. ``loss_cc`` uses the live arithmetic
    (``price x contracts``) so parity with the fold is exact."""
    return DetMaxUnit(
        unit_id=uid,
        our_side=side,
        contracts_centi=contracts_centi,
        entry_price_cc=price_cc,
        legs=legs,
        loss_cc=float(price_cc) * (contracts_centi / 100),
        risk_modeled=risk_modeled,
    )


# Two distinct games so no structural game bucket ever nets these units — the
# credit must come from the offsetting-position axis alone.
LEGS_A = (_leg("MKT-A", "KX-G1"), _leg("MKT-B", "KX-G2"))


# --------------------------------------------------------------------------
# FIX 3 — certification (state enumeration over leg literals)
# --------------------------------------------------------------------------


class TestCertification:
    def test_same_combo_opposite_side_is_certified(self) -> None:
        a = _unit("a", LEGS_A, side=Side.NO)
        b = _unit("b", LEGS_A, side=Side.YES)
        assert _certified_cannot_both_lose(
            a.our_side, _loss_literals(a) or {}, b.our_side, _loss_literals(b) or {}
        )

    def test_yes_subparlay_of_held_no_is_certified(self) -> None:
        # NO on {A,B} loses only if BOTH hit; YES on {A} loses only if A misses.
        a = _unit("a", LEGS_A, side=Side.NO)
        b = _unit("b", (LEGS_A[0],), side=Side.YES)
        assert _certified_cannot_both_lose(
            a.our_side, _loss_literals(a) or {}, b.our_side, _loss_literals(b) or {}
        )

    def test_yes_superset_is_NOT_certified(self) -> None:
        # YES on {A,B} can miss via B while NO on {A} still hits. Both can lose.
        a = _unit("a", (LEGS_A[0],), side=Side.NO)
        b = _unit("b", LEGS_A, side=Side.YES)
        assert not _certified_cannot_both_lose(
            a.our_side, _loss_literals(a) or {}, b.our_side, _loss_literals(b) or {}
        )

    def test_two_nos_needing_opposite_sides_of_one_market_are_certified(self) -> None:
        a = _unit("a", (_leg("MKT-A", "KX-G1", "yes"),), side=Side.NO)
        b = _unit("b", (_leg("MKT-A", "KX-G1", "no"),), side=Side.NO)
        assert _certified_cannot_both_lose(
            a.our_side, _loss_literals(a) or {}, b.our_side, _loss_literals(b) or {}
        )

    def test_two_nos_on_disjoint_markets_are_not_certified(self) -> None:
        a = _unit("a", (_leg("MKT-A", "KX-G1"),), side=Side.NO)
        b = _unit("b", (_leg("MKT-B", "KX-G2"),), side=Side.NO)
        assert not _certified_cannot_both_lose(
            a.our_side, _loss_literals(a) or {}, b.our_side, _loss_literals(b) or {}
        )

    def test_two_yes_are_never_certified(self) -> None:
        a = _unit("a", LEGS_A, side=Side.YES)
        b = _unit("b", LEGS_A, side=Side.YES)
        assert not _certified_cannot_both_lose(
            a.our_side, _loss_literals(a) or {}, b.our_side, _loss_literals(b) or {}
        )

    def test_unknown_leg_side_is_uncertifiable(self) -> None:
        bad = _unit("a", (LegRef("MKT-A", "KX-G1", "unknown"),), side=Side.NO)
        assert _loss_literals(bad) is None

    def test_reserved_holding_is_uncertifiable(self) -> None:
        assert _loss_literals(_unit("a", LEGS_A, risk_modeled=False)) is None

    def test_self_contradictory_leg_set_is_uncertifiable(self) -> None:
        bad = _unit(
            "a",
            (_leg("MKT-A", "KX-G1", "yes"), _leg("MKT-A", "KX-G1", "no")),
            side=Side.NO,
        )
        assert _loss_literals(bad) is None

    def test_empty_leg_set_is_uncertifiable(self) -> None:
        assert _loss_literals(_unit("a", ())) is None


# --------------------------------------------------------------------------
# FIX 3 — the fold charges a certified pair ONCE, an ambiguous pair TWICE
# --------------------------------------------------------------------------


class TestFoldChargesPairOnce:
    def test_certified_pair_charged_once_not_twice(self) -> None:
        """THE HEADLINE INVARIANT: two positions that cannot both lose are
        charged max(a, b), not a + b."""
        a = _unit("a", LEGS_A, side=Side.NO, price_cc=6_000)   # loss 600_000
        b = _unit("b", LEGS_A, side=Side.YES, price_cc=4_000)  # loss 400_000
        bound, credit = mutex_aware_det_max_and_credit([a, b])
        assert bound == 1_000_000.0            # comonotone: both charged full
        assert credit == 400_000.0             # the SMALLER of the pair
        assert bound - credit == 600_000.0     # == max(a, b): charged ONCE

    def test_ambiguous_pair_charged_in_full(self) -> None:
        """An UNCERTIFIED pair (two NOs on disjoint markets — both can lose)
        earns zero credit: fail toward the LARGER worst case."""
        a = _unit("a", (_leg("MKT-A", "KX-G1"),), side=Side.NO)
        b = _unit("b", (_leg("MKT-B", "KX-G2"),), side=Side.NO)
        bound, credit = mutex_aware_det_max_and_credit([a, b])
        assert credit == 0.0
        assert bound == a.loss_cc + b.loss_cc

    def test_unknown_side_pair_charged_in_full(self) -> None:
        a = _unit("a", LEGS_A, side=Side.NO)
        b = DetMaxUnit(
            unit_id="b",
            our_side=Side.YES,
            contracts_centi=10_000,
            entry_price_cc=5_000,
            legs=(LegRef("MKT-A", "KX-G1", "unknown"), LEGS_A[1]),
            loss_cc=500_000.0,
        )
        _bound, credit = mutex_aware_det_max_and_credit([a, b])
        assert credit == 0.0

    def test_reserved_complement_charged_in_full(self) -> None:
        a = _unit("a", LEGS_A, side=Side.NO)
        b = _unit("b", LEGS_A, side=Side.YES, risk_modeled=False)
        _bound, credit = mutex_aware_det_max_and_credit([a, b])
        assert credit == 0.0

    def test_matching_is_disjoint_never_a_clique(self) -> None:
        """One NO and TWO complements: only ONE pairing may be credited (a unit
        may appear in at most one pair), and the greedy takes the larger."""
        a = _unit("a", LEGS_A, side=Side.NO, price_cc=9_000)
        b = _unit("b", LEGS_A, side=Side.YES, price_cc=3_000)
        c = _unit("c", LEGS_A, side=Side.YES, price_cc=1_000)
        credit = _hedge_offset_credit_cc([a, b, c])
        assert credit == 300_000.0   # min(a,b) only — c stays charged in full

    def test_credit_never_exceeds_the_bound(self) -> None:
        a = _unit("a", LEGS_A, side=Side.NO)
        b = _unit("b", LEGS_A, side=Side.YES)
        bound, credit = mutex_aware_det_max_and_credit([a, b])
        assert 0.0 <= credit <= bound

    def test_credited_bound_never_below_largest_single_loss(self) -> None:
        """Soundness floor: the book can always lose at least its largest single
        position, so no matching may credit below that."""
        a = _unit("a", LEGS_A, side=Side.NO, price_cc=7_000)
        b = _unit("b", LEGS_A, side=Side.YES, price_cc=7_000)
        c = _unit("c", (_leg("MKT-Z", "KX-G9"),), side=Side.NO, price_cc=2_000)
        bound, credit = mutex_aware_det_max_and_credit([a, b, c])
        assert bound - credit >= max(a.loss_cc, b.loss_cc, c.loss_cc)

    def test_reserved_premium_is_never_credited(self) -> None:
        a = _unit("a", LEGS_A, side=Side.NO)
        b = _unit("b", LEGS_A, side=Side.YES)
        bound, credit = mutex_aware_det_max_and_credit(
            [a, b], reserved_loss_cc=250_000.0
        )
        assert bound == a.loss_cc + b.loss_cc + 250_000.0
        assert bound - credit >= 250_000.0

    def test_zero_loss_unit_never_earns_credit(self) -> None:
        """CROSS-FIX SAFETY (FIX 2 + FIX 3 armed together). FIX 2 zeroes the
        ``loss_cc`` of a position the exchange has already proved cannot lose,
        so that unit must earn NO hedge credit — otherwise the same dollar
        would be credited on both axes."""
        settled = _unit("settled", LEGS_A, side=Side.NO, price_cc=6_000)
        settled = DetMaxUnit(
            unit_id=settled.unit_id, our_side=settled.our_side,
            contracts_centi=settled.contracts_centi,
            entry_price_cc=settled.entry_price_cc, legs=settled.legs,
            loss_cc=0.0, risk_modeled=True,
        )
        live = _unit("live", LEGS_A, side=Side.YES, price_cc=4_000)
        _bound, credit = mutex_aware_det_max_and_credit([settled, live])
        assert credit == 0.0

    def test_pair_budget_truncation_only_lowers_credit(self) -> None:
        """The latency bound can only find FEWER pairs ⇒ LESS credit ⇒ a LARGER
        charged number. Never a soundness hole."""
        import combomaker.sim.book_risk as br

        units = [
            _unit(f"n{i}", LEGS_A, side=Side.NO, price_cc=5_000)
            for i in range(6)
        ] + [
            _unit(f"y{i}", LEGS_A, side=Side.YES, price_cc=5_000)
            for i in range(6)
        ]
        full = _hedge_offset_credit_cc(units)
        prior = br._HEDGE_PAIR_BUDGET
        try:
            br._HEDGE_PAIR_BUDGET = 1
            truncated = _hedge_offset_credit_cc(units)
        finally:
            br._HEDGE_PAIR_BUDGET = prior
        assert full > 0.0
        assert truncated <= full

    def test_legacy_entry_point_is_byte_identical(self) -> None:
        """``mutex_aware_det_max_from_units`` must keep returning the UNCREDITED
        bound — the credit is opt-in at the gate, never pre-subtracted."""
        a = _unit("a", LEGS_A, side=Side.NO)
        b = _unit("b", LEGS_A, side=Side.YES)
        legacy = mutex_aware_det_max_from_units([a, b])
        bound, credit = mutex_aware_det_max_and_credit([a, b])
        assert legacy == bound
        assert credit > 0.0


# --------------------------------------------------------------------------
# FIX 3 — arming: shadow is byte-identical, armed subtracts, nothing else moves
# --------------------------------------------------------------------------


def _pos(
    pid: str, legs: tuple[LegRef, ...], *, side: Side, price_cc: int
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=side,
        contracts=CentiContracts(10_000),
        entry_price_cc=CentiCents(price_cc),
        legs=legs,
    )


class _FakeBookRisk:
    """Minimal ``PortfolioRisk`` stand-in for the quote-time det-max cap."""

    def __init__(self, det: float, mutex: float | None, credit: float) -> None:
        self.usable = True
        self.unknown = False
        self.n_positions = 2
        self.governing_model_es_99_cc = 0.0
        self.deterministic_max_loss_cc = det
        self.mutex_aware_det_max_cc = mutex
        self.det_max_hedge_credit_cc = credit
        self.p_ruin = 0.0
        self.p_ruin_upper = 0.0
        self.loss_quantiles_cc: tuple[float, ...] = ()
        self.per_game_tail_cc: tuple[object, ...] = ()
        self.per_leg_tail_cc: tuple[object, ...] = ()


class TestQuoteTimeArming:
    """The quote-time SKIP_PORTFOLIO_DET_MAX cap: shadow measures, armed
    subtracts, and NO other cap moves either way."""

    def _breaches(self, *, armed: bool, credit: float) -> list[Breach]:
        from combomaker.risk.exposure import ExposureBook
        from tests.test_lifecycle import TEST_CONVENTIONS

        limits = RiskLimits(
            portfolio_det_max_frac=Fraction(10, 100),
            det_max_hedge_credit=armed,
        )
        book = ExposureBook(TEST_CONVENTIONS)
        return LimitChecker(limits).check(
            book,
            lambda t: 0.5,
            DailyPnl(),
            risk_bankroll_cc=1_000_000,
            bankroll_source_configured=True,
            book_risk=_FakeBookRisk(150_000.0, 150_000.0, credit),
        )

    def _det_breaches(self, breaches: list[Breach]) -> list[Breach]:
        return [
            b for b in breaches if b.reason is ReasonCode.SKIP_PORTFOLIO_DET_MAX
        ]

    def test_shadow_is_byte_identical(self) -> None:
        """A credit big enough to clear the wall changes NOTHING while
        disarmed — the breach fires exactly as with zero credit."""
        with_credit = self._breaches(armed=False, credit=80_000.0)
        no_credit = self._breaches(armed=False, credit=0.0)
        assert len(self._det_breaches(with_credit)) == 1
        assert len(self._det_breaches(no_credit)) == 1
        # Same reasons, same enforcement, same count across the WHOLE set.
        assert [(b.reason, b.shadow) for b in with_credit] == [
            (b.reason, b.shadow) for b in no_credit
        ]

    def test_armed_credit_clears_the_det_wall(self) -> None:
        # 150_000 bound − 80_000 credit = 70_000 <= 10% of 1_000_000.
        assert not self._det_breaches(self._breaches(armed=True, credit=80_000.0))

    def test_armed_insufficient_credit_still_breaches(self) -> None:
        assert self._det_breaches(self._breaches(armed=True, credit=10_000.0))

    def test_armed_credit_cannot_drive_the_gate_negative(self) -> None:
        # An absurd credit is clamped at the bound, never below zero.
        out = self._breaches(armed=True, credit=10**9)
        assert not self._det_breaches(out)

    def test_other_axes_byte_identical_across_arming(self) -> None:
        """PER-COMBO / ENTITY / SLATE / KILL-TAIL protections must not move.
        Compare the FULL breach set minus the det-max axis."""
        armed = self._breaches(armed=True, credit=80_000.0)
        shadow = self._breaches(armed=False, credit=80_000.0)
        strip = lambda bs: [  # noqa: E731 - local comparator
            (b.reason, b.shadow)
            for b in bs
            if b.reason is not ReasonCode.SKIP_PORTFOLIO_DET_MAX
        ]
        assert strip(armed) == strip(shadow)

    def test_no_mutex_bound_means_no_credit_even_when_armed(self) -> None:
        """A snapshot with no mutex-aware bound (the fold never ran) gates
        comonotone and takes NO credit — fail closed."""
        from combomaker.risk.exposure import ExposureBook
        from tests.test_lifecycle import TEST_CONVENTIONS

        limits = RiskLimits(
            portfolio_det_max_frac=Fraction(10, 100), det_max_hedge_credit=True
        )
        out = LimitChecker(limits).check(
            ExposureBook(TEST_CONVENTIONS),
            lambda t: 0.5,
            DailyPnl(),
            risk_bankroll_cc=1_000_000,
            bankroll_source_configured=True,
            book_risk=_FakeBookRisk(150_000.0, None, 80_000.0),
        )
        assert self._det_breaches(out)


class TestCandidateGateArming:
    """The confirm-path candidate gate arms/disarms on the SAME knob (a looser
    gate than cap is the renege zone)."""

    def _run(self, *, armed: bool):
        from combomaker.sim.book_risk import evaluate_candidate_book_risk

        held = _pos("held", LEGS_A, side=Side.NO, price_cc=6_000)
        # The complement of the held combo: cannot lose when the held one does.
        cand = _pos("cand", LEGS_A, side=Side.YES, price_cc=1_000)
        return evaluate_candidate_book_risk(
            [held],
            cand,
            marginals=lambda t: 0.5,
            n_samples=4_000,
            seed=11,
            bankroll_cc=1_000_000,
            portfolio_det_max_frac=0.65,
            det_max_hedge_credit=armed,
        )

    def test_credit_is_measured_in_both_modes(self) -> None:
        assert self._run(armed=False).post.det_max_hedge_credit_cc > 0.0
        assert self._run(armed=True).post.det_max_hedge_credit_cc > 0.0

    def test_shadow_declines_and_armed_admits_the_complement(self) -> None:
        # POST det = 600_000 + 100_000 = 700_000 > 65% of 1_000_000 = 650_000.
        # The complement's 100_000 credit brings the charged number to 600_000.
        shadow = self._run(armed=False)
        armed = self._run(armed=True)
        assert shadow.decline_reason == "post_deterministic_max_over_budget"
        assert armed.decline_reason != "post_deterministic_max_over_budget"


# --------------------------------------------------------------------------
# FIX 4 — value-ranked allocation of the fixed det-max budget
# --------------------------------------------------------------------------


def _resting(
    qid: str, edge_cc: int | None, *, no_bid_cc: int, contracts: int = 10_000
) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=qid,
        rfq_id=f"r-{qid}",
        combo_ticker=f"C-{qid}",
        collection=None,
        yes_bid_cc=CentiCents(0),
        no_bid_cc=CentiCents(no_bid_cc),
        contracts=CentiContracts(contracts),
        legs=(_leg(f"L-{qid}", f"KX-G-{qid}"),),
        expected_edge_cc=edge_cc,
    )


def _cand_quote(no_bid_cc: int, fair_cc: int) -> ConstructedQuote:
    return ConstructedQuote(
        yes_bid_cc=CentiCents(0),
        no_bid_cc=CentiCents(no_bid_cc),
        fair_cc=CentiCents(fair_cc),
        width_components_cc={},
    )


async def _rig(tmp_path: Path, mode: str):
    lc = await _lifecycle(
        tmp_path, LifecycleConfig(det_budget_value_ranking=mode)
    )
    lc._limits = LimitChecker(  # noqa: SLF001 - test rig wiring
        RiskLimits(caps_shadow_mode=True, per_combo_loss_frac=Fraction(99, 100))
    )
    lc._marginals = lambda t: 0.5  # noqa: SLF001
    return lc


def _det_breach() -> list[Breach]:
    return [
        Breach(ReasonCode.SKIP_PORTFOLIO_DET_MAX, "det budget", shadow=False)
    ]


class TestDetBudgetValueRanking:
    async def _attempt(self, lc, *, no_bid_cc: int, fair_cc: int, contracts: int):
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rc", market_ticker="KXMVE-C", contracts_fp="100.00",
        )
        result = _cand_quote(no_bid_cc, fair_cc)
        qty = CentiContracts(contracts)
        qrisk = lc._quote_risk(rfq, result, quote_id="pending", qty=qty)  # noqa: SLF001
        return await lc._try_slot_eviction(  # noqa: SLF001
            rfq, result, qty, qrisk, _det_breach()
        )

    async def test_density_ranking_evicts_the_least_dense(
        self, tmp_path: Path
    ) -> None:
        lc = await _rig(tmp_path, "on")
        # BIGGEST absolute EV but heavy det consumption:
        # 20_000cc EV / 900_000 det = density 0.022.
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 20_000, no_bid_cc=9_000)
        )
        # SMALLER absolute EV but cheap: 5_000cc / 100_000 det = 0.05 —
        # more than TWICE as dense as "fat" despite a quarter of the EV.
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("lean", 5_000, no_bid_cc=1_000)
        )
        # Candidate: NO at 1_000 on fair 3_000 => NO-side fair 7_000 => edge
        # 600_000cc over 100_000 of det — denser than both.
        await self._attempt(lc, no_bid_cc=1_000, fair_cc=3_000, contracts=10_000)
        # The FAT quote goes even though its ABSOLUTE EV is the largest —
        # arrival order and raw EV both had it wrong.
        assert "fat" not in lc._exposure.open_quotes  # noqa: SLF001
        assert "lean" in lc._exposure.open_quotes  # noqa: SLF001

    async def test_shadow_mode_evicts_nothing(self, tmp_path: Path) -> None:
        lc = await _rig(tmp_path, "shadow")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 9_000, no_bid_cc=9_000)
        )
        out = await self._attempt(
            lc, no_bid_cc=1_000, fair_cc=3_000, contracts=10_000
        )
        assert "fat" in lc._exposure.open_quotes  # noqa: SLF001
        # Breaches returned UNTOUCHED — the decision path is byte-identical.
        assert [(b.reason, b.shadow) for b in out] == [
            (b.reason, b.shadow) for b in _det_breach()
        ]

    async def test_off_mode_never_runs(self, tmp_path: Path) -> None:
        lc = await _rig(tmp_path, "off")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 9_000, no_bid_cc=9_000)
        )
        out = await self._attempt(
            lc, no_bid_cc=1_000, fair_cc=3_000, contracts=10_000
        )
        assert "fat" in lc._exposure.open_quotes  # noqa: SLF001
        assert len(out) == 1

    async def test_less_dense_candidate_does_not_evict(
        self, tmp_path: Path
    ) -> None:
        lc = await _rig(tmp_path, "on")
        # 9_000cc EV over 10_000 of det = density 0.9.
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("dense", 9_000, no_bid_cc=100)
        )
        # Candidate: NO at 9_000 on fair 0 => edge 100_000cc over 900_000 of
        # det = density 0.111, far BELOW the resting quote's 0.9.
        await self._attempt(lc, no_bid_cc=9_000, fair_cc=0, contracts=10_000)
        assert "dense" in lc._exposure.open_quotes  # noqa: SLF001

    async def test_unknown_ev_quote_is_never_the_loser(
        self, tmp_path: Path
    ) -> None:
        lc = await _rig(tmp_path, "on")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("unknown_ev", None, no_bid_cc=9_000)
        )
        await self._attempt(lc, no_bid_cc=1_000, fair_cc=3_000, contracts=10_000)
        assert "unknown_ev" in lc._exposure.open_quotes  # noqa: SLF001

    async def test_never_fires_when_another_enforced_wall_is_up(
        self, tmp_path: Path
    ) -> None:
        """It decides WHICH candidates get a fixed budget, never whether the
        budget exists: a second enforced wall disables the mechanism outright."""
        lc = await _rig(tmp_path, "on")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 9_000, no_bid_cc=9_000)
        )
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rc2", market_ticker="KXMVE-C2", contracts_fp="100.00",
        )
        result = _cand_quote(1_000, 3_000)
        qty = CentiContracts(10_000)
        qrisk = lc._quote_risk(rfq, result, quote_id="pending", qty=qty)  # noqa: SLF001
        mixed = [
            *_det_breach(),
            Breach(ReasonCode.SKIP_PER_COMBO_LOSS_CAP, "per-combo", shadow=False),
        ]
        out = await lc._try_slot_eviction(rfq, result, qty, qrisk, mixed)  # noqa: SLF001
        assert "fat" in lc._exposure.open_quotes  # noqa: SLF001
        assert len(out) == 2

    async def test_value_ranking_never_admits_a_cap_refused_candidate(
        self, tmp_path: Path
    ) -> None:
        """After the eviction the SAME check re-runs. With a real enforced
        per-combo cap the candidate still cannot pass — the eviction bought a
        reordering, never an admission."""
        lc = await _rig(tmp_path, "on")
        lc._limits = LimitChecker(  # noqa: SLF001
            RiskLimits(
                caps_shadow_mode=False,
                max_notional_per_quote_dollars=Fraction(1, 100),
            )
        )
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("fat", 9_000, no_bid_cc=9_000)
        )
        out = await self._attempt(
            lc, no_bid_cc=1_000, fair_cc=3_000, contracts=10_000
        )
        # The eviction happened (the det axis was the only breach we passed in)
        # but the re-check REFUSES the candidate on a different enforced cap.
        assert [b for b in out if not b.shadow], "cap-refused candidate admitted"

    async def test_eviction_cannot_thrash(self, tmp_path: Path) -> None:
        """A quote evicted at density d may not evict its evictor back out."""
        lc = await _rig(tmp_path, "on")
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("victim", 500, no_bid_cc=9_000)
        )
        # Round 1: the dense candidate evicts "victim" (combo_ticker C-victim).
        await self._attempt(lc, no_bid_cc=1_000, fair_cc=3_000, contracts=10_000)
        assert "victim" not in lc._exposure.open_quotes  # noqa: SLF001
        winner_density = lc._evicted_density["C-victim"]  # noqa: SLF001
        assert winner_density > 0.0
        # Round 2: the winner is now resting; "victim" comes back at its ORIGINAL
        # (lower) density and must NOT be able to evict it.
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("winner", 6_000, no_bid_cc=1_000)
        )
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rv", market_ticker="C-victim", contracts_fp="100.00",
        )
        result = _cand_quote(9_000, 0)  # edge 1_000cc / 900_000 det
        qty = CentiContracts(10_000)
        qrisk = lc._quote_risk(rfq, result, quote_id="pending", qty=qty)  # noqa: SLF001
        assert qrisk.combo_ticker == "C-victim"
        await lc._try_slot_eviction(rfq, result, qty, qrisk, _det_breach())  # noqa: SLF001
        assert "winner" in lc._exposure.open_quotes  # noqa: SLF001

    async def test_thrash_block_is_density_based_not_permanent(
        self, tmp_path: Path
    ) -> None:
        """A genuinely repriced (denser) quote on an evicted combo CAN evict —
        the ledger blocks the ping-pong, not the combo."""
        lc = await _rig(tmp_path, "on")
        lc._evicted_density["C-victim"] = 0.001  # noqa: SLF001
        lc._exposure.upsert_quote(  # noqa: SLF001
            _resting("weak", 100, no_bid_cc=9_000)
        )
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rv2", market_ticker="C-victim", contracts_fp="100.00",
        )
        result = _cand_quote(1_000, 3_000)  # density 0.06 > 0.001
        qty = CentiContracts(10_000)
        qrisk = lc._quote_risk(rfq, result, quote_id="pending", qty=qty)  # noqa: SLF001
        await lc._try_slot_eviction(rfq, result, qty, qrisk, _det_breach())  # noqa: SLF001
        assert "weak" not in lc._exposure.open_quotes  # noqa: SLF001

    async def test_slot_axis_still_ranks_on_raw_ev(self, tmp_path: Path) -> None:
        """The 2026-07-25 slot axis is UNCHANGED: it ranks on absolute EV, and
        it is gated by its own flag, not the det one."""
        lc = await _lifecycle(
            tmp_path,
            LifecycleConfig(
                open_quote_ev_eviction=True, det_budget_value_ranking="off"
            ),
        )
        lc._limits = LimitChecker(  # noqa: SLF001
            RiskLimits(
                max_open_quotes=2,
                caps_shadow_mode=True,
                per_combo_loss_frac=Fraction(99, 100),
            )
        )
        lc._marginals = lambda t: 0.5  # noqa: SLF001
        # "cheap" is DENSER (500/100_000) but has the smaller absolute EV, so
        # the slot axis must still evict IT, not the fat quote.
        lc._exposure.upsert_quote(_resting("cheap", 500, no_bid_cc=1_000))  # noqa: SLF001
        lc._exposure.upsert_quote(_resting("fat", 9_000, no_bid_cc=9_000))  # noqa: SLF001
        rfq = combo(
            [{"market_ticker": "A", "side": "yes", "event_ticker": "KX-G1"}],
            id="rs", market_ticker="KXMVE-S", contracts_fp="100.00",
        )
        result = _cand_quote(1_000, 3_000)
        qty = CentiContracts(10_000)
        qrisk = lc._quote_risk(rfq, result, quote_id="pending", qty=qty)  # noqa: SLF001
        await lc._try_slot_eviction(  # noqa: SLF001
            rfq, result, qty, qrisk,
            [Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "at cap", shadow=False)],
        )
        assert "cheap" not in lc._exposure.open_quotes  # noqa: SLF001
        assert "fat" in lc._exposure.open_quotes  # noqa: SLF001


class TestDetConsumptionArithmetic:
    def test_matches_the_det_axis_arithmetic(self) -> None:
        from combomaker.rfq.lifecycle import QuoteLifecycle

        q = _resting("q", 100, no_bid_cc=3_333, contracts=777)
        assert QuoteLifecycle._quote_det_consumed_cc(q) == 777 * 3_333 // 100

    def test_two_sided_quote_takes_the_worse_side(self) -> None:
        from combomaker.rfq.lifecycle import QuoteLifecycle

        q = OpenQuoteRisk(
            quote_id="q", rfq_id="r", combo_ticker="C", collection=None,
            yes_bid_cc=CentiCents(2_000), no_bid_cc=CentiCents(7_000),
            contracts=CentiContracts(10_000), legs=(_leg("L"),),
            expected_edge_cc=100,
        )
        assert QuoteLifecycle._quote_det_consumed_cc(q) == 10_000 * 7_000 // 100

    def test_zero_consumption_has_no_density(self) -> None:
        from combomaker.rfq.lifecycle import QuoteLifecycle

        assert QuoteLifecycle._value_density(100, 0) is None
        assert QuoteLifecycle._value_density(100, -5) is None
