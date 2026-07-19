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

MULTI-CLUSTER sections (operator directive 2026-07-19, classes TestMultiCluster*
/ TestSingleClusterByteIdentity / TestClusterStateCapOverflow /
TestClusterIdentification / TestPerQuoteCost): the live ESPARG two-cluster
shape (a second correlated loss cluster on a mutually exclusive branch rode
free — even collected the rebate — under single-plateau pricing), the exact
cluster-ratio widen arithmetic, the strictly-tighter all-clusters rebate
certification, peak_n_clusters=1 byte-identity to the 2026-07-18 ship,
shared-state-cap drop-lowest-first overflow, config knob validation/wiring,
and the per-quote evaluate cost measurement.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from fractions import Fraction

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import combomaker.sim.peak_profile as peak_profile_mod
from combomaker.core.conventions import DOC_ASSUMED, Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.grid import PriceGrid
from combomaker.ops.config import RiskConfig, SkewConfig
from combomaker.ops.quote_app import build_lifecycle_config
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import ConstructedQuote, construct_quote
from combomaker.rfq.lifecycle import LifecycleConfig
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
    GamePeakProfile,
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
    FRA_ML: 0.45, ESP_ML: 0.35, TOT3: 0.60, CORN: 0.50, H1TOT1: 0.65,
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
    positions: list[OpenPosition],
    *,
    k: int = 5,
    generation: int = 7,
    n_clusters: int = 3,
) -> PeakProfile:
    return build_peak_profile(
        positions,
        MARGINALS,
        None,
        CFG,
        k=k,
        n_clusters=n_clusters,
        input_generation=generation,
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
        # MAGNITUDE RECALIBRATION (operator directive 2026-07-19 evening): the
        # candidate-size factor is GONE — the per-contract price reflects
        # WHERE the risk lands, never the clip size (size is the caps'/
        # velocity brake's job). Book premium $30 -> peak 300_000cc; budget
        # $50 -> peak_ratio 0.6; severity 1.0 (hits the worst state).
        # widen = 600 x 1.0 x 0.6**2 = 216cc (~2.2c; was 13cc pre-recal).
        book = committed_book()
        profile = profile_for(book)
        candidate = no_position("cand", STACK_LEGS, contracts=1_000)
        peaked = skew_for(candidate, book, profile=profile)
        assert peaked.peak_cc == 216
        assert peaked.peak_per_game == ((GAME, 216, 1.0, "peak_hit"),)
        # Size-independence pin: a 10x clip pays the SAME per-contract price.
        big = no_position("cand", STACK_LEGS, contracts=10_000)
        assert skew_for(big, book, profile=profile).peak_cc == 216

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
        # ratios 0.15/0.3/0.6/0.9/1.0, severity 1.0, gamma 2: 600 x ratio**2.
        assert adders == [14, 54, 216, 486, 600]

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
        # Recalibrated rebate = 150 x peak_ratio 0.6 = 90cc (~0.9c tighter —
        # win the flattening auction; was 5cc pre-recal). Row factor = ratio.
        assert peaked.peak_per_game == ((GAME, -90, 0.6, "peak_miss_rebate"),)

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
        # full premium, so any rebate is unsound. MULTI-CLUSTER (2026-07-19):
        # at n_clusters >= 2 the top plateau is cluster 1 of the severity
        # walk, so the stacker is now WIDENED at severity 1.0 (strictly
        # better than the old neutral); n_clusters=1 pins the legacy neutral.
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
        assert skew.peak_cc > 0                             # widened (cluster 1)
        reasons = {row[3] for row in skew.peak_per_game if row[0] == GAME_B}
        assert reasons == {"peak_hit"}                      # not peak_miss_rebate
        # LEGACY PIN (peak_n_clusters=1 — the 2026-07-18 single-plateau ship):
        # the same stacker graded NEUTRAL (no rebate, but no widen either).
        legacy = profile_for(book, n_clusters=1)
        skew1 = self._skew(stacker, book, legacy)
        assert skew1.peak_tighten_cc == 0 and skew1.peak_cc == 0
        assert {r[3] for r in skew1.peak_per_game if r[0] == GAME_B} == {"neutral"}
        # Acknowledge the money fact the old rebate ignored: the candidate
        # RAISES the certified state-consistent worst case by its premium.
        entities = [entity_from_position(p) for p in book + [stacker]]
        with_cand = state_worst_case_by_game(entities, [], MARGINALS, None, CFG)[GAME_B]
        assert with_cand.certified
        assert with_cand.worst_case_cc == 520_000 + stacker.max_loss_cc

    def test_probe_b_plateau_stacker_btts_gets_no_rebate(self) -> None:
        # Same shape via BTTS: cached corner rows are all "ENG 0 - ARG k"
        # (BTTS-yes provably misses there) but plateau states where both
        # score and ARG advances exist -> never a rebate; multi-cluster
        # (n_clusters >= 2) widens it via cluster 1, n_clusters=1 = neutral.
        book = one_way_advance_book()
        profile = profile_for(book)
        stacker = no_position(
            "cand",
            (LegRef(ARG_ADV, ADV_EV, "yes"), LegRef(BTTSB, BTTSB_EV, "yes")),
            contracts=1_000,
        )
        skew = self._skew(stacker, book, profile)
        assert skew.peak_tighten_cc == 0
        assert skew.peak_cc > 0
        reasons = {row[3] for row in skew.peak_per_game if row[0] == GAME_B}
        assert reasons == {"peak_hit"}
        legacy = profile_for(book, n_clusters=1)
        skew1 = self._skew(stacker, book, legacy)
        assert skew1.peak_tighten_cc == 0 and skew1.peak_cc == 0
        assert {r[3] for r in skew1.peak_per_game if r[0] == GAME_B} == {"neutral"}

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
        # ratio 1 (book peak >= budget), severity 1 -> the raw term IS the
        # cap; never above it.
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

    def test_composed_tighten_at_armed_300_cap_survives_quote_clamps(self) -> None:
        # BOUNDARY RE-CHECK (2026-07-19 recalibration): the operator plans to
        # arm peak_tighten_max_cc=300. Composed tighten extreme = directional
        # -150 + peak -300 = classifier -450 -> applied +450 (the
        # no_bid-RAISING, free-money-dangerous direction, 4.5c toward the
        # taker). Through the REAL construct_quote the free-money/min-capture
        # clamps must keep the quote VALID and CAPTURE-POSITIVE.
        params300 = SkewParams(enabled=True, peak_tighten_max_cc=300)
        book = committed_book(held_contracts=200_000)
        profile = profile_for(book)
        anti = no_position(
            "cand",
            (LegRef(FRA_ML, ML_EV, "no"), LegRef(TOT3, TOT_EV, "no")),
            contracts=150_000,
        )
        limits = SkewLimits(
            max_event_delta_contracts=10.0,
            max_event_worst_case_loss_dollars=1.0,
            max_event_gross_notional_dollars=1e12,
        )
        s = skew_for(anti, book, profile=profile, limits=limits, params=params300)
        assert s.peak_cc == -300           # 300 x peak_ratio 1.0, at its clamp
        assert s.skew_cc == -450
        assert s.applied_cc == 450
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
            inventory_skew_cc=s.applied_cc,
        )
        assert isinstance(quote, ConstructedQuote)  # a QUOTE, never a decline
        # Capture invariant survives: yes_bid + no_bid <= $1 - min_capture.
        assert int(quote.yes_bid_cc) + int(quote.no_bid_cc) <= CC_PER_DOLLAR - 100
        # And it IS tighter (cheaper implied YES ask) than the unskewed quote.
        assert _implied_yes_ask_cc(450) < _implied_yes_ask_cc(0)

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
        # Recalibrated widen = 600 x 1.0 x 0.36 = 216cc — size-independent,
        # comfortably past the 10cc test grid step.
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
        # Recalibrated rebate = 150 x peak_ratio 0.6 = 90cc — past the grid step.
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


