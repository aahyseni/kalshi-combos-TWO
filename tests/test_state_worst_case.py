"""STATE-CONSISTENT per-game worst case (sim/state_worst_case.py) — the
confirm-path waiver machinery for handoff Problem A.

Covers the mandated properties: mutex respected (opposing-advance NO parlays
never co-lose; over/under on one market never co-loses), monotone in adding
open quotes (each quote contributes >= 0 per state), no hedge credit for
resting quotes, NO HEDGE CREDIT for ``earns_credit=False`` entities (the
outstanding-reservation treatment — finding 2, 2026-07-16: a released
reservation vanishes like an unfilled quote, so its miss-side credit never
certifies; its hit-side loss still sums), adversarial resolution of
non-structural / cross-game legs, fail-closed full-premium fallbacks (no
structural leg in the game, non-NO side, reserved holdings, unknown leg side),
<= the analytic comonotone sum, uncertifiable games, empty inputs, adapter
mapping from risk/exposure types, and CENT-EXACT PARITY with the validated
prototype (tools/proto_state_worst_case.py) on the same inputs — rule-8
discipline.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition, OpenQuoteRisk
from combomaker.sim.state_worst_case import (
    UNCERTIFIED_NO_PLAN,
    GameWorstCase,
    WorstCaseEntity,
    WorstCaseQuote,
    entity_from_position,
    quote_from_open_quote,
    state_worst_case_by_game,
    trim_open_quotes_for_games,
)
from combomaker.sim.structural_book import StructuralConfigView
from tools.proto_state_worst_case import (
    ProtoEntity,
    ProtoQuote,
    proto_state_worst_case_by_game,
)

# --- fixture game 1: ENG vs ARG knockout (KXWC => knockout format) -----------
GAME = "26JUL15ENGARG"
ADV_EV = f"KXWCADVANCE-{GAME}"
TOT_EV = f"KXWCTOTAL-{GAME}"
CORN_EV = f"KXWCCORNERS-{GAME}"
GOAL_EV = f"KXWCGOAL-{GAME}"
H1TOT_EV = f"KXWC1HTOTAL-{GAME}"
ARG_ADV = f"KXWCADVANCE-{GAME}-ARG"       # ARG suffixes ENGARG -> Team.B
ENG_ADV = f"KXWCADVANCE-{GAME}-ENG"       # ENG prefixes ENGARG -> Team.A
TOT3 = f"KXWCTOTAL-{GAME}-3"              # over 2.5 (>= 3 goals in 90')
CORN = f"KXWCCORNERS-{GAME}-10"           # corners: NOT scoreline-settleable
PLAYER = f"KXWCGOAL-{GAME}-ARGLMESSI10-1"  # ARG scorer, 1+ goals
H1TOT1 = f"KXWC1HTOTAL-{GAME}-1"          # 1H over 0.5

# --- fixture game 2: FRA vs ESP (cross-game legs) ----------------------------
GAME2 = "26JUL16FRAESP"
ML2_EV = f"KXWCGAME-{GAME2}"
TOT2_EV = f"KXWCTOTAL-{GAME2}"
FRA_ML = f"KXWCGAME-{GAME2}-FRA"          # regulation moneyline (TeamWin, 90')
TOT2 = f"KXWCTOTAL-{GAME2}-3"

# --- fixture game 3: GROUP-format soccer (no ET, no advance branches) --------
GAME3 = "26JUL20LAFCSEA"
ML3_EV = f"KXMLSGAME-{GAME3}"
TOT3G_EV = f"KXMLSTOTAL-{GAME3}"
GOAL3_EV = f"KXMLSGOAL-{GAME3}"
ML3 = f"KXMLSGAME-{GAME3}-LAFC"
TOT3G1 = f"KXMLSTOTAL-{GAME3}-1"          # over 0.5 (>= 1 goal)
PLAYER3 = f"KXMLSGOAL-{GAME3}-LAFCJDOE9-1"

MARGINALS: dict[str, float] = {
    ARG_ADV: 0.55, ENG_ADV: 0.45, TOT3: 0.48, CORN: 0.50,
    PLAYER: 0.25, H1TOT1: 0.65,
    FRA_ML: 0.45, TOT2: 0.60,
    ML3: 0.45, TOT3G1: 0.85, PLAYER3: 0.25,
}
CFG = StructuralConfigView()
CFG_SMALL = StructuralConfigView(max_goals=6)  # fast grid for the parity sweep

CONV = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# 1.00 contract = 100 centi-contracts -> gross notional $1 = 10_000 cc.
CONTRACTS = 100
NOTIONAL = 10_000


def leg(market: str, event: str | None, side: str = "yes") -> LegRef:
    return LegRef(market, event, side)


def ent(
    eid: str,
    legs: tuple[LegRef, ...],
    price_cc: int,
    *,
    side: Side = Side.NO,
    fee_cc: int = 0,
    modeled: bool = True,
    credit: bool = True,
) -> WorstCaseEntity:
    return WorstCaseEntity(
        eid, side, CONTRACTS, price_cc, legs, fee_cc, modeled, credit
    )


def quote(qid: str, *hypos: WorstCaseEntity) -> WorstCaseQuote:
    return WorstCaseQuote(qid, hypos)


def run(
    entities: list[WorstCaseEntity],
    quotes: list[WorstCaseQuote],
    cfg: StructuralConfigView = CFG,
) -> dict[str, GameWorstCase]:
    return state_worst_case_by_game(entities, quotes, MARGINALS, None, cfg)


# canonical entities (mirror the prototype demo — hand-computed expectations)
ARG_POS = ent("pos:arg", (leg(ARG_ADV, ADV_EV),), 8000)
ENG_POS = ent("pos:eng", (leg(ENG_ADV, ADV_EV),), 8000)
ENG_QUOTE = quote("q:eng", ent("q:eng:no", (leg(ENG_ADV, ADV_EV),), 8000))
OU_OVER = ent("pos:ou-over", (leg(ARG_ADV, ADV_EV), leg(TOT3, TOT_EV, "yes")), 8000)
OU_UNDER = ent("pos:ou-under", (leg(ARG_ADV, ADV_EV), leg(TOT3, TOT_EV, "no")), 8000)
CORN_ONLY = ent("pos:corners", (leg(CORN, CORN_EV),), 2500)
CORN_MIX = ent("pos:corners-mix", (leg(CORN, CORN_EV), leg(ARG_ADV, ADV_EV)), 2000)
# The outstanding-reservation treatment (finding 2): hedge credit clamped.
RESV_NC = ent("resv:eng-nc", (leg(ENG_ADV, ADV_EV),), 8000, credit=False)


# ------------------------------- mutex ---------------------------------------
class TestMutex:
    def test_opposing_advance_positions_never_co_lose(self):
        # In every enumerated state (incl. both shootout branches of a level
        # state) exactly one team advances: one parlay hits (8000), the other
        # provably misses (credit 8000 - 10000 = -2000). Worst = 6000, not 16000.
        r = run([ARG_POS, ENG_POS], [])[GAME]
        assert r.certified
        assert r.worst_case_cc == 6000

    def test_over_under_pair_never_co_loses(self):
        # Same total market held on both sides (the dimension the analytic
        # Stage-B bound can NEVER split — proto_mutex_game_cap test 8): the
        # state enumeration nets it exactly.
        r = run([OU_OVER, OU_UNDER], [])[GAME]
        assert r.certified
        assert r.worst_case_cc == 6000

    def test_one_sided_book_sums(self):
        # Two same-side ARG parlays CO-LOSE in every ARG state (no exclusion
        # between them) — netted only by the single opposing ENG credit:
        # ARG states: 8000 + 8000 - 2000 = 14000; ENG states: -2000*2 + 8000.
        r = run([ARG_POS, ent("pos:arg2", (leg(ARG_ADV, ADV_EV),), 8000), ENG_POS], [])[GAME]
        assert r.certified
        assert r.worst_case_cc == 14000

    def test_advance_branch_split_counts_states(self):
        # Level-after-ET states split into two shootout branches: with the
        # default cfg (max_goals=12, et_max=6) the knockout FT support is 793
        # states, branch-doubled to 1586. Pins the enumeration support — if
        # this moves, the support drifted and the parity gate must be re-run.
        r = run([ARG_POS, ENG_POS], [])[GAME]
        assert r.n_states == 1586


# --------------------------- quotes: clamp + monotone ------------------------
class TestQuotes:
    def test_position_plus_opposing_quote_do_not_sum(self):
        # THE handoff-A demo: ARG-advance-parlay-NO position + ENG-advance-
        # parlay-NO QUOTE. The quote is clamped at 0 in ARG states and its hit
        # states are hedged by the position's miss credit -> worst == 8000.
        r = run([ARG_POS], [ENG_QUOTE])[GAME]
        assert r.certified
        assert r.worst_case_cc == 8000

    def test_resting_quote_never_earns_credit(self):
        # Same legs committed instead of resting: nets to 6000. The resting
        # quote version stays at the position's full premium (8000).
        committed = run([ARG_POS, ENG_POS], [])[GAME].worst_case_cc
        resting = run([ARG_POS], [ENG_QUOTE])[GAME].worst_case_cc
        assert committed == 6000
        assert resting == 8000
        assert resting > committed

    def test_co_directional_quote_adds_fully(self):
        arg_quote = quote("q:arg", ent("q:arg:no", (leg(ARG_ADV, ADV_EV),), 8000))
        r = run([ARG_POS], [arg_quote])[GAME]
        assert r.worst_case_cc == 16000

    def test_two_sided_quote_worse_side_per_state(self):
        # YES-side hypothetical is non-NO -> fail-closed constant 3000; NO-side
        # nets per state. Contribution = max(0, max(sides)) per state.
        two_sided = quote(
            "q:2s",
            ent("q:2s:yes", (leg(ENG_ADV, ADV_EV),), 3000, side=Side.YES),
            ent("q:2s:no", (leg(ENG_ADV, ADV_EV),), 7000),
        )
        r = run([ARG_POS], [two_sided])[GAME]
        # ARG states: 8000 + max(0, max(3000, -3000)) = 11000
        # ENG states: -2000 + max(0, max(3000, 7000)) = 5000
        assert r.worst_case_cc == 11000

    def test_quote_with_no_hypotheticals_contributes_nothing(self):
        r = run([ARG_POS, ENG_POS], [WorstCaseQuote("q:empty", ())])
        assert r[GAME].worst_case_cc == 6000

    def test_monotone_adding_quotes_explicit(self):
        base = run([ARG_POS, ENG_POS], [])[GAME].worst_case_cc
        for q in (
            ENG_QUOTE,
            quote("q:arg", ent("q:arg:no", (leg(ARG_ADV, ADV_EV),), 5000)),
            quote("q:corn", ent("q:corn:no", (leg(CORN, CORN_EV),), 2500)),
            quote("q:ou", ent("q:ou:no", (leg(TOT3, TOT_EV, "no"),), 4000)),
        ):
            assert run([ARG_POS, ENG_POS], [q])[GAME].worst_case_cc >= base


# ------------- reservations: hedge credit clamped (finding 2, 2026-07-16) ----
class TestReservationCreditClamp:
    def test_no_credit_entity_never_hedges_the_candidate(self):
        # The finding-2 channel: an outstanding OPPOSING-advance reservation
        # must NOT supply the miss-side credit that certifies the candidate —
        # if it is later RELEASED the committed book outlives the credit. The
        # clamped bound is the candidate's own premium (8000), never the
        # fully-netted 6000.
        resv = ent("resv:eng", (leg(ENG_ADV, ADV_EV),), 8000, credit=False)
        r = run([ARG_POS, resv], [])[GAME]
        assert r.certified
        assert r.worst_case_cc == 8000

    def test_no_credit_entity_hit_loss_still_sums_fully(self):
        # Assume-committed conservatism: the reservation's HIT side is intact —
        # a co-directional reservation still sums (16000), exactly as before.
        resv = ent("resv:arg2", (leg(ARG_ADV, ADV_EV),), 8000, credit=False)
        r = run([ARG_POS, resv], [])[GAME]
        assert r.worst_case_cc == 16000

    def test_committed_still_nets_fully(self):
        # The design's committed-position netting is untouched by the clamp.
        r = run([ARG_POS, ENG_POS], [])[GAME]
        assert r.worst_case_cc == 6000

    def test_reservation_treatment_equals_quote_treatment(self):
        # A clamped entity is exactly the resting-quote clamp for the same
        # legs/price: both vanish without settling, both get max(0, loss).
        resv = ent("resv:eng", (leg(ENG_ADV, ADV_EV),), 8000, credit=False)
        as_entity = run([ARG_POS, resv], [])[GAME].worst_case_cc
        as_quote = run([ARG_POS], [ENG_QUOTE])[GAME].worst_case_cc
        assert as_entity == as_quote == 8000

    def test_adding_a_no_credit_entity_never_lowers_the_bound(self):
        # Monotone: a clamped entity contributes >= 0 per state, so adding one
        # can never LOWER the certified bound (the property the full-netting
        # reservation violated).
        base = run([ARG_POS, ENG_POS], [])[GAME].worst_case_cc
        for legs in (
            (leg(ENG_ADV, ADV_EV),),
            (leg(ARG_ADV, ADV_EV),),
            (leg(TOT3, TOT_EV, "no"),),
        ):
            resv = ent("resv:x", legs, 7000, credit=False)
            assert run([ARG_POS, ENG_POS, resv], [])[GAME].worst_case_cc >= base

    def test_no_credit_fail_closed_paths_unchanged(self):
        # Fail-closed entities are a constant hit_loss >= 0 in every state —
        # the clamp is a no-op there (full premium either way).
        for kwargs in ({"side": Side.YES}, {"modeled": False}):
            fc_credit = ent("e", (leg(ARG_ADV, ADV_EV),), 8000, **kwargs)
            fc_clamped = ent(
                "e", (leg(ARG_ADV, ADV_EV),), 8000, credit=False, **kwargs
            )
            a = run([fc_credit, ENG_POS], [])[GAME].worst_case_cc
            b = run([fc_clamped, ENG_POS], [])[GAME].worst_case_cc
            assert a == b == 16000


_QUOTE_LEGS = st.sampled_from(
    [
        (leg(ARG_ADV, ADV_EV),),
        (leg(ENG_ADV, ADV_EV),),
        (leg(TOT3, TOT_EV, "yes"),),
        (leg(TOT3, TOT_EV, "no"),),
        (leg(CORN, CORN_EV),),
        (leg(ARG_ADV, ADV_EV), leg(TOT3, TOT_EV, "yes")),
    ]
)
_QUOTE = st.builds(
    lambda legs, price, i: quote(f"q:{i}", ent(f"q:{i}:no", legs, price)),
    legs=_QUOTE_LEGS,
    price=st.integers(1000, 9000),
    i=st.integers(0, 999),
)


@given(quotes=st.lists(_QUOTE, max_size=4), extra=_QUOTE)
@settings(max_examples=40, deadline=None)
def test_monotone_adding_a_quote_never_lowers_the_bound(quotes, extra):
    """Adding any open quote never LOWERS the certified bound (each quote
    contributes >= 0 per state). Entities keep the game identified in every
    draw, so certification is fixed — the regime the property is stated for
    (the certification-flip caveat is documented + tested separately)."""
    base = run([ARG_POS, ENG_POS], quotes)[GAME].worst_case_cc
    more = run([ARG_POS, ENG_POS], [*quotes, extra])[GAME].worst_case_cc
    assert more >= base


_ENTITY_POOL = st.sampled_from(
    [ARG_POS, ENG_POS, OU_OVER, OU_UNDER, CORN_ONLY, CORN_MIX, RESV_NC]
)


@given(entities=st.lists(_ENTITY_POOL, min_size=1, max_size=5, unique_by=id),
       quotes=st.lists(_QUOTE, max_size=3))
@settings(max_examples=40, deadline=None)
def test_bound_never_exceeds_the_analytic_comonotone_sum(entities, quotes):
    """worst_case_cc <= sum of every entity's premium(+fee) + every quote's
    worst quotable side — certified or not (uncertified IS that sum)."""
    comonotone = sum(e.hit_loss_cc for e in entities) + sum(
        max((h.hit_loss_cc for h in q.hypotheticals), default=0) for q in quotes
    )
    r = run(entities, quotes)[GAME]
    assert r.worst_case_cc <= comonotone


# ------------------- adversarial / fail-closed resolutions -------------------
class TestAdversarialAndFailClosed:
    def test_corners_only_parlay_full_premium_every_state(self):
        # No structural leg in the game -> full comonotone premium in EVERY
        # state: it adds exactly its premium to the netted advance book.
        base = run([ARG_POS, ENG_POS], [])[GAME].worst_case_cc
        with_corn = run([ARG_POS, ENG_POS, CORN_ONLY], [])[GAME].worst_case_cc
        assert with_corn == base + 2500

    def test_mixed_corners_parlay_still_nets_on_its_structural_leg(self):
        # Corners resolves adversarially (assume hit) but the ARG-advance leg
        # still nets against the ENG position: worst 0, not 10000.
        r = run([CORN_MIX, ENG_POS], [])[GAME]
        assert r.certified
        assert r.worst_case_cc == 0

    def test_cross_game_leg_resolves_adversarially(self):
        # [ARG_ADV(G1) + FRA_ML(G2)]: in G1 the G2 leg is assumed hit, so the
        # parlay hits in every ARG state — but still nets vs the ENG position.
        cross = ent("pos:cross", (leg(ARG_ADV, ADV_EV), leg(FRA_ML, ML2_EV)), 8000)
        res = run([cross, ENG_POS], [])
        assert res[GAME].certified
        assert res[GAME].worst_case_cc == 6000
        # In G2 the game is touched only by this entity; FRA_ML alone cannot
        # identify a plan (1 team-level leg) -> uncertified comonotone there.
        assert not res[GAME2].certified
        assert res[GAME2].worst_case_cc == 8000
        assert res[GAME2].n_states == 0

    def test_cross_game_certified_when_g2_identified(self):
        cross = ent("pos:cross", (leg(ARG_ADV, ADV_EV), leg(FRA_ML, ML2_EV)), 8000)
        g2tot = ent("pos:g2tot", (leg(TOT2, TOT2_EV),), 8000)
        res = run([cross, ENG_POS, g2tot], [])
        assert res[GAME2].certified
        # FRA-win and over-2.5 CAN co-occur (3-0): comonotone is reachable.
        assert res[GAME2].worst_case_cc == 16000

    def test_non_no_side_entity_fails_closed(self):
        yes_arg = ent("pos:yes", (leg(ARG_ADV, ADV_EV),), 8000, side=Side.YES)
        r = run([yes_arg, ENG_POS], [])[GAME]
        assert r.worst_case_cc == 16000  # no netting for a non-NO entity

    def test_reserved_holding_fails_closed(self):
        reserved = ent("pos:res", (leg(ARG_ADV, ADV_EV),), 8000, modeled=False)
        r = run([reserved, ENG_POS], [])[GAME]
        assert r.worst_case_cc == 16000

    def test_unknown_leg_side_fails_closed_whole_entity(self):
        bad = ent("pos:bad", (leg(ARG_ADV, ADV_EV, "maybe"),), 8000)
        r = run([bad, ENG_POS], [])[GAME]
        assert r.worst_case_cc == 16000

    def test_fee_widens_hit_loss_and_shrinks_credit(self):
        arg_fee = ent("pos:argfee", (leg(ARG_ADV, ADV_EV),), 8000, fee_cc=500)
        r = run([arg_fee, ENG_POS], [])[GAME]
        # ARG states: 8500 + (8000-10000) = 6500; ENG: (8500-10000) + 8000 = 6500
        assert r.worst_case_cc == 6500

    def test_ungamed_leg_is_adversarial_not_blocking(self):
        # A leg with no resolvable event never grants credit but never blocks:
        # the entity still nets on its gamed structural leg.
        ug = ent("pos:ug", (leg("SYNTH-NOGAME", None), leg(ARG_ADV, ADV_EV)), 8000)
        r = run([ug, ENG_POS], [])[GAME]
        assert r.worst_case_cc == 6000


# ------------------------ player / half / group format -----------------------
class TestStructuralCoverage:
    def test_scorer_total_netting_in_group_format(self):
        # GROUP format (no ET): scorer possibly-hits iff the team scored. A
        # 90'-under parlay and a scorer parlay can never co-lose. The quote's
        # legs identify the model (TeamWin + TotalOver) — the quote-
        # identification path.
        py = ent("pos:py", (leg(PLAYER3, GOAL3_EV),), 8000)
        tu = ent("pos:tu", (leg(TOT3G1, TOT3G_EV, "no"),), 8000)
        q = quote(
            "q:ml3",
            ent("q:ml3:no", (leg(ML3, ML3_EV), leg(TOT3G1, TOT3G_EV)), 8000),
        )
        r = run([py, tu], [q])[GAME3]
        assert r.certified
        # LAFC>=1-goal LAFC-win states: 8000 + (8000-10000) + 8000 = 14000;
        # 0-0: -2000 + 8000 + 0 = 6000. Never 24000 (the comonotone).
        assert r.worst_case_cc == 14000
        assert r.n_states == 169  # 13x13 group grid, no shootout branches

    def test_scorer_yes_no_does_not_net(self):
        # The scorer coin is NOT part of the scoreline state: NO on a scorer
        # possibly-hits everywhere, so scorer yes/no parlays may co-lose
        # (conservative, correct — no fake hedge from an unresolved coin).
        py = ent("pos:py", (leg(PLAYER3, GOAL3_EV, "yes"),), 8000)
        pn = ent("pos:pn", (leg(PLAYER3, GOAL3_EV, "no"),), 8000)
        q = quote(
            "q:ml3",
            ent("q:ml3:no", (leg(ML3, ML3_EV), leg(TOT3G1, TOT3G_EV)), 8000),
        )
        r = run([py, pn], [q])[GAME3]
        assert r.worst_case_cc == 24000  # comonotone reachable

    def test_half_leg_upgrades_enumeration_and_nets(self):
        half = ent("pos:half", (leg(H1TOT1, H1TOT_EV), leg(ARG_ADV, ADV_EV)), 8000)
        r = run([half, ENG_POS], [])[GAME]
        assert r.certified
        # ARG states with a 1H goal: 8000 - 2000 = 6000; ENG states: 6000.
        assert r.worst_case_cc == 6000
        # Half-aware enumeration is strictly larger than the FT-only 1586.
        assert r.n_states > 1586
        assert r.n_states % 2 == 0  # advance branch doubling intact


# --------------------------- certification -----------------------------------
class TestCertification:
    def test_unidentifiable_game_is_uncertified_comonotone(self):
        res = run([CORN_ONLY], [])
        r = res[GAME]
        assert not r.certified
        assert r.worst_case_cc == 2500
        assert r.n_states == 0
        assert r.uncertified_reason == UNCERTIFIED_NO_PLAN

    def test_certification_flip_documented(self):
        # The certification-flip caveat (module docstring): an over/under pair
        # alone (one team-level ticker) is uncertified -> comonotone 16000; a
        # quote whose leg identifies the model flips it certified and the
        # committed pair nets. BOTH are valid state-dominant upper bounds on
        # every realizable fill subset; the flip is why global monotonicity in
        # quotes is stated at fixed certification.
        before = run([OU_OVER2LEGLESS := ent(
            "pos:ouo", (leg(TOT3, TOT_EV, "yes"),), 8000
        ), OU_UNDER2 := ent("pos:ouu", (leg(TOT3, TOT_EV, "no"),), 8000)], [])
        assert not before[GAME].certified
        assert before[GAME].worst_case_cc == 16000
        after = run(
            [OU_OVER2LEGLESS, OU_UNDER2],
            [quote("q:adv", ent("q:adv:no", (leg(ARG_ADV, ADV_EV),), 1000))],
        )
        assert after[GAME].certified
        # The over/under pair now nets (hit side 8000, miss side credit -2000)
        # and the quote adds its clamped premium where its parlay hits:
        # worst = 8000 - 2000 + 1000 = 7000 < the 16000 comonotone.
        assert after[GAME].worst_case_cc == 7000

    def test_disabled_structural_config_fails_closed(self):
        r = state_worst_case_by_game(
            [ARG_POS, ENG_POS], [], MARGINALS, None,
            StructuralConfigView(enabled=False),
        )[GAME]
        assert not r.certified
        assert r.worst_case_cc == 16000

    def test_missing_marginal_drops_leg_from_inversion_not_settlement(self):
        # TOT3 marginal missing: the over/under legs leave the INVERSION but
        # still settle from the scoreline (settlement is marginal-free), so the
        # pair still nets. ARG/ENG advances keep the game identified.
        marginals = {k: v for k, v in MARGINALS.items() if k != TOT3}
        r = state_worst_case_by_game(
            [OU_OVER, OU_UNDER, ENG_POS], [], marginals, None, CFG
        )[GAME]
        assert r.certified
        # ARG&tot>=3: 8000-2000-2000=4000; ARG&tot<3: 4000; ENG: -2000-2000+8000.
        assert r.worst_case_cc == 4000


# ------------------------------ empty / events -------------------------------
class TestEdges:
    def test_empty_inputs(self):
        assert state_worst_case_by_game([], [], {}, None, CFG) == {}

    def test_events_mapping_supplements_missing_leg_events(self):
        # Same book as the mutex test but LegRef.event_ticker is None and the
        # events mapping supplies it — identical result.
        arg = ent("pos:arg", (leg(ARG_ADV, None),), 8000)
        eng = ent("pos:eng", (leg(ENG_ADV, None),), 8000)
        events = {ARG_ADV: ADV_EV, ENG_ADV: ADV_EV}
        r = state_worst_case_by_game([arg, eng], [], MARGINALS, events, CFG)[GAME]
        assert r.certified
        assert r.worst_case_cc == 6000


# ------------------------------- adapters ------------------------------------
class TestAdapters:
    def test_entity_from_position_maps_to_the_cent(self):
        pos = OpenPosition(
            position_id="p1", combo_ticker="COMBO", collection=None,
            our_side=Side.NO, contracts=CentiContracts(250),
            entry_price_cc=CentiCents(4321),
            legs=(leg(ARG_ADV, ADV_EV),),
        )
        e = entity_from_position(pos, fee_cc=7)
        assert e.entity_id == "p1"
        assert e.premium_cc == pos.max_loss_cc
        assert e.gross_notional_cc == pos.gross_settlement_notional_cc
        assert e.hit_loss_cc == pos.max_loss_cc + 7
        assert e.legs == pos.legs
        assert e.risk_modeled is pos.risk_modeled

    def test_quote_from_open_quote_mirrors_hypothetical_positions(self):
        q = OpenQuoteRisk(
            quote_id="q1", rfq_id="r1", combo_ticker="COMBO", collection=None,
            yes_bid_cc=CentiCents(0), no_bid_cc=CentiCents(6000),
            contracts=CentiContracts(100), legs=(leg(ENG_ADV, ADV_EV),),
        )
        wq = quote_from_open_quote(q, CONV)
        assert wq.quote_id == "q1"
        assert len(wq.hypotheticals) == 1
        h = wq.hypotheticals[0]
        assert h.entity_id == "q1:no"
        assert h.our_side is Side.NO
        assert h.entry_price_cc == 6000
        assert h.contracts_centi == 100

    def test_adapter_path_equals_direct_construction(self):
        pos = OpenPosition(
            position_id="pos:arg", combo_ticker="COMBO", collection=None,
            our_side=Side.NO, contracts=CentiContracts(100),
            entry_price_cc=CentiCents(8000), legs=(leg(ARG_ADV, ADV_EV),),
        )
        q = OpenQuoteRisk(
            quote_id="q:eng", rfq_id="r1", combo_ticker="COMBO", collection=None,
            yes_bid_cc=CentiCents(0), no_bid_cc=CentiCents(8000),
            contracts=CentiContracts(100), legs=(leg(ENG_ADV, ADV_EV),),
        )
        via_adapters = state_worst_case_by_game(
            [entity_from_position(pos)], [quote_from_open_quote(q, CONV)],
            MARGINALS, None, CFG,
        )[GAME]
        direct = run([ARG_POS], [ENG_QUOTE])[GAME]
        assert via_adapters == direct


# ------------------------- parity with the prototype --------------------------
def _to_proto_entity(e: WorstCaseEntity) -> ProtoEntity:
    return ProtoEntity(
        e.entity_id, str(e.our_side), e.contracts_centi, e.entry_price_cc,
        tuple((lg.market_ticker, lg.event_ticker, lg.side) for lg in e.legs),
        e.fee_cc, e.risk_modeled, e.earns_credit,
    )


def _to_proto_quote(q: WorstCaseQuote) -> ProtoQuote:
    return ProtoQuote(q.quote_id, tuple(_to_proto_entity(h) for h in q.hypotheticals))


def _assert_parity(
    entities: list[WorstCaseEntity],
    quotes: list[WorstCaseQuote],
    cfg: StructuralConfigView,
) -> None:
    module = state_worst_case_by_game(entities, quotes, MARGINALS, None, cfg)
    proto = proto_state_worst_case_by_game(
        [_to_proto_entity(e) for e in entities],
        [_to_proto_quote(q) for q in quotes],
        dict(MARGINALS), None, cfg,
    )
    as_tuples = {
        g: (r.worst_case_cc, r.certified, r.n_states, r.uncertified_reason)
        for g, r in module.items()
    }
    assert as_tuples == proto


class TestPrototypeParity:
    def test_parity_on_the_demo_book_default_cfg(self):
        _assert_parity(
            [ARG_POS, OU_OVER, OU_UNDER, CORN_ONLY, CORN_MIX, RESV_NC],
            [ENG_QUOTE],
            CFG,
        )

    def test_parity_on_the_rich_multi_game_book(self):
        # Every semantic branch at once: netting positions, over/under, corners
        # (pure + mixed), fee, half-leg upgrade, player possibility, fail-closed
        # side/reserved/unknown-leg entities, cross-game legs, a certified and
        # an uncertified game, two-sided + empty + corners quotes.
        entities = [
            ARG_POS,
            OU_OVER,
            OU_UNDER,
            CORN_ONLY,
            CORN_MIX,
            ent("pos:fee", (leg(ENG_ADV, ADV_EV),), 8000, fee_cc=250),
            ent("pos:half", (leg(H1TOT1, H1TOT_EV), leg(ARG_ADV, ADV_EV)), 7000),
            ent("pos:player", (leg(PLAYER, GOAL_EV), leg(ENG_ADV, ADV_EV)), 6000),
            ent("pos:yes-side", (leg(ARG_ADV, ADV_EV),), 3000, side=Side.YES),
            ent("pos:reserved", (leg(ENG_ADV, ADV_EV),), 3000, modeled=False),
            ent("pos:badleg", (leg(TOT3, TOT_EV, "maybe"),), 3000),
            ent("pos:cross", (leg(ARG_ADV, ADV_EV), leg(FRA_ML, ML2_EV)), 8000),
            ent("pos:g3-player", (leg(PLAYER3, GOAL3_EV),), 8000),
            ent("pos:g3-under", (leg(TOT3G1, TOT3G_EV, "no"),), 8000),
            # finding-2 branch: a clamped (reservation-treatment) entity.
            ent("resv:eng-nc", (leg(ENG_ADV, ADV_EV),), 8000, credit=False),
        ]
        quotes = [
            ENG_QUOTE,
            quote(
                "q:2s",
                ent("q:2s:yes", (leg(ENG_ADV, ADV_EV),), 3000, side=Side.YES),
                ent("q:2s:no", (leg(ENG_ADV, ADV_EV),), 7000),
            ),
            quote("q:corn", ent("q:corn:no", (leg(CORN, CORN_EV),), 2500)),
            WorstCaseQuote("q:empty", ()),
            quote(
                "q:ml3",
                ent("q:ml3:no", (leg(ML3, ML3_EV), leg(TOT3G1, TOT3G_EV)), 8000),
            ),
        ]
        _assert_parity(entities, quotes, CFG_SMALL)
        # GAME2 touched only by the cross entity's single leg -> uncertified in
        # both implementations (sanity that parity covered that branch too).
        module = state_worst_case_by_game(entities, quotes, MARGINALS, None, CFG_SMALL)
        assert not module[GAME2].certified
        assert module[GAME].certified
        assert module[GAME3].certified


# --------------------------------------------------------------------------- #
# WAIVER ENTITY-SET TRIM (2026-07-18): trim_open_quotes_for_games             #
# --------------------------------------------------------------------------- #


class TestTrimOpenQuotesForGames:
    """The pure trim seam the last-look waiver applies when
    ``lastlook_waiver_topk_resting`` > 0 (rfq/lifecycle). Ranking, adders, the
    exact-drop of unrelated-game quotes, the union-keep across breached games,
    and the DOMINANCE property (trimmed + adder >= full — fail-closed)."""

    def test_keeps_topk_by_worst_loss_and_folds_tail_into_adder(self) -> None:
        qs = [
            quote("qa", ent("qa:no", (leg(ARG_ADV, ADV_EV),), 8000)),
            quote("qb", ent("qb:no", (leg(ENG_ADV, ADV_EV),), 5000)),
            quote("qc", ent("qc:no", (leg(ARG_ADV, ADV_EV),), 500)),
        ]
        kept, adders = trim_open_quotes_for_games(qs, [GAME], None, 2)
        assert [q.quote_id for q in kept] == ["qa", "qb"]  # input order kept
        assert adders == {GAME: 500}

    def test_worst_hit_loss_is_the_comonotone_per_quote_term(self) -> None:
        two_sided = quote(
            "q2",
            ent("q2:yes", (leg(ARG_ADV, ADV_EV),), 3000, side=Side.YES),
            ent("q2:no", (leg(ARG_ADV, ADV_EV),), 7000),
        )
        assert two_sided.worst_hit_loss_cc == 7000
        assert WorstCaseQuote("qe", ()).worst_hit_loss_cc == 0

    def test_unrelated_game_quote_dropped_without_adder(self) -> None:
        qs = [
            quote("qa", ent("qa:no", (leg(ARG_ADV, ADV_EV),), 8000)),
            quote("qx", ent("qx:no", (leg(FRA_ML, ML2_EV),), 9000)),
        ]
        kept, adders = trim_open_quotes_for_games(qs, [GAME], None, 5)
        assert [q.quote_id for q in kept] == ["qa"]
        assert adders == {}

    def test_union_keep_across_breached_games_never_double_charges(self) -> None:
        # qx touches BOTH breached games; it makes GAME's top-K (only toucher)
        # but not GAME2's (two larger quotes there) — kept via the union, so it
        # is enumerated everywhere and adds NOTHING anywhere.
        big2 = [
            quote(f"qg2-{i}", ent(f"qg2-{i}:no", (leg(FRA_ML, ML2_EV),), 9000))
            for i in range(2)
        ]
        cross = quote(
            "qx", ent("qx:no", (leg(ARG_ADV, ADV_EV), leg(FRA_ML, ML2_EV)), 8000)
        )
        kept, adders = trim_open_quotes_for_games(
            [*big2, cross], [GAME, GAME2], None, 2
        )
        assert {q.quote_id for q in kept} == {"qg2-0", "qg2-1", "qx"}
        assert adders == {}

    def test_dropped_cross_game_quote_adds_to_every_breached_game_it_touches(
        self,
    ) -> None:
        # k=0 keeps nothing: the cross quote's worst-side bound rides on BOTH
        # breached games (each game's fold is bounded independently).
        cross = quote(
            "qx", ent("qx:no", (leg(ARG_ADV, ADV_EV), leg(FRA_ML, ML2_EV)), 8000)
        )
        kept, adders = trim_open_quotes_for_games([cross], [GAME, GAME2], None, 0)
        assert kept == ()
        assert adders == {GAME: 8000, GAME2: 8000}

    @given(
        prices=st.lists(st.integers(500, 9000), min_size=1, max_size=6),
        k=st.integers(0, 6),
    )
    @settings(max_examples=25, deadline=None)
    def test_trimmed_plus_adder_dominates_full_enumeration(
        self, prices: list[int], k: int
    ) -> None:
        """THE fail-closed property: for every book and every K, the trimmed
        certified worst case plus the dropped-tail adder is >= the FULL
        enumeration's certified worst case — the trim can only tighten the
        admit direction, never loosen it."""
        qs = [
            quote(
                f"q{i}",
                ent(
                    f"q{i}:no",
                    (leg(ARG_ADV if i % 2 else ENG_ADV, ADV_EV),),
                    p,
                ),
            )
            for i, p in enumerate(prices)
        ]
        entities = [ent("cand", (leg(ARG_ADV, ADV_EV), leg(TOT3, TOT_EV)), 8000)]
        full = run(entities, qs, CFG_SMALL)[GAME].worst_case_cc
        kept, adders = trim_open_quotes_for_games(qs, [GAME], None, k)
        trimmed = run(entities, list(kept), CFG_SMALL)[GAME].worst_case_cc
        assert trimmed + adders.get(GAME, 0) >= full
