"""P0-7 PREFERRED — condition fallback copula legs on the game's shared structural
factor IN THE PRODUCTION SAMPLE (an upgrade from the interim worse-tail challenger).

The structural split used to sample a game's structural scoreline block and its
copula-only corners/cards block from INDEPENDENT rng, discarding same-game
structural↔copula dependence in the PRODUCTION sample. The preferred approach drives
each straddling copula leg's Gaussian latent from a CONSERVATIVE function of the
game's sampled scoreline intensity (a shared factor), so same-game covariance now
appears in production — captured directly in the tail, not only in a challenger.

This suite proves the mandated behaviours:
 1. Same-game corner/structural covariance now appears in the PRODUCTION sample
    (the raw split gave ~independence).
 2. The conditioning PRESERVES the copula leg's marginal exactly (a
    standard-normal-preserving rotation).
 3. The conditioned governing tail is >= the independent-split tail for a
    concentrated same-game book (conditioning may only fatten, never thin — the
    unconditioned-split guard is folded into the governing max).
 4. Ungamed / cross-game / group-format copula legs are UNCHANGED (no defensible
    link ⇒ 0 loading ⇒ independence + the worse-tail challenger backstop).
 5. A leg type with no defensible link (a group-format corners leg, or cards) is
    NOT conditioned and still routes to the full-copula challenger.

Deterministic seeds → reproducible MC. Probability space throughout (floats OK,
hard rule 5); the book P&L axis is float cc by the simulator's design.
"""
from __future__ import annotations

import numpy as np

from combomaker.sim.book_model import BookModel
from combomaker.sim.book_risk import (
    _build_conditioning,
    _copula_leg_loading,
    _select_sampler,
    compute_book_risk,
)
from combomaker.sim.engine import ComboPosition, LegModel
from combomaker.sim.structural_book import (
    StructuralConfigView,
    build_game_plans,
    sample_structural_values,
)

CFG = StructuralConfigView()

# Knockout game (ENGARG). Two structural advance legs + one copula-only TOTAL
# corners leg on the SAME game — the straddling case. Corners settle including ET,
# so the knockout corners leg carries the ONE defensible shared-factor loading.
_EV = "KXWCADVANCE-26JUL15ENGARG"
_ADV_ARG = "KXWCADVANCE-26JUL15ENGARG-ARG"
_ADV_ENG = "KXWCADVANCE-26JUL15ENGARG-ENG"
_CORNERS = "KXWCCORNERS-26JUL15ENGARG-9"
_CORNERS_EV = "KXWCCORNERS-26JUL15ENGARG"


def _mixed_model() -> BookModel:
    legs = (LegModel(p=0.55), LegModel(p=0.45), LegModel(p=0.40))
    positions = (ComboPosition((0, 2), "no", 50, 8000, leg_sides=("yes", "yes")),)
    corr = np.eye(3)
    leg_index = {_ADV_ARG: 0, _ADV_ENG: 1, _CORNERS: 2}
    event_by_index = {0: _EV, 1: _EV, 2: _CORNERS_EV}
    return BookModel(
        legs, positions, corr, corr.copy(), corr.copy(),
        leg_index, event_by_index, False,
    )


# --------------------------------------------------------------------------- #
# 1 + 2. Covariance appears in production AND the marginal is preserved.
# --------------------------------------------------------------------------- #


def test_conditioning_injects_covariance_and_preserves_marginal():
    """The conditioned PRODUCTION sample carries same-game corners↔advance
    covariance (the raw independent split does not), while the corners marginal is
    unchanged to well within MC error."""
    model = _mixed_model()
    bundle = _select_sampler(model, CFG)
    assert bundle.conditioned is True
    assert bundle.split_sampler is not None
    corr = model.corr_for_band("high")
    n = 300_000
    cond = bundle.sampler(model.legs, corr, n, np.random.default_rng(1))
    split = bundle.split_sampler(model.legs, corr, n, np.random.default_rng(1))
    # Marginal preserved (both ~0.40).
    assert abs(cond[:, 2].mean() - 0.40) < 0.005
    assert abs(split[:, 2].mean() - 0.40) < 0.005
    # Covariance appears in production (nonzero) where the split gives ~0.
    cov_cond = float(np.cov(cond[:, 0], cond[:, 2])[0, 1])
    cov_split = float(np.cov(split[:, 0], split[:, 2])[0, 1])
    assert abs(cov_cond) > abs(cov_split)
    assert abs(cov_split) < 0.001  # the raw split is ~independent