# ===========================================================================
# MULTI-CLUSTER peak steer (operator directive 2026-07-19 — the live ESPARG
# two-cluster book: only the argmax plateau was cached, so the second
# correlated loss cluster on a mutually exclusive branch rode nearly free,
# even collecting the anti-peak rebate for provably missing cluster A).
# ===========================================================================

# --- fixture: the live two-cluster shape on GAME (FRA/ESP, default grid) ----
# Cluster A (top plateau): {FRA wins & over 2.5} — the goal-fest pile.
# Cluster B (mutually exclusive): {ESP wins} at ~60% of the top loss — the
# live "ARG-champ ladder" analog (same one-way taker stacking one outcome).
#   p1 = NO {FRA & over 2.5} 100ct @ $0.90 -> hit +900k, miss -100k
#   p2 = NO {ESP wins}        70ct @ $0.80 -> hit +560k, miss -140k
# Levels: A = 900k - 140k = 760_000; B = 560k - 100k = 460_000 (= 23/38 of
# the top ~ 0.605); everything else -240_000.


def two_cluster_book() -> list[OpenPosition]:
    return [
        no_position("p1", STACK_LEGS, contracts=10_000, entry_price=9_000),
        no_position(
            "p2",
            (LegRef(ESP_ML, ML_EV, "yes"),),
            contracts=7_000,
            entry_price=8_000,
        ),
    ]


