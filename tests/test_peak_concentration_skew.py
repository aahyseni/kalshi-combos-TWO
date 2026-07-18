"""Peak-concentration PRICING steer (operator directive 2026-07-18 evening):
the committed-book peak-state profile (sim/peak_profile.py) + the additive
peak component on the skew seam (risk/skew._peak_component).

Covers the mandated minimum: (1) a peak-stacking candidate (hits the book's
cached worst scoreline) is WIDENED, scaled up as the book's peak approaches
the game-loss budget; (2) an anti-peak candidate (provably misses every cached
peak) earns the TIGHTEN rebate; (3) neutral/unknown (no profile, foreign game,
non-structural legs, unknown side, half-leg vs non-halves profile, disabled)
=> a hard ZERO adder; (4) clamps respected, including composition with the
directional skew at both extremes (overall bound = the sum of the cap pairs);
(5) empty/tiny book => ~zero adders; (6) generation-stamped cache: a stale
profile is NEUTRAL, a matching one is active; (7) property sweep: the adder
never exceeds its clamp and the mechanism is PRICING ONLY (an int into the
pricer — never a refusal, and it never feeds the widen-vs-decline per_game);
(8) end-to-end through the REAL construct_quote (the public quoting path the
existing skew tests use): a peak-stacker prices a strictly HIGHER implied YES
ask than the same quote on a fresh book.

Plus builder anchors: the profile's per-game ``top_loss_cc`` equals the
certified ``state_worst_case_by_game`` worst case on the SAME committed-only
book (the waiver machinery is the single source of state truth — hard rule
8c), shootout-branch handling on Advance games, and fail-safe omission of
uncertifiable games.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import DOC_ASSUMED, Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.grid import PriceGrid
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import ConstructedQuote, construct_quote
from combomaker.risk.exposure import ExposureBook, ExposureSnapshot, LegRef, OpenPosition
from combomaker.risk.skew import (
    InventorySkew,
    SkewLimits,
    SkewParams,
    WidenPolicyParams,
    compute_inventory_skew,
    decide_widen_or_decline,
)
from combomaker.sim.peak_profile import (
    PeakProfile,
    build_peak_profile,
    evaluate_peak_containment,
)
from combomaker.sim.state_worst_case import (
    entity_from_position,
    state_worst_case_by_game,
)
from combomaker.sim.structural_book import StructuralConfigView

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

CFG = StructuralConfigView()

# --- fixture game A: FRA vs ESP (moneyline + total invert the DC model) ------
GAME = "26JUL16FRAESP"
ML_EV = f"KXWCGAME-{GAME}"
TOT_EV = f"KXWCTOTAL-{GAME}"
CORN_EV = f"KXWCCORNERS-{GAME}"
H1_EV = f"KXWC1HTOTAL-{GAME}"
FRA_ML = f"KXWCGAME-{GAME}-FRA"            # FRA prefixes FRAESP -> Team.A
ESP_ML = f"KXWCGAME-{GAME}-ESP"            # ESP suffixes FRAESP -> Team.B
TOT3 = f"KXWCTOTAL-{GAME}-3"               # over 2.5 (>= 3 goals in 90')
CORN = f"KXWCCORNERS-{GAME}-10"            # corners: NOT scoreline-settleable
H1TOT1 = f"KXWC1HTOTAL-{GAME}-1"           # 1H over 0.5 (half-aware states only)

# --- fixture game B: ENG vs ARG knockout Advance (shootout branches) ---------
GAME_B = "26JUL15ENGARG"
ADV_EV = f"KXWCADVANCE-{GAME_B}"
TOTB_EV = f"KXWCTOTAL-{GAME_B}"
BTTSB_EV = f"KXWCBTTS-{GAME_B}"
ARG_ADV = f"KXWCADVANCE-{GAME_B}-ARG"      # ARG suffixes ENGARG -> Team.B
ENG_ADV = f"KXWCADVANCE-{GAME_B}-ENG"      # ENG prefixes ENGARG -> Team.A
TOTB6 = f"KXWCTOTAL-{GAME_B}-6"            # over 5.5 (>= 6 goals in 90')
BTTSB = f"KXWCBTTS-{GAME_B}-BTTS"          # both teams to score

MARGINALS: dict[str, float] = {
    FRA_ML: 0.45, TOT3: 0.60, CORN: 0.50, H1TOT1: 0.65,
    ARG_ADV: 0.55, ENG_ADV: 0.45, TOTB6: 0.10, BTTSB: 0.55,
}


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def no_position(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    contracts: int = 10_000,       # centi-contracts: 10_000 = 100 contracts
    entry_price: int = 3_000,      # $0.30 -> premium $30.00 at 100 contracts
) -> OpenPosition:
    """A LONG-NO position (sell-only book) — same helper shape as test_skew."""
    return OpenPosition(
        position_id=pid,
        combo_ticker="COMBO",
        collection=None,
        our_side=Side.NO,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


STACK_LEGS = (LegRef(FRA_ML, ML_EV, "yes"), LegRef(TOT3, TOT_EV, "yes"))
ANTI_LEGS = (LegRef(FRA_ML, ML_EV, "no"),)


def committed_book(held_contracts: int = 10_000) -> list[OpenPosition]:
    """A one-way book: long NO of the {FRA wins & over 2.5} parlay — the peak
    scoreline cluster (every worst state has FRA winning with >= 3 goals)."""
    return [no_position("held", STACK_LEGS, contracts=held_contracts)]


def profile_for(
    positions: list[OpenPosition], *, k: int = 5, generation: int = 7
) -> PeakProfile:
    return build_peak_profile(
        positions, MARGINALS, None, CFG, k=k, input_generation=generation
    )


def limits_with_budget(budget_dollars: float) -> SkewLimits:
    """Delta/notional axes kept loose so the peak/loss budget is the driver."""
    return SkewLimits(
        max_event_delta_contracts=1e9,
        max_event_worst_case_loss_dollars=budget_dollars,
        max_event_gross_notional_dollars=1e12,
    )


PARAMS = SkewParams(enabled=True)  # peak_enabled defaults True
LIMITS_50 = limits_with_budget(50.0)  # book premium $30 -> peak_ratio 0.6


def snapshot_of(positions: list[OpenPosition]) -> ExposureSnapshot:
    book = ExposureBook(CONVENTIONS)
    for position in positions:
        book.add_position(position)
    return book.snapshot(provider(MARGINALS), mass_acceptance=False)


def skew_for(
    candidate: OpenPosition,
    positions: list[OpenPosition],
    *,
    profile: PeakProfile | None,
    generation: int | None = 7,
    limits: SkewLimits = LIMITS_50,
    params: SkewParams = PARAMS,
) -> InventorySkew:
    return compute_inventory_skew(
        candidate,
        snapshot_of(positions),
        provider(MARGINALS),
        CONVENTIONS,
        limits,
        params,
        peak_profile=profile,
        peak_book_generation=generation,
    )


# ---------------------------------------------------------------------------
# (1) Peak-stacking candidate is WIDENED, scaled by peak-vs-budget.
# ---------------------------------------------------------------------------


class TestPeakStacking:
    def test_stacker_gets_widened_additively(self) -> None:
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        base = skew_for(candidate, book, profile=None)
        peaked = skew_for(candidate, book, profile=profile)
        assert peaked.peak_cc > 0
        assert peaked.peak_widen_cc > 0 and peaked.peak_tighten_cc == 0
        # Additive composition: the directional halves are untouched and the
        # composed classifier is exactly baseline + the peak component.
        assert peaked.concentration_cc == base.concentration_cc
        assert peaked.offset_cc == base.offset_cc
        assert peaked.skew_cc == base.skew_cc + peaked.peak_cc
        assert [row[3] for row in peaked.peak_per_game] == ["peak_hit"]

    def test_exact_formula_values(self) -> None:
        # Hand-computed: premium(cand) = 1_000 x 3_000 // 100 = 30_000cc ($3);
        # budget $50 = 500_000cc -> overlap 0.06. Book premium $30 -> peak
        # 300_000cc -> ratio 0.6. severity 1 (hits the worst state).
        # widen = 600 x 0.06 x 0.6**2 = 12.96 -> 13cc.
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        peaked = skew_for(candidate, book, profile=profile)
        assert peaked.peak_cc == 13
        assert peaked.peak_per_game == ((GAME, 13, 0.06, "peak_hit"),)

    def test_widen_scales_up_as_peak_approaches_budget(self) -> None:
        # Same candidate, same $50 budget; the BOOK grows -> the game's peak
        # loss approaches (then hits) the budget -> the widen ramps convexly.
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        last = -1
        adders: list[int] = []
        for held in (2_500, 5_000, 10_000, 15_000, 25_000):
            book = committed_book(held_contracts=held)
            profile = profile_for(book)
            peaked = skew_for(candidate, book, profile=profile)
            assert peaked.peak_cc >= last
            last = peaked.peak_cc
            adders.append(peaked.peak_cc)
        assert adders[0] < adders[-1]  # strictly larger by the top
        # ratios 0.15/0.3/0.6/0.9/1.0, overlap 0.06, gamma 2:
        assert adders == [1, 3, 13, 29, 36]

    def test_severity_scales_by_which_peak_tier_is_hit(self) -> None:
        # Severity < 1 needs the cached top-K to span DIFFERENT loss tiers, so
        # use a small grid where the worst tier has < K states: high-entry
        # positions keep the miss-side credit small (premium ~ notional), so
        # tier2 stays a positive loss instead of netting negative.
        #   p1 = NO {FRA wins & over 2.5} 100ct @ $0.90 -> hit 900k, miss -100k
        #   p2 = NO {ESP wins}             50ct @ $0.80 -> hit 400k, miss -100k
        # FRA-win-over states (4 cells on the max_goals=3 grid): 800k = tier1;
        # ESP-win states: 300k = tier2. K=8 caches 4 + 4 (verified layout).
        cfg_small = StructuralConfigView(max_goals=3)
        marginals = {FRA_ML: 0.45, ESP_ML: 0.35, TOT3: 0.60}
        book = [
            no_position("p1", STACK_LEGS, contracts=10_000, entry_price=9_000),
            no_position(
                "p2",
                (LegRef(ESP_ML, ML_EV, "yes"),),
                contracts=5_000,
                entry_price=8_000,
            ),
        ]
        profile = build_peak_profile(
            book, marginals, None, cfg_small, k=8, input_generation=7
        )
        losses = [
            loss for sl in profile.by_game[GAME].slices for loss in sl.losses_cc
        ]
        assert sorted(losses, reverse=True) == [800_000] * 4 + [300_000] * 4
        tier1 = evaluate_peak_containment(profile, GAME, list(STACK_LEGS))
        tier2 = evaluate_peak_containment(
            profile, GAME, [LegRef(ESP_ML, ML_EV, "yes")]
        )
        assert tier1 is not None and tier1.hit_severity == 1.0
        assert tier2 is not None and tier2.hit_severity == 0.375  # 300k/800k
        # Through the skew: the tier-1 stacker pays strictly more widen.
        limits = limits_with_budget(100.0)  # peak_ratio = 0.8
        t1 = no_position("c1", STACK_LEGS, contracts=1_000)
        t2 = no_position("c2", (LegRef(ESP_ML, ML_EV, "yes"),), contracts=1_000)
        snap = snapshot_of(book)
        s1 = compute_inventory_skew(
            t1, snap, provider(marginals), CONVENTIONS, limits, PARAMS,
            peak_profile=profile, peak_book_generation=7,
        )
        s2 = compute_inventory_skew(
            t2, snap, provider(marginals), CONVENTIONS, limits, PARAMS,
            peak_profile=profile, peak_book_generation=7,
        )
        assert s1.peak_cc > s2.peak_cc > 0


# ---------------------------------------------------------------------------
# (2) Anti-peak candidate (provably misses all cached peaks) earns the rebate.
# ---------------------------------------------------------------------------


class TestAntiPeak:
    def test_provable_miss_earns_rebate(self) -> None:
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", ANTI_LEGS, contracts=1_000)
        base = skew_for(candidate, book, profile=None)
        peaked = skew_for(candidate, book, profile=profile)
        assert peaked.peak_cc < 0
        assert peaked.peak_tighten_cc > 0 and peaked.peak_widen_cc == 0
        assert peaked.skew_cc == base.skew_cc + peaked.peak_cc
        # rebate = 150 x 0.06 x 0.6 = 5.4 -> 5cc.
        assert peaked.peak_per_game == ((GAME, -5, 0.06, "peak_miss_rebate"),)

    def test_advance_mutex_opposite_side_rebates(self) -> None:
        # Advance is exactly-mutex per enumerated state (shootout branches
        # split): a book net long-NO of {ARG advances} peaks on ARG states,
        # where an {ENG advances} parlay provably misses -> rebate; the
        # same-side candidate hits -> widen. (Both sides held so the game's
        # committed universe carries the >= 2 identifying legs the DC
        # inversion needs; the small ENG side keeps the peak on ARG states.)
        book = [
            no_position("arg", (LegRef(ARG_ADV, ADV_EV, "yes"),), entry_price=6_000),
            no_position(
                "eng",
                (LegRef(ENG_ADV, ADV_EV, "yes"),),
                contracts=2_000,
                entry_price=6_000,
            ),
        ]
        profile = build_peak_profile(
            book, MARGINALS, None, CFG, k=5, input_generation=7
        )
        assert GAME_B in profile.by_game
        # ARG states: 600k hit − 80k ENG credit = 520k (the cached plateau).
        assert profile.by_game[GAME_B].top_loss_cc == 520_000
        same = no_position("c-arg", (LegRef(ARG_ADV, ADV_EV, "yes"),), contracts=1_000)
        anti = no_position("c-eng", (LegRef(ENG_ADV, ADV_EV, "yes"),), contracts=1_000)
        s_same = skew_for(same, book, profile=profile)
        s_anti = skew_for(anti, book, profile=profile)
        assert s_same.peak_cc > 0
        assert s_anti.peak_cc < 0


# ---------------------------------------------------------------------------
# PLATEAU REBATE CERTIFICATION (2026-07-18 adversarial-verify fix, SERIOUS).
# The K cached rows are a SAMPLE; on a one-way Advance book all ~793
# same-outcome states tie at the identical loss and the K rows land on the
# "ENG 0 - ARG 1..K" corner. A plateau-STACKING refinement (ARG-adv & over
# 5.5 / & BTTS) provably missed those K rows and pocketed the rebate while
# RAISING the certified worst case. The rebate must certify a miss of the
# ENTIRE top-loss level (GamePeakProfile.plateau_slices).
# ---------------------------------------------------------------------------


def one_way_advance_book() -> list[OpenPosition]:
    """Net one-way {ARG advances} book (small ENG side so the game's committed
    universe carries the >= 2 identifying legs the inversion needs). ARG
    states: 600k hit - 80k ENG credit = 520k, the plateau across EVERY
    ARG-advance state (~793 of the 1586 branch-expanded states)."""
    return [
        no_position("arg", (LegRef(ARG_ADV, ADV_EV, "yes"),), entry_price=6_000),
        no_position(
            "eng",
            (LegRef(ENG_ADV, ADV_EV, "yes"),),
            contracts=2_000,
            entry_price=6_000,
        ),
    ]


class TestPlateauRebateCertification:
    def _skew(
        self,
        candidate: OpenPosition,
        book: list[OpenPosition],
        profile: PeakProfile,
    ) -> InventorySkew:
        return skew_for(candidate, book, profile=profile)

    def test_probe_a_plateau_stacker_over55_gets_no_rebate(self) -> None:
        # THE verifier probe: NO {ARG-adv & over 5.5} misses all K cached rows
        # (their totals are 1..K) but can still hit plateau states with >= 6
        # goals — it STACKS the plateau. Certified worst case RISES by its
        # full premium, so any rebate is unsound: it must grade NEUTRAL.
        book = one_way_advance_book()
        profile = profile_for(book)
        gp = profile.by_game[GAME_B]
        assert gp.top_loss_cc == 520_000
        assert gp.plateau_slices is not None
        assert gp.n_plateau_states > gp.n_peak_states  # plateau wider than K
        stacker = no_position(
            "cand",
            (LegRef(ARG_ADV, ADV_EV, "yes"), LegRef(TOTB6, TOTB_EV, "yes")),
            contracts=1_000,
        )
        skew = self._skew(stacker, book, profile)
        assert skew.peak_tighten_cc == 0                    # NO rebate
        assert skew.peak_cc >= 0
        reasons = {row[3] for row in skew.peak_per_game if row[0] == GAME_B}
        assert reasons == {"neutral"}                       # not peak_miss_rebate
        # Acknowledge the money fact the old rebate ignored: the candidate
        # RAISES the certified state-consistent worst case by its premium.
        entities = [entity_from_position(p) for p in book + [stacker]]
        with_cand = state_worst_case_by_game(entities, [], MARGINALS, None, CFG)[GAME_B]
        assert with_cand.certified
        assert with_cand.worst_case_cc == 520_000 + stacker.max_loss_cc

    def test_probe_b_plateau_stacker_btts_gets_no_rebate(self) -> None:
        # Same shape via BTTS: cached corner rows are all "ENG 0 - ARG k"
        # (BTTS-yes provably misses there) but plateau states where both
        # score and ARG advances exist -> neutral, never a rebate.
        book = one_way_advance_book()
        profile = profile_for(book)
        stacker = no_position(
            "cand",
            (LegRef(ARG_ADV, ADV_EV, "yes"), LegRef(BTTSB, BTTSB_EV, "yes")),
            contracts=1_000,
        )
        skew = self._skew(stacker, book, profile)
        assert skew.peak_tighten_cc == 0
        assert skew.peak_cc >= 0
        reasons = {row[3] for row in skew.peak_per_game if row[0] == GAME_B}
        assert reasons == {"neutral"}

    def test_true_opposite_outcome_still_rebates(self) -> None:
        # NO {ENG advances} provably misses EVERY plateau state (advance is
        # exactly-mutex per enumerated state incl. both shootout branches):
        # the genuine flattening flow keeps its rebate under the full-plateau
        # certification.
        book = one_way_advance_book()
        profile = profile_for(book)
        anti = no_position("cand", (LegRef(ENG_ADV, ADV_EV, "yes"),), contracts=1_000)
        skew = self._skew(anti, book, profile)
        assert skew.peak_tighten_cc > 0
        assert skew.peak_cc < 0
        reasons = {row[3] for row in skew.peak_per_game if row[0] == GAME_B}
        assert reasons == {"peak_miss_rebate"}

    def test_mixed_book_rebate_requires_missing_whole_plateau_only(self) -> None:
        # Plateau + lower shoulder (small grid: 4-state tier1 plateau @800k,
        # tier2 shoulder @300k, negative elsewhere). The rebate requires
        # missing the WHOLE PLATEAU — not the shoulders: {FRA & under 2.5}
        # misses every plateau state (all are FRA-win-over) and every cached
        # row, so it rebates even though it hits uncached negative-loss
        # shoulder states. A shoulder-hitting candidate ({ESP wins}) stays on
        # the widen path.
        cfg_small = StructuralConfigView(max_goals=3)
        marginals = {FRA_ML: 0.45, ESP_ML: 0.35, TOT3: 0.60}
        book = [
            no_position("p1", STACK_LEGS, contracts=10_000, entry_price=9_000),
            no_position(
                "p2",
                (LegRef(ESP_ML, ML_EV, "yes"),),
                contracts=5_000,
                entry_price=8_000,
            ),
        ]
        profile = build_peak_profile(
            book, marginals, None, cfg_small, k=8, input_generation=7
        )
        gp = profile.by_game[GAME]
        assert gp.plateau_slices is not None
        assert gp.n_plateau_states == 4  # the exact tier1 plateau
        under = no_position(
            "c-under",
            (LegRef(FRA_ML, ML_EV, "yes"), LegRef(TOT3, TOT_EV, "no")),
            contracts=1_000,
        )
        snap = snapshot_of(book)
        s_under = compute_inventory_skew(
            under, snap, provider(marginals), CONVENTIONS,
            limits_with_budget(100.0), PARAMS,
            peak_profile=profile, peak_book_generation=7,
        )
        assert s_under.peak_tighten_cc > 0          # whole-plateau miss => rebate
        shoulder = no_position("c-esp", (LegRef(ESP_ML, ML_EV, "yes"),), contracts=1_000)
        s_shoulder = compute_inventory_skew(
            shoulder, snap, provider(marginals), CONVENTIONS,
            limits_with_budget(100.0), PARAMS,
            peak_profile=profile, peak_book_generation=7,
        )
        assert s_shoulder.peak_cc > 0               # widen path, untouched

    @settings(derandomize=True, max_examples=120, deadline=None)
    @given(
        cand=st.integers(min_value=1, max_value=200_000),
        shape=st.sampled_from(
            ["arg", "eng", "arg_over", "eng_over", "arg_btts", "over", "arg_no"]
        ),
        budget=st.floats(min_value=1.0, max_value=500.0),
    )
    def test_property_rebate_implies_zero_loss_on_every_plateau_state(
        self, cand: int, shape: str, budget: float
    ) -> None:
        # PROPERTY (verifier (e)): a granted rebate implies the candidate adds
        # ZERO loss in EVERY top-plateau state — asserted against the FULL
        # enumeration (a k = n_states profile caches every state row; severity
        # 1.0 there iff the candidate can hit a state at the top loss level),
        # not against the K-row cache that grants the rebate.
        shapes: dict[str, tuple[LegRef, ...]] = {
            "arg": (LegRef(ARG_ADV, ADV_EV, "yes"),),
            "eng": (LegRef(ENG_ADV, ADV_EV, "yes"),),
            "arg_over": (
                LegRef(ARG_ADV, ADV_EV, "yes"), LegRef(TOTB6, TOTB_EV, "yes"),
            ),
            "eng_over": (
                LegRef(ENG_ADV, ADV_EV, "yes"), LegRef(TOTB6, TOTB_EV, "yes"),
            ),
            "arg_btts": (
                LegRef(ARG_ADV, ADV_EV, "yes"), LegRef(BTTSB, BTTSB_EV, "yes"),
            ),
            "over": (LegRef(TOTB6, TOTB_EV, "yes"),),
            "arg_no": (LegRef(ARG_ADV, ADV_EV, "no"),),
        }
        candidate = no_position("cand", shapes[shape], contracts=cand)
        skew = compute_inventory_skew(
            candidate,
            _PLATEAU_SNAP,
            provider(MARGINALS),
            CONVENTIONS,
            limits_with_budget(budget),
            PARAMS,
            peak_profile=_PLATEAU_PROFILE_K5,
            peak_book_generation=7,
        )
        rebated = any(row[3] == "peak_miss_rebate" for row in skew.peak_per_game)
        if rebated:
            # The full profile caches EVERY enumerated state with its loss;
            # its severity is 1.0 iff the candidate can hit a state at the
            # top loss level. Rebate => severity < 1.0 there, i.e. the
            # candidate adds ZERO loss in every top-plateau state. (A
            # legitimate opposite-outcome hedge still HITS the book's
            # negative-loss states, so full-cache misses_all is NOT the
            # oracle — the top-level severity is.)
            full = evaluate_peak_containment(
                _PLATEAU_PROFILE_FULL, GAME_B, list(candidate.legs)
            )
            assert full is not None
            assert full.hit_severity < 1.0   # cannot hit ANY top-plateau state


_PLATEAU_BOOK = one_way_advance_book()
_PLATEAU_SNAP = snapshot_of(_PLATEAU_BOOK)
_PLATEAU_PROFILE_K5 = profile_for(_PLATEAU_BOOK)
# k >= the enumeration size => the "cache" IS the full enumeration (every
# state row with its loss) — the property's independent full-state oracle.
_PLATEAU_PROFILE_FULL = profile_for(_PLATEAU_BOOK, k=1_000_000)


# ---------------------------------------------------------------------------
# (3) Neutral / unknown -> a hard ZERO adder (fail-safe; pricing only).
# ---------------------------------------------------------------------------


class TestNeutralUnknown:
    def test_no_profile_is_byte_identical_to_baseline(self) -> None:
        book = committed_book()
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        base = skew_for(candidate, book, profile=None, generation=None)
        plain = compute_inventory_skew(
            candidate,
            snapshot_of(book),
            provider(MARGINALS),
            CONVENTIONS,
            LIMITS_50,
            PARAMS,
        )
        assert base == plain
        assert base.peak_cc == 0 and base.peak_per_game == ()

    def test_candidate_game_absent_from_profile(self) -> None:
        book = committed_book()  # profile only knows GAME (FRA/ESP)
        profile = profile_for(book)
        foreign = no_position(
            "cand", (LegRef(ARG_ADV, ADV_EV, "yes"),), contracts=1_000
        )
        peaked = skew_for(foreign, book, profile=profile)
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game == ((GAME_B, 0, 0.0, "no_peak_profile"),)

    def test_non_structural_candidate_is_unknown_zero(self) -> None:
        # Corners never settle from the scoreline -> nothing evaluable ->
        # UNKNOWN -> zero (never a widen born from doubt).
        book = committed_book()
        profile = profile_for(book)
        corners = no_position(
            "cand", (LegRef(CORN, CORN_EV, "yes"),), contracts=1_000
        )
        peaked = skew_for(corners, book, profile=profile)
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game == ((GAME, 0, 0.0, "unknown"),)

    def test_unknown_leg_side_is_unknown_zero(self) -> None:
        book = committed_book()
        profile = profile_for(book)
        weird = no_position(
            "cand",
            (LegRef(FRA_ML, ML_EV, "maybe"), LegRef(TOT3, TOT_EV, "yes")),
            contracts=1_000,
        )
        peaked = skew_for(weird, book, profile=profile)
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game == ((GAME, 0, 0.0, "unknown"),)

    def test_half_leg_vs_nonhalves_profile_is_unknown_zero(self) -> None:
        # The committed book has no 1H legs, so the cached states are the FT
        # enumeration (``_NO_HALF`` sentinels); a 1H candidate indicator raises
        # by design and the containment fail-safes to UNKNOWN -> zero.
        book = committed_book()
        profile = profile_for(book)
        assert not profile.by_game[GAME].params.with_halves
        half = no_position("cand", (LegRef(H1TOT1, H1_EV, "yes"),), contracts=1_000)
        peaked = skew_for(half, book, profile=profile)
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game == ((GAME, 0, 0.0, "unknown"),)

    def test_peak_disabled_is_zero(self) -> None:
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        off = SkewParams(enabled=True, peak_enabled=False)
        peaked = skew_for(candidate, book, profile=profile, params=off)
        assert peaked.peak_cc == 0 and peaked.peak_per_game == ()

    def test_zero_budget_is_neutral(self) -> None:
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        no_budget = limits_with_budget(0.0)
        peaked = skew_for(candidate, book, profile=profile, limits=no_budget)
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game == (("*", 0, 0.0, "no_budget"),)


# ---------------------------------------------------------------------------
# (4) Clamps — including composition with the directional skew at extremes.
# ---------------------------------------------------------------------------


class TestClamps:
    def test_peak_widen_clamped_at_cap(self) -> None:
        # overlap 1 (candidate premium >= budget), ratio 1 (book peak >=
        # budget), severity 1 -> the raw term IS the cap; never above it.
        book = committed_book()
        profile = profile_for(book)
        big = no_position("cand", STACK_LEGS, contracts=100_000)
        peaked = skew_for(big, book, profile=profile, limits=limits_with_budget(1.0))
        assert peaked.peak_cc == PARAMS.peak_widen_max_cc == 600

    def test_peak_tighten_clamped_at_cap(self) -> None:
        book = committed_book()
        profile = profile_for(book)
        big_anti = no_position("cand", ANTI_LEGS, contracts=100_000)
        peaked = skew_for(
            big_anti, book, profile=profile, limits=limits_with_budget(1.0)
        )
        assert peaked.peak_cc == -PARAMS.peak_tighten_max_cc == -150

    def test_composed_widen_bound_is_sum_of_caps(self) -> None:
        # Directional at ITS clamp (+600: huge concentrating candidate at util
        # 1) + peak at ITS clamp (+600) -> composed classifier exactly 1200 =
        # skew_max_widen_cc + peak_widen_max_cc, and applied negates it.
        book = committed_book()
        profile = profile_for(book)
        big = no_position("cand", STACK_LEGS, contracts=900_000)
        peaked = skew_for(big, book, profile=profile, limits=limits_with_budget(1.0))
        assert peaked.skew_cc == 600 + 600
        assert peaked.applied_cc == -1200

    def test_composed_tighten_bound_is_sum_of_caps(self) -> None:
        # Directional rebate at its clamp (-150) + peak rebate at its clamp
        # (-150): candidate long-NO of {FRA no, TOT3 no} opposes the book's
        # delta AND provably misses every peak state (FRA wins & over).
        book = committed_book(held_contracts=200_000)
        profile = profile_for(book)
        anti = no_position(
            "cand",
            (LegRef(FRA_ML, ML_EV, "no"), LegRef(TOT3, TOT_EV, "no")),
            contracts=150_000,
        )
        # Tight delta axis so the directional util is 1 and its rebate clamps.
        limits = SkewLimits(
            max_event_delta_contracts=10.0,
            max_event_worst_case_loss_dollars=1.0,
            max_event_gross_notional_dollars=1e12,
        )
        peaked = skew_for(anti, book, profile=profile, limits=limits)
        assert peaked.peak_cc == -150
        assert peaked.skew_cc == -(150 + 150)

    def test_peak_never_feeds_widen_vs_decline(self) -> None:
        # PRICING ONLY: the peak component must not create a decline. The
        # widen-vs-decline policy reads ``per_game`` (directional) — a pure
        # peak-stacker with NO directional concentration (empty directional
        # per-game via a missing marginal) never trips it even at full clamp.
        book = committed_book()
        profile = profile_for(book)
        big = no_position("cand", STACK_LEGS, contracts=900_000)
        snap = snapshot_of(book)
        # Missing marginal for the candidate's legs -> directional map empty.
        peaked = compute_inventory_skew(
            big,
            snap,
            provider({}),
            CONVENTIONS,
            limits_with_budget(1.0),
            PARAMS,
            peak_profile=profile,
            peak_book_generation=7,
        )
        assert peaked.peak_cc == 600          # full peak widen...
        assert peaked.per_game == ()          # ...but no directional entry
        decision = decide_widen_or_decline(
            peaked, snap, big, limits_with_budget(1.0), WidenPolicyParams(enabled=True)
        )
        assert not decision.would_decline


# ---------------------------------------------------------------------------
# (5) Empty / tiny book -> ~zero adders.
# ---------------------------------------------------------------------------


class TestEmptyTinyBook:
    def test_empty_book_profile_is_neutral(self) -> None:
        profile = build_peak_profile([], MARGINALS, None, CFG, k=5, input_generation=7)
        assert profile.by_game == {}
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        peaked = skew_for(candidate, [], profile=profile)
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game == ((GAME, 0, 0.0, "no_peak_profile"),)

    def test_tiny_book_rounds_to_zero(self) -> None:
        # Book premium $0.30 vs a $100 budget: ratio 0.003, gamma 2 -> the
        # widen term rounds to 0 (small book => ~no effect, by construction).
        book = committed_book(held_contracts=100)
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        peaked = skew_for(
            candidate, book, profile=profile, limits=limits_with_budget(100.0)
        )
        assert peaked.peak_cc == 0
        assert peaked.peak_per_game[0][3] == "peak_hit"  # evaluated, just ~0


# ---------------------------------------------------------------------------
# (6) Generation-stamped cache: stale -> neutral; matching -> active.
# ---------------------------------------------------------------------------


class TestGenerationCache:
    def test_stale_profile_is_neutral(self) -> None:
        book = committed_book()
        profile = profile_for(book, generation=7)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        stale = skew_for(candidate, book, profile=profile, generation=8)
        assert stale.peak_cc == 0
        assert stale.peak_per_game == (("*", 0, 0.0, "stale_profile"),)
        fresh = skew_for(candidate, book, profile=profile, generation=7)
        assert fresh.peak_cc > 0

    def test_unstamped_profile_fails_generation_match_closed(self) -> None:
        book = committed_book()
        profile = profile_for(book, generation=-1)  # un-stamped default
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        peaked = skew_for(candidate, book, profile=profile, generation=0)
        assert peaked.peak_cc == 0

    def test_missing_live_generation_is_neutral(self) -> None:
        book = committed_book()
        profile = profile_for(book, generation=7)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        peaked = skew_for(candidate, book, profile=profile, generation=None)
        assert peaked.peak_cc == 0

    def test_rebuilt_profile_at_new_generation_reactivates(self) -> None:
        book = committed_book()
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        rebuilt = profile_for(book, generation=8)
        peaked = skew_for(candidate, book, profile=rebuilt, generation=8)
        assert peaked.peak_cc > 0


# ---------------------------------------------------------------------------
# (7) Property: bounded by the composed clamp; pricing only (an int, never a
#     refusal); concentration/offset halves stay non-negative.
# ---------------------------------------------------------------------------

_PROP_BOOK = committed_book()
_PROP_PROFILE = profile_for(_PROP_BOOK)
_PROP_SNAP = snapshot_of(_PROP_BOOK)
_SHAPES: dict[str, tuple[LegRef, ...]] = {
    "stack": STACK_LEGS,
    "anti": ANTI_LEGS,
    "corners": (LegRef(CORN, CORN_EV, "yes"),),
    "cross": (LegRef(ARG_ADV, ADV_EV, "yes"),),
    "mixed": (LegRef(FRA_ML, ML_EV, "yes"), LegRef(CORN, CORN_EV, "yes")),
}


class TestPropertyBounds:
    @settings(derandomize=True, max_examples=250, deadline=None)
    @given(
        cand=st.integers(min_value=1, max_value=1_000_000),
        shape=st.sampled_from(sorted(_SHAPES)),
        budget=st.floats(min_value=0.0, max_value=1_000.0),
        widen_cap=st.integers(min_value=0, max_value=1_200),
        tighten_cap=st.integers(min_value=0, max_value=400),
        peak_widen=st.integers(min_value=0, max_value=1_200),
        peak_tighten=st.integers(min_value=0, max_value=400),
        gamma=st.floats(min_value=0.5, max_value=4.0),
        generation=st.sampled_from([None, 6, 7]),
    )
    def test_composed_clamp_always_holds(
        self,
        cand: int,
        shape: str,
        budget: float,
        widen_cap: int,
        tighten_cap: int,
        peak_widen: int,
        peak_tighten: int,
        gamma: float,
        generation: int | None,
    ) -> None:
        params = SkewParams(
            enabled=True,
            gamma=gamma,
            skew_max_widen_cc=widen_cap,
            skew_max_tighten_cc=tighten_cap,
            peak_widen_max_cc=peak_widen,
            peak_tighten_max_cc=peak_tighten,
        )
        candidate = no_position("cand", _SHAPES[shape], contracts=cand)
        skew = compute_inventory_skew(
            candidate,
            _PROP_SNAP,
            provider(MARGINALS),
            CONVENTIONS,
            limits_with_budget(budget),
            params,
            peak_profile=_PROP_PROFILE,
            peak_book_generation=generation,
        )
        # The documented overall clamp: each addend bounded by its own cap.
        assert -(tighten_cap + peak_tighten) <= skew.skew_cc <= widen_cap + peak_widen
        assert -peak_tighten <= skew.peak_cc <= peak_widen
        assert skew.peak_widen_cc >= 0 and skew.peak_tighten_cc >= 0
        # Pricing only: the result is an int adder, and the applied value is
        # its bounded negation — there is no refusal channel in this type.
        assert isinstance(skew.skew_cc, int)
        assert skew.applied_cc == -skew.skew_cc


# ---------------------------------------------------------------------------
# (8) End-to-end through the REAL construct_quote (the public quoting path the
#     existing skew tests use): peak-stacker prices wider than a fresh book.
# ---------------------------------------------------------------------------

_SCHEDULE = FeeSchedule.from_strings("0.07", "0.0175")
_TAKER_FEES = FeeModel(_SCHEDULE, DOC_ASSUMED)


def _deci_grid() -> PriceGrid:
    return PriceGrid.from_market_payload(
        {
            "ticker": "T",
            "price_ranges": [{"start": "0.001", "end": "0.999", "step": "0.001"}],
        }
    )


def _implied_yes_ask_cc(skew_applied_cc: int) -> int:
    """Price through the REAL construct_quote at the given applied skew and
    return the implied YES ask ($1 − no_bid) — same helper as test_skew."""
    quote = construct_quote(
        joint=JointEstimate(
            p=0.30, uncertainty=0.0, frechet_lo=0.0, frechet_hi=1.0, notes=()
        ),
        n_legs=2,
        qty=Q(10_000),
        grid=_deci_grid(),
        fee_model=_TAKER_FEES,
        fee_type=FeeType.QUADRATIC,
        fee_multiplier=Fraction(1),
        time_to_close_s=48 * 3600.0,
        in_play=False,
        yes_cap_cc=CC(9_900),
        no_cap_cc=CC(9_900),
        inventory_skew_cc=skew_applied_cc,
    )
    assert isinstance(quote, ConstructedQuote)  # never a NoQuote from the adder
    return CC_PER_DOLLAR - int(quote.no_bid_cc)


class TestEndToEnd:
    def test_peak_stacker_prices_wider_than_fresh_book(self) -> None:
        # Candidate sized so the peak component spans the 10cc test grid step
        # (premium $15 -> overlap 0.3 -> widen 600 x 0.3 x 0.36 = 65cc).
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=5_000)
        fresh = skew_for(candidate, [], profile=None, generation=None)
        assert fresh.skew_cc == 0  # empty book: no directional, no peak
        stacked = skew_for(candidate, book, profile=profile)
        assert stacked.applied_cc < fresh.applied_cc  # widen enters negative
        fresh_ask = _implied_yes_ask_cc(fresh.applied_cc)
        stacked_ask = _implied_yes_ask_cc(stacked.applied_cc)
        assert stacked_ask > fresh_ask  # dearer combo — we sell LESS of the peak

    def test_peak_component_alone_moves_the_quote(self) -> None:
        # Same book, same candidate, only the profile differs: the ask with
        # the profile is strictly higher — the delta is PURELY the peak steer.
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=5_000)
        without = skew_for(candidate, book, profile=None)
        with_peak = skew_for(candidate, book, profile=profile)
        assert with_peak.peak_cc > 0
        assert _implied_yes_ask_cc(with_peak.applied_cc) > _implied_yes_ask_cc(
            without.applied_cc
        )

    def test_anti_peak_prices_tighter(self) -> None:
        # Sized past the grid step: rebate 150 x 0.3 x 0.6 = 27cc.
        book = committed_book()
        profile = profile_for(book)
        anti = no_position("cand", ANTI_LEGS, contracts=5_000)
        without = skew_for(anti, book, profile=None)
        with_peak = skew_for(anti, book, profile=profile)
        assert with_peak.peak_cc < 0
        assert _implied_yes_ask_cc(with_peak.applied_cc) < _implied_yes_ask_cc(
            without.applied_cc
        )

    def test_max_composed_widen_still_quotes(self) -> None:
        # Property (7) end-to-end corollary: even the maximum composed widen
        # (default caps: 600 + 600 = 12c) still produces a QUOTE on a normal
        # parlay — pricing, never a de-facto block.
        assert _implied_yes_ask_cc(-1_200) > _implied_yes_ask_cc(0)

    def test_dark_ship_gates_peak_too(self) -> None:
        # SkewConfig.enabled False (dark seam): the peak component is computed
        # and logged like the rest of the classifier but applied_cc is 0.
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        dark = skew_for(
            candidate, book, profile=profile, params=SkewParams(enabled=False)
        )
        assert dark.peak_cc > 0        # honest classifier still carries it
        assert dark.applied_cc == 0    # ...but dark applies nothing


# ---------------------------------------------------------------------------
# Profile builder anchors (waiver parity + branches + fail-safe omission).
# ---------------------------------------------------------------------------


class TestProfileBuilder:
    def test_top_loss_matches_certified_state_worst_case(self) -> None:
        # The profile's worst cached loss must equal the CERTIFIED
        # state-consistent worst case of the SAME committed-only book — the
        # waiver machinery is the one source of state truth (hard rule 8c).
        book = committed_book() + [
            no_position("h2", (LegRef(FRA_ML, ML_EV, "yes"),), contracts=4_000),
            no_position("h3", (LegRef(TOT3, TOT_EV, "no"),), contracts=3_000),
        ]
        profile = profile_for(book)
        entities = [entity_from_position(p) for p in book]
        certified = state_worst_case_by_game(entities, [], MARGINALS, None, CFG)[GAME]
        assert certified.certified
        assert profile.by_game[GAME].top_loss_cc == certified.worst_case_cc

    def test_top_k_counts_and_ordering(self) -> None:
        book = committed_book()
        for k in (1, 3, 5):
            profile = profile_for(book, k=k)
            gp = profile.by_game[GAME]
            assert gp.n_peak_states == k
            losses = [loss for sl in gp.slices for loss in sl.losses_cc]
            assert max(losses) == gp.top_loss_cc

    def test_advance_game_covers_both_shootout_branches(self) -> None:
        # Opposing advance holdings: each branch of a level state settles one
        # side, so with enough K the cached peaks span BOTH branches.
        book = [
            no_position("arg", (LegRef(ARG_ADV, ADV_EV, "yes"),)),
            no_position("eng", (LegRef(ENG_ADV, ADV_EV, "yes"),)),
        ]
        profile = build_peak_profile(
            book, MARGINALS, None, CFG, k=8, input_generation=7
        )
        gp = profile.by_game[GAME_B]
        assert gp.n_peak_states == 8
        assert gp.n_states_enumerated == 1586  # branch-doubled enumeration

    def test_uncertifiable_game_omitted(self) -> None:
        # A corners-only book has no structural plan -> the game is ABSENT
        # (the neutral branch), never a guessed profile.
        book = [no_position("held", (LegRef(CORN, CORN_EV, "yes"),))]
        profile = profile_for(book)
        assert profile.by_game == {}

    def test_generation_and_knockout_series_stamped(self) -> None:
        profile = profile_for(committed_book(), generation=41)
        assert profile.input_generation == 41
        assert profile.knockout_series == tuple(CFG.knockout_series)

    def test_containment_direct(self) -> None:
        profile = profile_for(committed_book())
        hit = evaluate_peak_containment(profile, GAME, list(STACK_LEGS))
        assert hit is not None and hit.hit_severity == 1.0
        miss = evaluate_peak_containment(profile, GAME, list(ANTI_LEGS))
        assert miss is not None and miss.provably_misses_all
        assert evaluate_peak_containment(profile, "NOPE", list(STACK_LEGS)) is None