def test_conditioned_copula_sampler_parity_when_all_loadings_zero():
    """KEEP-IN-SYNC parity (hard rule 8c): ``_sample_copula_conditioned`` reproduces
    ``sim/engine.sample_leg_values`` byte-for-byte when NO leg is conditioned (every
    loading 0). This pins the mirrored copula math so a future engine change cannot
    silently diverge the two paths."""
    from combomaker.sim.engine import sample_leg_values
    from combomaker.sim.structural_book import (
        CopulaConditioning,
        _sample_copula_conditioned,
    )

    legs = [LegModel(p=0.40), LegModel(p=0.55), LegModel(p=0.30)]
    corr = np.full((3, 3), 0.25)
    np.fill_diagonal(corr, 1.0)
    n = 40_000
    # Empty conditioning (no loading on any leg) → must equal the engine sampler.
    cond = CopulaConditioning({}, {})
    got = _sample_copula_conditioned(
        legs, corr, n, np.random.default_rng(11), [0, 1, 2], cond, {}
    )
    want = sample_leg_values(legs, corr, n, np.random.default_rng(11))
    assert np.array_equal(got, want)


def test_conditioning_off_is_byte_identical_to_split():
    """With ``corners_et_loading=0`` (or a group game) the production sampler is the
    UNCONDITIONED split bit-for-bit (safety default: conditioning is opt-in)."""
    off = StructuralConfigView(corners_et_loading=0.0)
    model = _mixed_model()
    bundle = _select_sampler(model, off)
    assert bundle.conditioned is False
    assert bundle.split_sampler is None
    corr = model.corr_for_band("high")
    v1 = bundle.sampler(model.legs, corr, 50_000, np.random.default_rng(4))
    # Bit-identical to a direct unconditioned split on the same seed.
    plans, cop = build_game_plans(
        [_ADV_ARG, _ADV_ENG, _CORNERS], [_EV, _EV, _CORNERS_EV],
        [0.55, 0.45, 0.40], off,
    )
    v2 = sample_structural_values(
        plans, cop, list(model.legs), corr, 50_000, np.random.default_rng(4)
    )
    assert np.array_equal(v1, v2)


# --------------------------------------------------------------------------- #
# 3. Conditioned governing tail >= independent split (never thinner).
# --------------------------------------------------------------------------- #


def _concentrated_model() -> BookModel:
    """A concentrated same-game book whose corners leg carries real tail weight: a
    single NO combo on (ARG advance AND corners). Positive same-game coupling makes
    the two legs fail together more, fattening the graded tail."""
    legs = (LegModel(p=0.55), LegModel(p=0.45), LegModel(p=0.55))
    positions = (ComboPosition((0, 2), "no", 80, 6000, leg_sides=("yes", "yes")),)
    corr = np.eye(3)
    leg_index = {_ADV_ARG: 0, _ADV_ENG: 1, _CORNERS: 2}
    event_by_index = {0: _EV, 1: _EV, 2: _CORNERS_EV}
    return BookModel(
        legs, positions, corr, corr.copy(), corr.copy(),
        leg_index, event_by_index, False,
    )