# --- fixture: a THREE-cluster book (adds {FRA & under 2.5} as cluster C) ----
#   p3 = NO {FRA & under 2.5} 60ct @ $0.80 -> hit +480k, miss -120k
# Levels: A = 900-140-120 = 640_000; B = -100+560-120 = 340_000 (0.53125);
# C = -100-140+480 = 240_000 (0.375); draws -360_000. min_frac 0.30 x 640k =
# 192k, so B and C both qualify -> top plateau + 2 lower clusters.
UNDER_LEGS = (LegRef(FRA_ML, ML_EV, "yes"), LegRef(TOT3, TOT_EV, "no"))


def three_cluster_book() -> list[OpenPosition]:
    return [
        no_position("p1", STACK_LEGS, contracts=10_000, entry_price=9_000),
        no_position(
            "p2",
            (LegRef(ESP_ML, ML_EV, "yes"),),
            contracts=7_000,
            entry_price=8_000,
        ),
        no_position("p3", UNDER_LEGS, contracts=6_000, entry_price=8_000),
    ]


B_LEGS = (LegRef(ESP_ML, ML_EV, "yes"),)
LIMITS_95 = limits_with_budget(95.0)  # two-cluster book: peak_ratio exactly 0.8
LIMITS_80 = limits_with_budget(80.0)  # three-cluster book: peak_ratio exactly 0.8


def _cluster_sizes(gp: GamePeakProfile) -> list[int]:
    return [sum(int(s.states.w.size) for s in c.slices) for c in gp.lower_clusters]


# ---------------------------------------------------------------------------
# (M1) THE LIVE SHAPE: a B-stacking candidate is widened ~ (B loss / top
# loss) x the A-widen; pre-fix (peak_n_clusters=1) it rode
# free — it even collected the anti-peak rebate for provably missing A.
# ---------------------------------------------------------------------------


