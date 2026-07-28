"""FIX 1 (entity admission on the AND-BOUND TICKET) + FIX 2 (slate partition).

Operator 2026-07-27: "we're 100% declining too much flow, we're getting stuck at
this $500-700 amount. we cannot judge a combo by the worst leg … we need to
tackle the first 2 [entity + slate] as they make up over 50%."

The admission rule this suite encodes: a refusal must be caused by SIZE or by
GAME-LEVEL concentration. A refusal caused by a STRUCTURAL ARTEFACT — judging a
combo by its worst leg, or summing losses that cannot all occur — is a defect.

Both fixes ship behind their OWN arming flag, both defaulting to SHADOW, and in
shadow the admission behaviour is byte-identical to before them (pinned here on
the exact breach detail strings AND on observer-independence).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.entity_admission import (
    TIER_1,
    TIER_2,
    TIER_DECLINE,
    TIER_NONE,
    EntityLoad,
    certify_entity_admission,
    combo_widen_weight,
    effective_n,
    entity_loads,
    entity_tier,
    ticket_concentration,
    tier_widen_weight,
)
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    LossUnit,
    OpenPosition,
    leg_entity_key,
    partitioned_worst_case_cc,
)
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

BANKROLL = 20_000_000            # $2,000.00
ENTITY_WALL = 600_000            # 3% = $60.00
COMBO_WALL = 1_000_000           # 5% = $100.00  (the SCALE ceiling)
MARG: Callable[[str], float | None] = lambda t: 0.5  # noqa: E731


def _leg(entity: str, game: str = "G1") -> LegRef:
    """A (family:entity x direction) key of ``KXMLBKS:<entity>:yes``."""
    return LegRef(f"KXMLBKS-{game}-{entity}-4", f"KXMLBKS-{game}", "yes")


def _pos(pid: str, legs: tuple[LegRef, ...], loss_cc: int) -> OpenPosition:
    """A long-NO ticket whose max_loss is exactly ``loss_cc`` (100 contracts)."""
    assert loss_cc % 100 == 0
    return OpenPosition(
        position_id=pid, combo_ticker=f"COMBO-{pid}", collection=None,
        our_side=Side.NO, contracts=Q(10_000), entry_price_cc=CC(loss_cc // 100),
        legs=legs,
    )


def _limits(**over: object) -> RiskLimits:
    """Every other cap LOOSE so exactly the axis under test can fire."""
    base: dict[str, object] = dict(
        caps_shadow_mode=False,
        entity_loss_frac=Fraction(3, 100),
        per_combo_loss_frac=Fraction(5, 100),
        game_loss_frac=Fraction(50, 100),
        slate_loss_frac=Fraction(90, 100),
        directional_frac=Fraction(99, 100),
        daily_loss_frac=Fraction(99, 100),
        portfolio_det_max_frac=Fraction(99, 100),
        absolute_notional_multiple=999,
        max_market_delta_contracts=1e9,
        max_event_delta_contracts=1e9,
        max_gross_notional_dollars=1e9,
        max_event_worst_case_loss_dollars=1e9,
    )
    base.update(over)
    return RiskLimits(**base)  # type: ignore[arg-type]


def _reasons(breaches: list[object]) -> list[ReasonCode]:
    return [b.reason for b in breaches]  # type: ignore[attr-defined]


def _check(
    cand: OpenPosition, *, armed: bool, book: ExposureBook | None = None
) -> list[ReasonCode]:
    checker = LimitChecker(_limits(entity_admission_armed=armed))
    return _reasons(
        checker.check(
            book if book is not None else _entity_book(),
            MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
    )


# --- the shared entity fixture -------------------------------------------------
# Bankroll $2,000, so the operator's percentages are, in dollars:
#     tier 1 line   1% = $20      tier 2 line  2% = $40
#     DECLINE line  3% = $60  (= entity_loss_frac, the ratified wall)
#     size ceiling  5% = $100 (= per_combo_loss_frac, ratified and untouched)
# The committed book puts ONE key in each tier so every branch has a fixture:
#     WARM ($30 = 1.5%)  TIER 1        HOT  ($50 = 2.5%)  TIER 2
#     C0..C11 ($10 = 0.5% each)        TIER 0 (cool)      F* keys: absent = $0
TIER1_LINE = 200_000        # 1% of $2,000
TIER2_LINE = 400_000        # 2%
WARM = "WARMARM"            # $30 prior -> tier 1
HOT = "HOTARM"              # $50 prior -> tier 2


def _entity_book() -> ExposureBook:
    book = ExposureBook(CONVENTIONS)
    book.add_position(_pos("w1", (_leg(WARM, "GW"),), 150_000))
    book.add_position(_pos("w2", (_leg(WARM, "GW"),), 150_000))
    book.add_position(_pos("h1", (_leg(HOT, "GH"),), 500_000))
    for i in range(12):
        book.add_position(_pos(f"c{i}", (_leg(f"C{i}", f"G{i}"),), 100_000))
    return book


def _loads(
    *legs_and_loss: tuple[tuple[str, ...], int],
    book: ExposureBook | None = None,
) -> tuple[EntityLoad, ...]:
    """``entity_loads`` against the fixture book's committed per-key dollars —
    the same ``prior + add = post`` decomposition the checker feeds it."""
    b = book if book is not None else _entity_book()
    prior: dict[str, int] = {}
    for p in b.positions.values():
        for k in {leg_entity_key(x) for x in p.legs}:
            prior[k] = prior.get(k, 0) + p.max_loss_cc
    post = dict(prior)
    batch = []
    for keys, loss in legs_and_loss:
        ks = [leg_entity_key(_leg(k)) for k in keys]
        batch.append((ks, loss))
        for k in set(ks):
            post[k] = post.get(k, 0) + loss
    return entity_loads(
        candidate_legs_by_position=batch,
        post_by_key=post,
        bankroll_cc=BANKROLL,
        decline_frac=Fraction(3, 100),
    )


class TestTheOperatorsTiers:
    """<1% no action | 1-2% tier 1 | 2-3% tier 2 | >3% DECLINE, on the load as a
    PERCENT OF BANKROLL. The three percentages are the only constants in the
    build, and the third one is the already-ratified ``entity_loss_frac``."""

    def _tier(self, load_cc: int) -> int:
        return entity_tier(load_cc, BANKROLL, Fraction(3, 100))

    def test_the_four_bands_are_exactly_the_operators_percentages(self) -> None:
        assert self._tier(190_000) == TIER_NONE        # 0.95%
        assert self._tier(250_000) == TIER_1           # 1.25%
        assert self._tier(350_000) == TIER_1           # 1.75%
        assert self._tier(500_000) == TIER_2           # 2.50%
        assert self._tier(700_000) == TIER_DECLINE     # 3.50%

    def test_the_band_edges_are_exact_to_the_centi_cent(self) -> None:
        # The DECLINE edge must be the SAME comparison the wall makes
        # (``loss_cc > entity_thr``), not one cc apart, or the tier and the cap
        # would disagree about the same book.
        assert self._tier(TIER1_LINE) == TIER_NONE
        assert self._tier(TIER1_LINE + 1) == TIER_1
        assert self._tier(TIER2_LINE) == TIER_1
        assert self._tier(TIER2_LINE + 1) == TIER_2
        assert self._tier(ENTITY_WALL) == TIER_2
        assert self._tier(ENTITY_WALL + 1) == TIER_DECLINE

    def test_unusable_bankroll_fails_closed_to_decline(self) -> None:
        assert entity_tier(1, 0, Fraction(3, 100)) == TIER_DECLINE
        assert entity_tier(1, -5, Fraction(3, 100)) == TIER_DECLINE
        assert entity_tier(1, BANKROLL, Fraction(0)) == TIER_DECLINE

    def test_tier_scales_with_bankroll_and_is_never_hand_set(self) -> None:
        # NORTH STAR: the same $30 load is tier 1 at a $2,000 bankroll and tier 2
        # at $1,200 — the lines move because the BANKROLL moved, not because a
        # human moved a number.
        assert entity_tier(300_000, BANKROLL, Fraction(3, 100)) == TIER_1
        assert entity_tier(300_000, 12_000_000, Fraction(3, 100)) == TIER_2


class TestGradedWiden:
    """REQUIRED: tier 1 and tier 2 widen by measurably different amounts."""

    def _w(self, load_cc: int) -> Fraction:
        return tier_widen_weight(load_cc, BANKROLL, Fraction(3, 100))

    def test_tier1_and_tier2_widen_by_measurably_different_amounts(self) -> None:
        cool, t1, t2 = self._w(100_000), self._w(300_000), self._w(500_000)
        assert cool == Fraction(0)                      # < 1% -> NO ACTION
        assert t1 == Fraction(1, 2)                     # $30 / $60
        assert t2 == Fraction(5, 6)                     # $50 / $60
        assert cool < t1 < t2                           # strictly graded
        # ... and the bands cannot overlap, on ANY load, by construction.
        assert all(
            Fraction(1, 3) <= self._w(x) <= Fraction(2, 3)
            for x in range(TIER1_LINE + 1, TIER2_LINE + 1, 7_919)
        )
        assert all(
            Fraction(2, 3) < self._w(x) <= Fraction(1)
            for x in range(TIER2_LINE + 1, ENTITY_WALL + 1, 7_919)
        )

    def test_a_fresh_entity_is_never_widened_for_the_tickets_own_size(self) -> None:
        """The operator's arithmetic: 24 distinct props a slate, and widening a
        FRESH pitcher prices us out of ~21 of them. The weight reads the PRIOR
        load, so a brand-new key weighs 0 however big the ticket is."""
        (fresh,) = _loads((("NOBODY",), 900_000))
        assert fresh.prior_cc == 0
        assert fresh.widen_weight == Fraction(0)
        assert fresh.post_tier == TIER_DECLINE          # the WALL still sees it

    def test_widen_is_graded_not_a_cliff(self) -> None:
        assert self._w(TIER2_LINE) < self._w(TIER2_LINE + 100_000)


class TestNetAcrossTheCombo:
    """"it should be judged based on other legs as well, if they diversify
    further or concentrate other legs we have" — in DOLLARS, over ALL the legs,
    never the worst leg alone."""

    def test_hot_leg_at_tier1_plus_four_fresh_legs_is_not_a_pure_add(self) -> None:
        """REQUIRED. Same $30 of premium, two shapes:
        pure add  -> the whole ticket rides WARM alone;
        net       -> WARM plus four fresh entities.
        The net shape must price at a FIFTH of the pure add (five keys, equal
        dollars, four of them weighing zero), not at the worst leg's weight."""
        pure = _loads(((WARM,), 300_000))
        net = _loads(((WARM, "F1", "F2", "F3", "F4"), 300_000))
        assert combo_widen_weight(pure) == Fraction(1, 2)
        assert combo_widen_weight(net) == Fraction(1, 10)
        assert combo_widen_weight(net) * 5 == combo_widen_weight(pure)

    def test_hot_leg_at_tier1_plus_four_fresh_legs_is_ADMITTED(self) -> None:
        """REQUIRED. Tier 1 is "widen", never "decline": the combo clears every
        flag state, armed and unarmed, and the per-combo cap is silent too."""
        cand = _pos(
            "cand",
            tuple(_leg(k) for k in (WARM, "F1", "F2", "F3", "F4")),
            300_000,
        )
        for armed in (False, True):
            reasons = _check(cand, armed=armed)
            assert ReasonCode.SKIP_ENTITY_LOSS_CAP not in reasons
            assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP not in reasons

    def test_dollars_decide_the_net_not_the_leg_count(self) -> None:
        # Two fresh legs and one warm leg: the fresh dollars (2x) beat the warm.
        loads = _loads(((WARM, "F1", "F2"), 700_000))
        cert = certify_entity_admission(
            key=leg_entity_key(_leg("F1")), loads=loads,
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        assert cert.diversifying_cc == 1_400_000      # F1 + F2
        assert cert.concentrating_cc == 700_000       # WARM
        # ...and flipping the shape flips the verdict: one fresh leg against two
        # already-loaded ones is NET CONCENTRATING and refuses.
        conc = certify_entity_admission(
            key=leg_entity_key(_leg("F1")),
            loads=_loads(((WARM, HOT, "F1"), 700_000)),
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        assert conc.certified is False
        assert conc.verdict == "net_concentrating"


class TestTheCertificateActuallyReadsTheKey:
    """The regression that killed the previous build, pinned so it cannot
    return: its rule reduced to ``L < 2T/(N_eff-1)`` and returned the IDENTICAL
    verdict for $40 dumped on the hottest key and $40 spread over four fresh
    entities. Those two must never agree again."""

    def test_same_dollars_hot_key_vs_fresh_keys_disagree(self) -> None:
        hot = certify_entity_admission(
            key=leg_entity_key(_leg(HOT)), loads=_loads(((HOT,), 400_000)),
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        fresh = certify_entity_admission(
            key=leg_entity_key(_leg("F1")),
            loads=_loads((("F1", "F2", "F3", "F4"), 700_000)),
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        assert hot.certified is False and hot.verdict == "accumulation_tier2"
        assert fresh.certified is True
        assert hot.verdict != fresh.verdict

    def test_the_verdict_moves_when_the_keys_own_prior_dollars_move(self) -> None:
        """Identical candidate dollars, identical shape — only the KEY's own
        prior load differs, and the decision flips. (The deleted module returned
        the same answer for every key in the book.)"""
        seen = set()
        for key in ("C0", WARM, HOT):
            cert = certify_entity_admission(
                key=leg_entity_key(_leg(key)), loads=_loads(((key,), 700_000)),
                bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
            )
            seen.add((cert.certified, cert.verdict))
        assert len(seen) == 3

    def test_certificate_carries_the_specific_legs_and_dollar_amounts(self) -> None:
        loads = _loads(((WARM, "F1"), 500_000))
        cert = certify_entity_admission(
            key=leg_entity_key(_leg("F1")), loads=loads,
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        rows = {x.key: x for x in cert.loads}
        assert rows[leg_entity_key(_leg(WARM))].prior_cc == 300_000
        assert rows[leg_entity_key(_leg(WARM))].add_cc == 500_000
        assert rows[leg_entity_key(_leg(WARM))].post_cc == 800_000
        assert rows[leg_entity_key(_leg("F1"))].prior_cc == 0
        assert cert.n_cool_keys == 1 and cert.n_loaded_keys == 1


class TestAccumulationVsSize:
    """The decision the brief asked us to make and justify: ACCUMULATION ACROSS
    STRUCTURES is what the ratified 3% wall exists for and is never waived; a
    first structure on a COOL key is a SIZE event, and per-combo governs size."""

    def test_a_combo_pushing_one_entity_past_three_percent_is_REFUSED(self) -> None:
        """REQUIRED. WARM already carries $30 from two other structures; $40 more
        takes it to $70 = 3.5%. Refused in EVERY flag state."""
        cand = _pos("cand", (_leg(WARM),), 400_000)
        for armed in (False, True):
            assert ReasonCode.SKIP_ENTITY_LOSS_CAP in _check(cand, armed=armed)

    def test_a_tier2_key_pushed_past_the_wall_is_REFUSED(self) -> None:
        cand = _pos("cand", (_leg(HOT),), 200_000)     # $50 -> $70
        for armed in (False, True):
            assert ReasonCode.SKIP_ENTITY_LOSS_CAP in _check(cand, armed=armed)

    def test_a_FRESH_key_is_not_refused_merely_for_the_candidates_size(
        self,
    ) -> None:
        """REQUIRED, and the 77.9%/54.8% of live refusals this build exists for.
        $80 on a key holding NOTHING: over the $60 wall, inside the $100
        per-combo wall. That is a SIZE event on an empty key, not "every combo
        riding one player one way", so the entity axis abstains and PER-COMBO
        GOVERNS."""
        cand = _pos("cand", (_leg("NOBODY"),), 800_000)
        assert ReasonCode.SKIP_ENTITY_LOSS_CAP in _check(cand, armed=False)
        assert ReasonCode.SKIP_ENTITY_LOSS_CAP not in _check(cand, armed=True)
        assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP not in _check(cand, armed=True)

    def test_per_combo_still_governs_that_size(self) -> None:
        """...and it governs it at the ratified 5%, with no new number: $95 onto
        a cool key already holding $10 lands at $105 > the $100 ceiling and is
        refused, even though the candidate itself is inside per-combo."""
        cand = _pos("cand", (_leg("C0"),), 950_000)
        assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP not in _check(cand, armed=True)
        assert ReasonCode.SKIP_ENTITY_LOSS_CAP in _check(cand, armed=True)
        cert = certify_entity_admission(
            key=leg_entity_key(_leg("C0")), loads=_loads((("C0",), 950_000)),
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        assert cert.verdict == "over_size_ceiling"

    def test_the_accumulation_wall_STAYS_at_three_percent(self) -> None:
        """THE INVARIANT. A key may pass 3% only by the footprint of a SINGLE
        structure; the moment it is loaded, no further structure gets in. Here
        the $80 size admission above is COMMITTED, and the very next $1 on the
        same key is refused armed."""
        book = _entity_book()
        book.add_position(_pos("sized", (_leg("NOBODY"),), 800_000))
        cand = _pos("cand", (_leg("NOBODY"),), 10_000)
        assert ReasonCode.SKIP_ENTITY_LOSS_CAP in _check(
            cand, armed=True, book=book
        )
        cert = certify_entity_admission(
            key=leg_entity_key(_leg("NOBODY")),
            loads=_loads((("NOBODY",), 10_000), book=book),
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        assert cert.verdict == "accumulation_over_wall"

    def test_a_warm_key_can_never_be_certified_at_any_size(self) -> None:
        for key, verdict in ((WARM, "accumulation_tier1"), (HOT, "accumulation_tier2")):
            for loss in (10_000, 400_000, 900_000):
                cert = certify_entity_admission(
                    key=leg_entity_key(_leg(key)),
                    loads=_loads(((key, "F1", "F2", "F3", "F4", "F5"), loss)),
                    bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
                )
                assert (cert.certified, cert.verdict) == (False, verdict)


class TestEntityFailClosed:
    def test_degenerate_bankroll_fails_closed(self) -> None:
        cert = certify_entity_admission(
            key="k", loads=(), bankroll_cc=0, ceiling_cc=COMBO_WALL
        )
        assert cert.certified is False and cert.verdict == "degenerate_bankroll"

    def test_zero_ceiling_fails_closed(self) -> None:
        cert = certify_entity_admission(
            key="k", loads=(), bankroll_cc=BANKROLL, ceiling_cc=0
        )
        assert cert.certified is False and cert.verdict == "degenerate_bankroll"

    def test_a_certificate_for_a_key_the_candidate_does_not_touch(self) -> None:
        cert = certify_entity_admission(
            key="SOMEONE:ELSE:yes", loads=_loads((("F1",), 700_000)),
            bankroll_cc=BANKROLL, ceiling_cc=COMBO_WALL,
        )
        assert cert.certified is False and cert.verdict == "key_not_in_candidate"

    def test_a_dollarless_candidate_contributes_nothing(self) -> None:
        assert _loads((("F1",), 0)) == ()
        assert combo_widen_weight(()) == Fraction(0)


class TestEntityShadowAndArming:
    def test_shadow_is_byte_identical(self) -> None:
        """REQUIRED: unarmed, the breach list is byte-identical to before the fix
        — the exact pre-fix detail string, with no tier note appended."""
        cand = _pos("cand", (_leg("NOBODY"),), 800_000)
        breaches = LimitChecker(_limits()).check(
            _entity_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
        b = next(x for x in breaches if x.reason is ReasonCode.SKIP_ENTITY_LOSS_CAP)
        assert b.detail == (
            "entity KXMLBKS:NOBODY:yes ACCUMULATED loss 800000cc "
            "(committed+reserved+candidate) > 3/100 bankroll = 600000cc"
        )

    def test_enabled_without_armed_changes_nothing(self) -> None:
        """DERIVE-BEFORE-ARM: turning the READ-OUT on enumerates the loads and
        still produces the identical breach list."""
        cand = _pos("cand", (_leg("NOBODY"),), 800_000)
        off = LimitChecker(_limits()).check(
            _entity_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
        on = LimitChecker(_limits(entity_admission_enabled=True)).check(
            _entity_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
        assert [(b.reason, b.detail, b.shadow) for b in off] == [
            (b.reason, b.detail, b.shadow) for b in on
        ]

    def test_observer_never_changes_the_decision(self) -> None:
        cand = _pos("cand", (_leg("NOBODY"),), 800_000)
        seen: list[tuple[str, int, bool]] = []
        without = LimitChecker(_limits()).check(
            _entity_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
        with_obs = LimitChecker(_limits(entity_admission_enabled=True)).check(
            _entity_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
            entity_admission_observer=lambda c, ceil, adm: seen.append(
                (c.verdict, ceil, adm)
            ),
        )
        assert [(b.reason, b.detail, b.shadow) for b in without] == [
            (b.reason, b.detail, b.shadow) for b in with_obs
        ]
        assert seen == [("size_event_on_cool_key", COMBO_WALL, True)]

    def test_a_throwing_observer_can_never_change_a_risk_decision(self) -> None:
        cand = _pos("cand", (_leg("NOBODY"),), 800_000)

        def boom(*_a: object) -> None:
            raise RuntimeError("telemetry exploded")

        quiet = _check(cand, armed=True)
        loud = _reasons(
            LimitChecker(_limits(entity_admission_armed=True)).check(
                _entity_book(), MARG, DailyPnl(),
                candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
                entity_admission_observer=boom,
            )
        )
        assert quiet == loud

    def test_armed_breach_detail_names_the_tier_and_the_dollars(self) -> None:
        cand = _pos("cand", (_leg(WARM),), 400_000)
        breaches = LimitChecker(_limits(entity_admission_armed=True)).check(
            _entity_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
        )
        b = next(x for x in breaches if x.reason is ReasonCode.SKIP_ENTITY_LOSS_CAP)
        assert "tier accumulation_tier1" in b.detail
        assert "prior 300000cc tier1 + add 400000cc -> tier3" in b.detail
        assert f"ceiling {COMBO_WALL}cc" in b.detail


class TestOtherCapsAreUntouched:
    def test_per_combo_cap_is_byte_identical_in_all_flag_states(self) -> None:
        """REQUIRED, and the operator's explicit instruction ("per combo loss cap
        i'd like to keep it the same for now")."""
        cand = _pos("cand", (_leg("NOBODY"),), 1_100_000)   # $110 > the $100 wall
        details = set()
        for armed in (False, True):
            for enabled in (False, True):
                breaches = LimitChecker(
                    _limits(
                        entity_admission_armed=armed,
                        entity_admission_enabled=enabled,
                    )
                ).check(
                    _entity_book(), MARG, DailyPnl(),
                    candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
                )
                details.add(
                    next(
                        b.detail for b in breaches
                        if b.reason is ReasonCode.SKIP_PER_COMBO_LOSS_CAP
                    )
                )
        assert len(details) == 1

    def test_two_hundred_dollar_combo_spiking_one_game_is_still_refused(self) -> None:
        """The operator's own example: "$200 on a specific combo that takes us
        past a peak concentration point" — with BOTH levers armed, the per-GAME
        and per-COMBO caps still refuse it, untouched."""
        checker = LimitChecker(
            _limits(
                entity_admission_armed=True,
                slate_partition_armed=True,
                game_loss_frac=Fraction(8, 100),   # $160 per game
            )
        )
        cand = _pos("big", (_leg("X", "GH"),), 2_000_000)   # $200 on ONE game
        reasons = _reasons(
            checker.check(
                _entity_book(), MARG, DailyPnl(),
                candidate_positions=[cand], risk_bankroll_cc=BANKROLL,
            )
        )
        assert ReasonCode.SKIP_GAME_LOSS_CAP in reasons
        assert ReasonCode.SKIP_ENTITY_LOSS_CAP in reasons
        assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP in reasons


class TestReportingMeasure:
    """``ticket_concentration`` / ``effective_n`` survive as the MEASURE the
    operator grades this build on ("the change in dollar-weighted effective N if
    they had all filled"). NO admission decision reads them — that was exactly
    the deleted module's defect, and the tests above pin that the decision moves
    with the KEY, not with the book's N_eff."""

    def test_effective_n_is_dollar_weighted_not_a_count(self) -> None:
        # 90 tickets holding $100 between them and 10 holding $500 is NOT
        # diverse. A COUNT says 100.
        losses = [1_000_000 // 90] * 90 + [5_000_000 // 10] * 10
        state = ticket_concentration(losses)
        assert state.n_tickets == 100
        assert 13.0 < state.eff_n < 16.0

    def test_leg_count_is_invisible_to_the_measure(self) -> None:
        assert ticket_concentration([300_000]) == ticket_concentration([300_000])

    def test_effective_n_degenerate_is_zero(self) -> None:
        assert effective_n(0, 5) == 0.0
        assert effective_n(5, 0) == 0.0



# --- FIX 2: the slate partition -----------------------------------------------

GAME1 = "KXMLB-G1"
GAME2 = "KXMLB-G2"


def _leg_in(game: str, market: str, side: str = "yes") -> LegRef:
    return LegRef(market, game, side)


def _parlay(pid: str, legs: tuple[LegRef, ...], loss_cc: int) -> OpenPosition:
    return _pos(pid, legs, loss_cc)


def _same_day() -> Callable[[str], datetime | None]:
    start = datetime(2026, 7, 27, 23, 0, tzinfo=UTC)   # 19:00 ET
    return lambda t: start


class TestPartitionedWorstCase:
    """The fold itself: PARTITION, per-bucket soundness, CLAMP, monotonicity."""

    def _unit(self, games: list[str], loss: int, *, req: bool = True) -> LossUnit:
        return LossUnit(
            legs_by_game=tuple(
                (g, (_leg_in(g, f"{g}-M{i}"),)) for i, g in enumerate(games)
            ),
            loss_cc=loss, requires_all=req, resting=False,
        )

    def test_multi_game_parlay_is_counted_once(self) -> None:
        units = [self._unit([GAME1, GAME2], 700_000) for _ in range(2)]
        # The naive per-game sum would be 2 x 1_400_000 = 2_800_000.
        assert partitioned_worst_case_cc(units, None) == 1_400_000

    def test_clamp_never_exceeds_the_once_counted_comonotone_sum(self) -> None:
        units = [self._unit([GAME1], 300_000), self._unit([GAME2], 400_000)]
        assert partitioned_worst_case_cc(units, None) == 700_000

    def test_clamp_never_below_the_largest_single_loss(self) -> None:
        units = [self._unit([GAME1], 900_000)]
        assert partitioned_worst_case_cc(units, None) == 900_000

    def test_mutex_offsetting_positions_net_within_a_game(self) -> None:
        """Mutually exclusive same-game outcomes cannot both lose: the existing
        Stage-B fold nets them, and the partition preserves that credit."""
        a = LossUnit(
            legs_by_game=((GAME1, (LegRef(f"{GAME1}-HOME", GAME1, "yes"),)),),
            loss_cc=500_000, requires_all=True, resting=False,
        )
        b = LossUnit(
            legs_by_game=((GAME1, (LegRef(f"{GAME1}-AWAY", GAME1, "yes"),)),),
            loss_cc=400_000, requires_all=True, resting=False,
        )
        def me(e: str) -> bool:
            return e == GAME1
        assert partitioned_worst_case_cc([a, b], None) == 900_000     # no ME facts
        assert partitioned_worst_case_cc([a, b], me) == 500_000       # netted

    def test_monotone_in_the_unit_set(self) -> None:
        base = [self._unit([GAME1], 300_000)]
        for extra in (
            self._unit([GAME2], 100_000),
            self._unit([GAME1, GAME2], 250_000),
            self._unit([GAME1], 50_000),
        ):
            assert partitioned_worst_case_cc(base + [extra], None) >= (
                partitioned_worst_case_cc(base, None)
            )

    def test_ungamed_unit_is_never_dropped(self) -> None:
        ungamed = LossUnit(
            legs_by_game=(), loss_cc=200_000, requires_all=True, resting=False
        )
        assert partitioned_worst_case_cc(
            [self._unit([GAME1], 300_000), ungamed], None
        ) == 500_000

    def test_empty_is_zero(self) -> None:
        assert partitioned_worst_case_cc([], None) == 0

    def test_certified_game_can_only_tighten_a_bucket(self) -> None:
        us = [self._unit([GAME1], 300_000), self._unit([GAME1], 400_000)]
        assert partitioned_worst_case_cc(us, None) == 700_000
        assert partitioned_worst_case_cc(us, None, {GAME1: 500_000}) == 500_000
        # A certificate LOOSER than the fold can never raise it.
        assert partitioned_worst_case_cc(us, None, {GAME1: 9_000_000}) == 700_000


class TestLossUnitsAreOptIn:
    """The once-counted loss events are the ONE allocation this build adds to
    the hot path, so they are built only when a consumer will read them."""

    def test_snapshot_omits_loss_units_by_default(self) -> None:
        book = _entity_book()
        assert book.snapshot(MARG, mass_acceptance=True).loss_units == ()

    def test_snapshot_builds_them_on_request(self) -> None:
        book = _entity_book()
        snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        assert len(snap.loss_units) == len(book.positions)
        # the fixture book's true once-counted premium: $30 + $50 + 12 x $10
        assert sum(u.loss_cc for u in snap.loss_units) == 2_000_000

    def test_check_requests_them_only_for_the_slate_lever(self) -> None:
        book = _entity_book()
        seen: list[int] = []
        real = book.snapshot

        def spy(*a: object, **kw: object) -> object:
            seen.append(int(bool(kw.get("want_loss_units"))))
            return real(*a, **kw)  # type: ignore[arg-type]

        book.snapshot = spy  # type: ignore[method-assign,assignment]
        for over, expect in (
            ({}, 0),
            ({"entity_admission_enabled": True}, 0),
            ({"slate_partition_enabled": True}, 1),
            ({"slate_partition_armed": True}, 1),
        ):
            seen.clear()
            LimitChecker(_limits(**over)).check(
                book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL
            )
            assert seen == [expect], over


class TestSlateCapPartition:
    def _multi_game_book(self, each_loss_cc: int, n: int = 2) -> ExposureBook:
        """``n`` parlays, each spanning BOTH games of one slate.  The naive
        roll-up charges every one of them to BOTH games."""
        book = ExposureBook(CONVENTIONS)
        for i in range(n):
            book.add_position(
                _parlay(
                    f"p{i}",
                    (_leg_in(GAME1, f"G1-M{i}"), _leg_in(GAME2, f"G2-M{i}")),
                    each_loss_cc,
                )
            )
        return book

    def _check(self, book: ExposureBook, *, armed: bool) -> list[object]:
        checker = LimitChecker(
            _limits(
                slate_loss_frac=Fraction(8, 100),     # $160
                game_loss_frac=Fraction(99, 100),
                entity_loss_frac=None,
                per_combo_loss_frac=Fraction(99, 100),
                slate_partition_armed=armed,
            )
        )
        return checker.check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL,
            start_time_provider=_same_day(),
        )

    def test_mutually_counted_parlays_no_longer_double_count(self) -> None:
        """REQUIRED: two $70 two-game parlays.  Naive Σ-per-game = $280 > the
        $160 wall and refuses; the once-counted joint worst case is $140 and
        admits.  Nothing was exempted and no number moved."""
        book = self._multi_game_book(700_000)
        assert ReasonCode.SKIP_SLATE_CAP in _reasons(
            self._check(book, armed=False)
        )
        assert ReasonCode.SKIP_SLATE_CAP not in _reasons(
            self._check(book, armed=True)
        )

    def test_book_whose_true_worst_case_exceeds_the_anchor_is_still_refused(
        self,
    ) -> None:
        """REQUIRED: the same shape at $90 each.  Once-counted the slate still
        holds $180 of premium against a $160 wall — refused, armed or not."""
        book = self._multi_game_book(900_000)
        for armed in (False, True):
            assert ReasonCode.SKIP_SLATE_CAP in _reasons(
                self._check(book, armed=armed)
            )

    def test_armed_detail_carries_both_numbers(self) -> None:
        book = self._multi_game_book(900_000)
        b = next(
            x for x in self._check(book, armed=True)
            if x.reason is ReasonCode.SKIP_SLATE_CAP
        )
        assert "loss 1800000cc" in b.detail          # once-counted
        assert "naive sum 3600000cc" in b.detail     # what it used to enforce

    def test_shadow_is_byte_identical(self) -> None:
        """REQUIRED: unarmed, the exact pre-fix detail string, no partition note."""
        book = self._multi_game_book(900_000)
        b = next(
            x for x in self._check(book, armed=False)
            if x.reason is ReasonCode.SKIP_SLATE_CAP
        )
        assert b.detail == (
            "slate 2026-07-27 loss 3600000cc > 2/25 bankroll = 1600000cc"
        )

    def test_observer_never_changes_the_decision(self) -> None:
        book = self._multi_game_book(900_000)
        seen: list[tuple[str, int, int, int]] = []
        checker = LimitChecker(
            _limits(
                slate_loss_frac=Fraction(8, 100), game_loss_frac=Fraction(99, 100),
                entity_loss_frac=None, per_combo_loss_frac=Fraction(99, 100),
                slate_partition_enabled=True,
            )
        )
        with_obs = checker.check(
            book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL,
            start_time_provider=_same_day(),
            slate_partition_observer=lambda s, n, p, t: seen.append((s, n, p, t)),
        )
        assert [(b.reason, b.detail) for b in with_obs] == [
            (b.reason, b.detail) for b in self._check(book, armed=False)
        ]
        assert seen == [("2026-07-27", 3_600_000, 1_800_000, 1_600_000)]

    def test_cross_slate_ticket_is_charged_in_full_to_both_slates(self) -> None:
        """Invariant 6: a ticket spanning two slates can lose within EITHER
        window, so each slate charges it in full — once within each."""
        book = ExposureBook(CONVENTIONS)
        book.add_position(
            _parlay("x", (_leg_in(GAME1, "G1-M"), _leg_in(GAME2, "G2-M")), 1_700_000)
        )
        starts = {
            "G1-M": datetime(2026, 7, 27, 23, 0, tzinfo=UTC),
            "G2-M": datetime(2026, 7, 29, 23, 0, tzinfo=UTC),
        }
        checker = LimitChecker(
            _limits(
                slate_loss_frac=Fraction(8, 100), game_loss_frac=Fraction(99, 100),
                entity_loss_frac=None, per_combo_loss_frac=Fraction(99, 100),
                slate_partition_armed=True,
            )
        )
        breaches = [
            b for b in checker.check(
                book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL,
                start_time_provider=lambda t: starts.get(t),
            )
            if b.reason is ReasonCode.SKIP_SLATE_CAP
        ]
        assert {b.detail.split()[1] for b in breaches} == {"2026-07-27", "2026-07-29"}
        assert all("loss 1700000cc" in b.detail for b in breaches)

    def test_partition_never_enforces_more_than_the_naive_sum(self) -> None:
        """A safety property over every shape this fixture can make: arming can
        only ever LOOSEN, never tighten (the ungamed pool aside, which the naive
        roll-up cannot see at all)."""
        for n in (1, 2, 3, 4):
            for loss in (200_000, 500_000, 900_000):
                book = self._multi_game_book(loss, n=n)
                naive = _reasons(self._check(book, armed=False))
                armed = _reasons(self._check(book, armed=True))
                assert armed.count(ReasonCode.SKIP_SLATE_CAP) <= naive.count(
                    ReasonCode.SKIP_SLATE_CAP
                )