def test_governing_tail_at_least_independent_split():
    """The conditioned production tail is folded with the unconditioned-split guard,
    so the governing model ES is never below the independent split ES (conditioning
    may only fatten). Also verifies the production ES equals the conditioned sample
    (not the split), i.e. the covariance is captured in production."""
    model = _concentrated_model()
    snap = compute_book_risk(model, n_samples=120_000, seed=5, structural_cfg=CFG)
    assert snap.usable
    # Independent-split ES computed the same way, on the guard's sampler.
    bundle = _select_sampler(model, CFG)
    assert bundle.conditioned is True
    from combomaker.sim.book_risk import _book_pnl_from_values, _es_from_pnl

    corr = model.corr_for_band("high")
    # The guard uses the FIFTH spawned substream; reproduce it deterministically.
    seqs = np.random.SeedSequence(5).spawn(5)
    split_vals = bundle.split_sampler(  # type: ignore[union-attr]
        model.legs, corr, 120_000, np.random.default_rng(seqs[4])
    )
    _, split_es = _es_from_pnl(
        _book_pnl_from_values(split_vals, model.positions), 0.99
    )
    # Governing tail dominates the independent split (never thinner).
    assert snap.governing_model_es_99_cc >= split_es - 1e-6


# --------------------------------------------------------------------------- #
# 4 + 5. No-defensible-link legs stay independent + route to the challenger.
# --------------------------------------------------------------------------- #


def test_no_defensible_link_leg_routes_to_challenger_not_conditioning():
    """A straddling copula leg with NO defensible link (config disables the corners
    loading) is NOT conditioned — yet the full-copula challenger (bridge) still fires
    because the knockout game straddles both blocks: independence in the production
    sample + the worse-tail challenger as the backstop (exactly the mandated routing
    for a leg type with no defensible structural link)."""
    model = _mixed_model()
    off = StructuralConfigView(corners_et_loading=0.0)  # no defensible link ⇒ 0
    bundle = _select_sampler(model, off)
    cond = _build_conditioning(
        model, *build_game_plans(
            [_ADV_ARG, _ADV_ENG, _CORNERS], [_EV, _EV, _CORNERS_EV],
            [0.55, 0.45, 0.40], off,
        ), off,
    )
    assert cond.active() is False  # no loading → not conditioned
    assert bundle.conditioned is False
    assert bundle.bridge_needed is True  # worse-tail challenger still the backstop


def test_loading_zero_for_non_corners_and_group():
    """The per-leg loading is nonzero ONLY for a knockout TOTAL-corners leg; cards,
    team-corners, group corners, and any other copula type get 0 (fail-closed: no
    fabricated correlation where no defensible link exists)."""
    # Knockout total corners → nonzero.
    assert _copula_leg_loading(_CORNERS, is_knockout=True, cfg=CFG) > 0.0
    # Group total corners → 0 (corners ⊥ goals measured in group play).
    assert _copula_leg_loading(_CORNERS, is_knockout=False, cfg=CFG) == 0.0
    # Cards (no defensible link) → 0 even in a knockout.
    assert _copula_leg_loading(
        "KXWCCARDS-26JUL15ENGARG-3", is_knockout=True, cfg=CFG
    ) == 0.0
    # Team corners → 0 (not the total-corners ET channel).
    assert _copula_leg_loading(
        "KXWCTCORNERS-26JUL15ENGARG-ARG4", is_knockout=True, cfg=CFG
    ) == 0.0
    # Loading disabled by config → 0 even for knockout total corners.
    off = StructuralConfigView(corners_et_loading=0.0)
    assert _copula_leg_loading(_CORNERS, is_knockout=True, cfg=off) == 0.0


def test_ungamed_copula_leg_unchanged():
    """A copula leg with no event ticker never straddles a structural game, so it is
    never conditioned (loading map has no entry for it) — unchanged, fail-closed."""
    model = _mixed_model()
    # Rebuild with the corners leg UNGAMED (event None).
    model = BookModel(
        model.legs, model.positions, model.corr_high, model.corr_low.copy(),
        model.corr_high.copy(), model.leg_index, {0: _EV, 1: _EV, 2: None}, False,
    )
    plans, cop = build_game_plans(
        [_ADV_ARG, _ADV_ENG, _CORNERS], [_EV, _EV, None], [0.55, 0.45, 0.40], CFG
    )
    cond = _build_conditioning(model, plans, cop, CFG)
    assert cond.active() is False  # ungamed corners: not conditioned