class TestMultiClusterLiveShape:
    def test_profile_caches_cluster_b(self) -> None:
        profile = profile_for(two_cluster_book())
        gp = profile.by_game[GAME]
        assert gp.top_loss_cc == 760_000
        assert gp.n_clusters == 3
        assert [c.loss_cc for c in gp.lower_clusters] == [460_000]
        # Shared cap accounting: plateau + cluster states all cached.
        assert gp.n_plateau_states + gp.n_lower_cluster_states <= 4096
        assert gp.n_lower_cluster_states > 0

    def test_b_stacker_widened_at_cluster_loss_ratio(self) -> None:
        # EXACT ARITHMETIC (2026-07-19 recalibration — no candidate-size
        # factor): budget $95 -> peak_ratio 760k/950k = 0.8; gamma 2.
        #   A-stacker: severity 1.0     -> 600 x 1.0   x 0.64 = 384cc
        #   B-stacker: severity 23/38   -> 600 x 23/38 x 0.64 = 232.42 -> 232cc
        # i.e. the B-widen is ~0.6053 x the A-widen (the cluster loss ratio),
        # and BOTH are real cents now (~3.8c / ~2.3c at ratio 0.8).
        book = two_cluster_book()
        profile = profile_for(book)
        a_st = no_position("ca", STACK_LEGS, contracts=1_000, entry_price=7_600)
        b_st = no_position("cb", B_LEGS, contracts=1_000, entry_price=7_600)
        s_a = skew_for(a_st, book, profile=profile, limits=LIMITS_95)
        s_b = skew_for(b_st, book, profile=profile, limits=LIMITS_95)
        assert s_a.peak_cc == 384
        assert s_a.peak_per_game == ((GAME, 384, 1.0, "peak_hit"),)
        assert s_b.peak_cc == 232
        assert s_b.peak_per_game == ((GAME, 232, 0.605263, "peak_hit"),)
        # Additive composition with the directional classifier is untouched.
        base_b = skew_for(b_st, book, profile=None, limits=LIMITS_95)
        assert s_b.skew_cc == base_b.skew_cc + s_b.peak_cc
        # Containment severity is EXACTLY the cluster loss ratio.
        c = evaluate_peak_containment(profile, GAME, list(B_LEGS))
        assert c is not None and c.hit_severity == 460_000 / 760_000

    def test_b_stacker_with_nonstructural_prop_rider_same_widen(self) -> None:
        # The live ladder carries a player-prop rider (Messi analog): a
        # non-structural leg is ADVERSARIAL (assumed hit) and cannot dilute
        # the cluster charge — same widen as the pure B-stacker. And the
        # price is CLIP-SIZE independent (a $15 rung and a $150 rung pay the
        # same per-contract widen — the live under-pricing was exactly the
        # old size factor zeroing realistic clips).
        book = two_cluster_book()
        profile = profile_for(book)
        for contracts in (1_000, 10_000):
            b_prop = no_position(
                "cb",
                (LegRef(ESP_ML, ML_EV, "yes"), LegRef(CORN, CORN_EV, "yes")),
                contracts=contracts,
                entry_price=7_600,
            )
            s = skew_for(b_prop, book, profile=profile, limits=LIMITS_95)
            assert s.peak_cc == 232

    def test_prefix_n1_b_stacker_rode_free_and_collected_rebate(self) -> None:
        # THE REGRESSION PIN (peak_n_clusters=1 == the single-plateau cluster
        # view): the B-stacker missed every cached row AND provably missed the
        # whole A plateau (mutually exclusive branch), so it not only paid no
        # widen — it collected the anti-peak rebate (recalibrated magnitude:
        # 150 x peak_ratio 0.8 = 120cc BACK). Multi-cluster (n=3) flips the
        # same candidate to a +232cc widen — the live fix.
        book = two_cluster_book()
        legacy = profile_for(book, n_clusters=1)
        assert legacy.by_game[GAME].lower_clusters == ()
        b_st = no_position("cb", B_LEGS, contracts=1_000, entry_price=7_600)
        s_b = skew_for(b_st, book, profile=legacy, limits=LIMITS_95)
        assert s_b.peak_cc == -120
        assert s_b.peak_per_game == ((GAME, -120, 0.8, "peak_miss_rebate"),)

    def test_three_cluster_severity_ladder(self) -> None:
        # Three clusters price at their exact loss ratios (budget $80 ->
        # ratio 640k/800k = 0.8, ratio**2 = 0.64; gamma 2):
        #   A: 600 x 1.0     x 0.64 = 384;  B: 600 x 0.53125 x 0.64 = 204
        #   C: 600 x 0.375   x 0.64 = 144
        book = three_cluster_book()
        profile = profile_for(book)
        gp = profile.by_game[GAME]
        assert gp.top_loss_cc == 640_000
        assert [c.loss_cc for c in gp.lower_clusters] == [340_000, 240_000]
        expected = {"A": (STACK_LEGS, 384), "B": (B_LEGS, 204), "C": (UNDER_LEGS, 144)}
        for _name, (legs, cc) in expected.items():
            cand = no_position("c", legs, contracts=2_000, entry_price=6_400)
            s = skew_for(cand, book, profile=profile, limits=LIMITS_80)
            assert s.peak_cc == cc
            assert [row[3] for row in s.peak_per_game] == ["peak_hit"]


# ---------------------------------------------------------------------------
# (M2) REBATE: missing A but hitting B => NO rebate (pre-fix it rebated);
# provably missing A AND B => rebate survives (strictly tighter cert).
# ---------------------------------------------------------------------------


class TestMultiClusterRebate:
    def test_miss_a_hit_b_gets_no_rebate(self) -> None:
        book = two_cluster_book()
        profile = profile_for(book)
        b_st = no_position("cb", B_LEGS, contracts=1_000, entry_price=7_600)
        s = skew_for(b_st, book, profile=profile, limits=LIMITS_95)
        assert s.peak_tighten_cc == 0                      # the new no-rebate
        assert s.peak_cc > 0                               # it is cluster flow
        c = evaluate_peak_containment(profile, GAME, list(B_LEGS))
        assert c is not None and not c.provably_misses_all
        # Pre-fix pin: n_clusters=1 granted the rebate to the same candidate
        # (recalibrated magnitude: 150 x 0.8 = 120cc).
        legacy = profile_for(book, n_clusters=1)
        s1 = skew_for(b_st, book, profile=legacy, limits=LIMITS_95)
        assert s1.peak_tighten_cc > 0 and s1.peak_cc == -120

    def test_miss_a_and_b_still_rebates(self) -> None:
        # {FRA & under 2.5} provably misses EVERY A state (all over) and EVERY
        # B state (all ESP-win): genuine flattening flow keeps the rebate
        # under the all-clusters certification — recalibrated to real cents:
        # 150 x peak_ratio 0.8 = 120cc (~1.2c tighter, wins its auction).
        book = two_cluster_book()
        profile = profile_for(book)
        flat = no_position("cf", UNDER_LEGS, contracts=1_000, entry_price=7_600)
        s = skew_for(flat, book, profile=profile, limits=LIMITS_95)
        assert s.peak_cc == -120
        assert s.peak_per_game == ((GAME, -120, 0.8, "peak_miss_rebate"),)
        c = evaluate_peak_containment(profile, GAME, list(UNDER_LEGS))
        assert c is not None and c.provably_misses_all and c.hit_severity == 0.0


# ---------------------------------------------------------------------------
# (M3) peak_n_clusters=1 => byte-identical to the 2026-07-18 single-plateau
# behaviour (property over the existing test fixtures).
# ---------------------------------------------------------------------------


def _assert_states_equal(a: object, b: object) -> None:
    for fld in ("w", "a90", "b90", "a_et", "b_et", "a_1h", "b_1h"):
        assert np.array_equal(getattr(a, fld), getattr(b, fld))


def _assert_legacy_fields_identical(g1: GamePeakProfile, g3: GamePeakProfile) -> None:
    """The n=1 build must carry EXACTLY the legacy fields (K sample + full
    top plateau + top loss) the pre-multi-cluster builder produced — which the
    n>=2 build must also leave untouched (clusters are strictly additive)."""
    assert g1.top_loss_cc == g3.top_loss_cc
    assert g1.n_states_enumerated == g3.n_states_enumerated
    assert len(g1.slices) == len(g3.slices)
    for s1, s3 in zip(g1.slices, g3.slices, strict=True):
        assert s1.branch == s3.branch and s1.losses_cc == s3.losses_cc
        _assert_states_equal(s1.states, s3.states)
    assert (g1.plateau_slices is None) == (g3.plateau_slices is None)
    if g1.plateau_slices is not None and g3.plateau_slices is not None:
        assert len(g1.plateau_slices) == len(g3.plateau_slices)
        for p1, p3 in zip(g1.plateau_slices, g3.plateau_slices, strict=True):
            assert p1.branch == p3.branch
            _assert_states_equal(p1.states, p3.states)
    # And the n=1 profile carries NO cluster machinery at all.
    assert g1.lower_clusters == () and g1.n_clusters == 1


class TestSingleClusterByteIdentity:
    def test_builder_n1_legacy_fields_on_all_fixtures(self) -> None:
        fixtures: list[list[OpenPosition]] = [
            committed_book(),
            one_way_advance_book(),
            two_cluster_book(),
            three_cluster_book(),
        ]
        for book in fixtures:
            p1 = profile_for(book, n_clusters=1)
            p3 = profile_for(book, n_clusters=3)
            assert set(p1.by_game) == set(p3.by_game)
            for game in p1.by_game:
                _assert_legacy_fields_identical(p1.by_game[game], p3.by_game[game])

    def test_n1_reproduces_single_cluster_pinned_values(self) -> None:
        # Explicit n_clusters=1 magnitude pins (the recalibrated 2026-07-19
        # formula applies at EVERY n — n=1 rolls back the cluster VIEW, never
        # the formula): 216cc stacker widen, -90cc anti rebate, the
        # [14, 54, 216, 486, 600] budget ramp — identical to the n=3 values
        # on this single-cluster book.
        book = committed_book()
        legacy = profile_for(book, n_clusters=1)
        stack = no_position("cand", STACK_LEGS, contracts=1_000)
        s = skew_for(stack, book, profile=legacy)
        assert s.peak_cc == 216
        assert s.peak_per_game == ((GAME, 216, 1.0, "peak_hit"),)
        anti = no_position("cand", ANTI_LEGS, contracts=1_000)
        a = skew_for(anti, book, profile=legacy)
        assert a.peak_per_game == ((GAME, -90, 0.6, "peak_miss_rebate"),)
        adders = []
        for held in (2_500, 5_000, 10_000, 15_000, 25_000):
            b = committed_book(held_contracts=held)
            p = profile_for(b, n_clusters=1)
            adders.append(skew_for(stack, b, profile=p).peak_cc)
        assert adders == [14, 54, 216, 486, 600]

    def test_n1_equals_n3_on_single_cluster_book_shapes(self) -> None:
        # committed_book has ONE loss cluster; on the module's candidate
        # shapes the full InventorySkew is IDENTICAL between n=1 and n=3
        # profiles across sizes and budgets (multi-cluster is strictly about
        # additional clusters; a single-cluster book prices the same).
        book = committed_book()
        p1 = profile_for(book, n_clusters=1)
        p3 = profile_for(book, n_clusters=3)
        assert p3.by_game[GAME].lower_clusters == ()
        for shape in sorted(_SHAPES):
            for contracts in (1, 1_000, 5_000, 100_000, 900_000):
                for budget in (1.0, 50.0, 1_000.0):
                    cand = no_position("c", _SHAPES[shape], contracts=contracts)
                    s1 = skew_for(
                        cand, book, profile=p1, limits=limits_with_budget(budget)
                    )
                    s3 = skew_for(
                        cand, book, profile=p3, limits=limits_with_budget(budget)
                    )
                    assert s1 == s3, (shape, contracts, budget)


# ---------------------------------------------------------------------------
# (M4) Shared state-cap overflow: drop the LOWEST clusters first; a dropped
# cluster contributes NO widen (today's behaviour) and never crashes.
# ---------------------------------------------------------------------------


class TestClusterStateCapOverflow:
    def _sizes(self) -> tuple[int, list[int]]:
        gp = profile_for(three_cluster_book()).by_game[GAME]
        return gp.n_plateau_states, _cluster_sizes(gp)

    def test_drops_lowest_cluster_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plateau_n, sizes = self._sizes()
        assert len(sizes) == 2  # B and C both cached at the real cap
        # Cap fits plateau + B exactly -> C (the LOWEST cluster) is dropped.
        monkeypatch.setattr(
            peak_profile_mod, "_PLATEAU_CACHE_MAX_STATES", plateau_n + sizes[0]
        )
        book = three_cluster_book()
        profile = profile_for(book)
        gp = profile.by_game[GAME]
        assert [c.loss_cc for c in gp.lower_clusters] == [340_000]  # C gone
        assert gp.plateau_slices is not None                        # top intact
        # Dropped cluster => NO widen from it: the C-stacker provably misses
        # every CACHED cluster (A and B) and rides exactly like the
        # single-plateau view (the rebate path) — no crash, no widen born
        # from the dropped C. Recalibrated rebate = 150 x ratio 0.8 = 120cc.
        c_st = no_position("cc", UNDER_LEGS, contracts=2_000, entry_price=6_400)
        s = skew_for(c_st, book, profile=profile, limits=LIMITS_80)
        assert s.peak_cc == -120  # same as n_clusters=1 (uncached = neutral C)
        assert [row[3] for row in s.peak_per_game] == ["peak_miss_rebate"]
        # B (the higher cluster) kept its widen (600 x 0.53125 x 0.64 = 204).
        b_st = no_position("cb", B_LEGS, contracts=2_000, entry_price=6_400)
        assert skew_for(b_st, book, profile=profile, limits=LIMITS_80).peak_cc == 204

    def test_overflow_cascades_lowest_first_never_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plateau_n, sizes = self._sizes()
        # One state short of fitting B: B is dropped AND C with it (drop-
        # lowest-first is a cascade — never "skip B, keep the smaller C").
        monkeypatch.setattr(
            peak_profile_mod, "_PLATEAU_CACHE_MAX_STATES", plateau_n + sizes[0] - 1
        )
        gp = profile_for(three_cluster_book()).by_game[GAME]
        assert gp.lower_clusters == ()
        assert gp.plateau_slices is not None  # the top plateau always survives

    def test_plateau_overflow_caches_nothing_no_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plateau_n, _sizes = self._sizes()
        monkeypatch.setattr(
            peak_profile_mod, "_PLATEAU_CACHE_MAX_STATES", plateau_n - 1
        )
        book = three_cluster_book()
        profile = profile_for(book)
        gp = profile.by_game[GAME]
        assert gp.plateau_slices is None and gp.lower_clusters == ()
        # No rebate (uncertifiable), no cluster widen; the K-sample widen path
        # still works (fail-safe neutral everywhere else, never a crash).
        b_st = no_position("cb", B_LEGS, contracts=2_000, entry_price=6_400)
        s_b = skew_for(b_st, book, profile=profile, limits=LIMITS_80)
        assert s_b.peak_cc == 0
        a_st = no_position("ca", STACK_LEGS, contracts=2_000, entry_price=6_400)
        assert skew_for(a_st, book, profile=profile, limits=LIMITS_80).peak_cc == 384


# ---------------------------------------------------------------------------
# (M5) Cluster identification rule details + config knobs.
# ---------------------------------------------------------------------------


class TestClusterIdentification:
    def test_min_frac_threshold_is_exact_and_inclusive(self) -> None:
        book = two_cluster_book()  # B/top = 460/760 = 23/38 exactly
        at = build_peak_profile(
            book, MARGINALS, None, CFG, k=5,
            n_clusters=3, cluster_min_frac=Fraction(23, 38), input_generation=7,
        )
        assert [c.loss_cc for c in at.by_game[GAME].lower_clusters] == [460_000]
        above = build_peak_profile(
            book, MARGINALS, None, CFG, k=5,
            n_clusters=3, cluster_min_frac=Fraction(61, 100), input_generation=7,
        )
        assert above.by_game[GAME].lower_clusters == ()

    def test_n_clusters_two_keeps_only_the_highest_lower_cluster(self) -> None:
        gp = profile_for(three_cluster_book(), n_clusters=2).by_game[GAME]
        assert [c.loss_cc for c in gp.lower_clusters] == [340_000]

    def test_negative_and_subthreshold_levels_never_cluster(self) -> None:
        # committed_book: single positive level (300k) + the profit level
        # (-700k): nothing below the plateau qualifies at ANY min_frac.
        gp = profile_for(committed_book()).by_game[GAME]
        assert gp.lower_clusters == ()


class TestMultiClusterConfigKnobs:
    def test_defaults_parity_and_builder(self) -> None:
        assert SkewConfig().peak_n_clusters == 3
        assert SkewConfig().peak_cluster_min_frac == "0.30"
        assert LifecycleConfig().peak_n_clusters == 3
        assert LifecycleConfig().peak_cluster_min_frac == "0.30"
        cfg = build_lifecycle_config(RiskConfig())
        assert cfg.peak_n_clusters == 3
        assert cfg.peak_cluster_min_frac == "0.30"

    def test_pass_through_reaches_lifecycle_config(self) -> None:
        cfg = build_lifecycle_config(
            RiskConfig(), peak_n_clusters=1, peak_cluster_min_frac="0.55"
        )
        assert cfg.peak_n_clusters == 1
        assert cfg.peak_cluster_min_frac == "0.55"

    @pytest.mark.parametrize("good", [1, 3, 8])
    def test_n_clusters_accepts(self, good: int) -> None:
        assert SkewConfig(peak_n_clusters=good).peak_n_clusters == good

    @pytest.mark.parametrize("bad", [0, 9, -1])
    def test_n_clusters_rejects(self, bad: int) -> None:
        with pytest.raises(ValidationError):
            SkewConfig(peak_n_clusters=bad)

    @pytest.mark.parametrize("good", ["0.30", "1", "0.999", "0.01"])
    def test_min_frac_accepts(self, good: str) -> None:
        assert SkewConfig(peak_cluster_min_frac=good).peak_cluster_min_frac == good

    @pytest.mark.parametrize(
        "bad", ["0", "1.5", "-0.3", "abc", "NaN", "Infinity", ""]
    )
    def test_min_frac_rejects(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            SkewConfig(peak_cluster_min_frac=bad)


# ---------------------------------------------------------------------------
# (M6) Per-quote evaluate cost on a 3-cluster profile (must stay well under
# 1ms; the measured number is reported by the test output).
# ---------------------------------------------------------------------------


class TestPerQuoteCost:
    def test_three_cluster_evaluate_cost(self) -> None:
        book = three_cluster_book()
        profile = profile_for(book)
        gp = profile.by_game[GAME]
        assert len(gp.lower_clusters) == 2  # a true 3-cluster profile
        shapes = [list(STACK_LEGS), list(B_LEGS), list(UNDER_LEGS)]
        for legs in shapes:  # warm-up (imports, numpy dispatch)
            assert evaluate_peak_containment(profile, GAME, legs) is not None
        n = 200
        t0 = time.perf_counter()
        for _ in range(n):
            for legs in shapes:
                evaluate_peak_containment(profile, GAME, legs)
        eval_ms = (time.perf_counter() - t0) * 1_000.0 / (n * len(shapes))
        # Full skew (directional + peak) per quote, for the composed number.
        snap = snapshot_of(book)
        cands = [
            no_position("c", tuple(legs), contracts=2_000, entry_price=6_400)
            for legs in shapes
        ]
        t0 = time.perf_counter()
        for _ in range(n):
            for cand in cands:
                compute_inventory_skew(
                    cand, snap, provider(MARGINALS), CONVENTIONS, LIMITS_80,
                    PARAMS, peak_profile=profile, peak_book_generation=7,
                )
        skew_ms = (time.perf_counter() - t0) * 1_000.0 / (n * len(cands))
        print(
            f"\n[multi-cluster cost] evaluate_peak_containment: {eval_ms:.4f} "
            f"ms/call; compute_inventory_skew (directional+peak): "
            f"{skew_ms:.4f} ms/call (3-cluster profile)"
        )
        assert eval_ms < 1.0  # per-quote budget: well under 1ms
        assert skew_ms < 2.0  # generous CI bound; report the printed number
