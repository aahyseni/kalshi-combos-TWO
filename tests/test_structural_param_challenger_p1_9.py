"""P1.9 — independent STRUCTURAL-PARAMETER challenger.

The correlation-inflation challenger (P0-8) and the same-game dependence bridge
(P0-7) both stress the JOINT dependence while treating every structural INPUT — the
inverted per-game goal rates, the Dixon-Coles low-score rho, the extra-time /
shootout / half-share settlement constants, the knockout (mutex-metadata)
classification, and the feed marginals — as ground truth. That is a monoculture on
the structural axis. The P1.9 challenger re-inverts and re-samples the structural
games under a conservatively-perturbed ``StructuralConfigView`` and folds its tail
into the governing model max (gate on the WORSE tail), exactly as the correlation
and bridge challengers do.

The suite pins the contract:

1. **Purely additive / never lowers a number.** With the challenger DISABLED (the
   default) every snapshot field is bit-identical to before it existed. Enabling it
   can only WIDEN the governing tail — never narrow it.
2. **A zero-width bands set is an exact no-op** even when the flag is on (absence of
   a band never manufactures a shock).
3. **An active challenger bites** — the perturbed config re-inverts to DIFFERENT
   goal rates / settlement geometry, and on a book where that fattens the tail the
   governing ES lifts strictly above the un-challenged value.
4. **Fail-closed** — no structural cfg, or a config under which nothing re-inverts,
   leaves the governing tail unchanged (the challenger simply does not run; it never
   defaults to a convenient tail-narrowing number).
5. **The perturbation is monotone-conservative** — each named input moves in its
   documented tail-fattening direction and is clamped to a valid range.

Deterministic seeds → the Monte-Carlo assertions are reproducible. Probability space
throughout (floats OK, hard rule 5); the book P&L axis is float cc by design.
"""
from __future__ import annotations

import numpy as np

from combomaker.sim.book_model import BookModel
from combomaker.sim.book_risk import (
    DEFAULT_STRUCTURAL_CHALLENGER_BANDS,
    StructuralChallengerBands,
    _challenger_structural_cfg,
    _shock_marginals,
    _structural_challenger_bundle,
    compute_book_risk,
)
from combomaker.sim.engine import ComboPosition, LegModel
from combomaker.sim.structural_book import StructuralConfigView

CFG = StructuralConfigView()

# One knockout game (ENGARG) with a structural advance PAIR — Dixon-Coles inverts the
# pair, so the whole game is sampled structurally (no copula leg, no P0-7 bridge). A
# single NO combo on both advance legs carries the sell-side tail.
_EV = "KXWCADVANCE-26JUL15ENGARG"
_ADV_ARG = "KXWCADVANCE-26JUL15ENGARG-ARG"
_ADV_ENG = "KXWCADVANCE-26JUL15ENGARG-ENG"


def _structural_model(
    p_arg: float = 0.55, p_eng: float = 0.45, rho: float = 0.2
) -> BookModel:
    """A pure-structural book: [ARG advance, ENG advance] in game ENGARG, one NO
    combo on the pair. No copula leg ⇒ no bridge; the only challenger that can fire
    besides the correlation one is the P1.9 structural-parameter challenger."""
    legs = (LegModel(p=p_arg), LegModel(p=p_eng))
    positions = (ComboPosition((0, 1), "no", 50, 8000, leg_sides=("yes", "yes")),)
    corr = np.eye(2)
    corr[0, 1] = corr[1, 0] = rho
    return BookModel(
        legs, positions, corr, corr.copy(), corr.copy(),
        {_ADV_ARG: 0, _ADV_ENG: 1}, {0: _EV, 1: _EV}, False,
    )


_GRADED_TEAMS = (
    "AAABBB", "CCCDDD", "EEEFFF", "GGGHHH",
    "IIIJJJ", "KKKLLL", "MMMNNN", "OOOPPP",
)


def _graded_structural_book() -> BookModel:
    """Many INDEPENDENT structural games (a ML pair + a total-over per game), each
    with a small NO combo on the total-over leg. The book P&L is a SMOOTH graded
    distribution — the 0.99 ES sits on the interior of the loss distribution (well
    below the deterministic all-hit maximum), so a structural-input shift that pushes
    the total-over marginals into the tail actually MOVES the sampled ES (a single
    saturated combo whose whole loss IS the tail cannot demonstrate the lift)."""
    legs: list[LegModel] = []
    leg_index: dict[str, int] = {}
    event_by_index: dict[int, str | None] = {}
    positions: list[ComboPosition] = []
    idx = 0
    for t in _GRADED_TEAMS:
        ev = f"KXWCGAME-26JUL15{t}"
        a, b = t[:3], t[3:]
        leg_index[f"KXWCGAME-26JUL15{t}-{a}"] = idx
        event_by_index[idx] = ev
        legs.append(LegModel(p=0.55))
        idx += 1
        leg_index[f"KXWCGAME-26JUL15{t}-{b}"] = idx
        event_by_index[idx] = ev
        legs.append(LegModel(p=0.45))
        idx += 1
        total_idx = idx
        leg_index[f"KXWCTOTAL-26JUL15{t}-3"] = idx
        event_by_index[idx] = ev
        # 0.35 (a total-over BELOW even), so a marginal shock TOWARD 0.5 RAISES it
        # (more overs → the NO-total combos lose more → the tail lifts) — an adverse,
        # tail-fattening feed-error regime the challenger must catch.
        legs.append(LegModel(p=0.35))
        idx += 1
        positions.append(
            ComboPosition((total_idx,), "no", 10, 5000, leg_sides=("yes",))
        )
    n = len(legs)
    corr = np.eye(n)
    return BookModel(
        tuple(legs), tuple(positions), corr, corr.copy(), corr.copy(),
        leg_index, event_by_index, False,
    )


# --------------------------------------------------------------------------- #
# 1. Disabled ⇒ bit-identical. Enabled ⇒ never lower.
# --------------------------------------------------------------------------- #


def test_disabled_by_default_is_bit_identical():
    """The challenger is OFF by default: a snapshot computed with the default
    arguments equals one computed with ``structural_challenger=False`` explicitly,
    to the bit, on every gating field (the feature is purely additive)."""
    model = _structural_model()
    base = compute_book_risk(model, n_samples=40_000, seed=1, structural_cfg=CFG)
    off = compute_book_risk(
        model, n_samples=40_000, seed=1, structural_cfg=CFG,
        structural_challenger=False,
    )
    assert base.governing_model_es_99_cc == off.governing_model_es_99_cc
    assert base.production_es_99_cc == off.production_es_99_cc
    assert base.challenger_es_99_cc == off.challenger_es_99_cc
    assert base.p_ruin == off.p_ruin


def test_enabling_never_lowers_the_governing_tail():
    """Enabling the structural challenger can only WIDEN the governing model ES: the
    production/correlation/bridge scenarios are sampled identically (spawned
    substreams unchanged) and the structural ES only ever joins the max. So the
    enabled governing ES is >= the disabled one, every seed."""
    model = _structural_model()
    for seed in (1, 2, 7, 13):
        off = compute_book_risk(
            model, n_samples=40_000, seed=seed, structural_cfg=CFG,
            structural_challenger=False,
        )
        on = compute_book_risk(
            model, n_samples=40_000, seed=seed, structural_cfg=CFG,
            structural_challenger=True,
        )
        assert on.governing_model_es_99_cc >= off.governing_model_es_99_cc
        assert on.p_ruin >= off.p_ruin
        # The other scenarios are byte-identical (enabling the structural challenger
        # never perturbs the production / correlation-challenger books).
        assert on.production_es_99_cc == off.production_es_99_cc
        assert on.challenger_es_99_cc == off.challenger_es_99_cc


# --------------------------------------------------------------------------- #
# 2. A zero-width bands set is an exact no-op even when enabled.
# --------------------------------------------------------------------------- #


def test_zero_width_bands_are_an_exact_no_op():
    """``StructuralChallengerBands()`` (every band 0.0 / False) is ``active is
    False``; enabling the challenger with it yields a snapshot bit-identical to the
    disabled one — a zero-width perturbation manufactures no shock."""
    empty = StructuralChallengerBands()
    assert empty.active is False
    model = _structural_model()
    off = compute_book_risk(
        model, n_samples=40_000, seed=5, structural_cfg=CFG,
        structural_challenger=False,
    )
    noop = compute_book_risk(
        model, n_samples=40_000, seed=5, structural_cfg=CFG,
        structural_challenger=True, structural_challenger_bands=empty,
    )
    assert noop.governing_model_es_99_cc == off.governing_model_es_99_cc
    assert noop.p_ruin == off.p_ruin


# --------------------------------------------------------------------------- #
# 3. An active challenger bites (perturbs the structural inputs).
# --------------------------------------------------------------------------- #


def test_perturbed_config_re_inverts_to_different_settlement_rate():
    """The challenger re-inverts each structural game under the perturbed config +
    shocked marginals, so its YES-settlement rates DIFFER from the production
    structural sampler (the goal-rate perturbation IS the re-fit). Prove the
    challenger sampler produces a different total-over hit rate than production on the
    same legs — the challenger is not a copy of production."""
    from combomaker.sim.book_risk import _select_sampler

    model = _graded_structural_book()
    # A marginal shock guarantees the target marginals move (a feed-error proxy), so
    # the re-inverted goal rates — and hence the settled rates — visibly differ.
    bands = StructuralChallengerBands(rho_band=0.08, marginal_shock=0.20)
    prod = _select_sampler(model, CFG)
    bundle = _structural_challenger_bundle(model, CFG, bands)
    assert bundle is not None
    corr = model.corr_for_band("high")
    vals_p = prod.sampler(model.legs, corr, 200_000, np.random.default_rng(9))
    vals_c = bundle.sampler(model.legs, corr, 200_000, np.random.default_rng(9))
    # The total-over leg (index 2 of the first game) settles at a DIFFERENT rate —
    # the shocked marginal re-inverted to a different goal rate.
    assert abs(vals_p[:, 2].mean() - vals_c[:, 2].mean()) > 0.01


def test_active_challenger_lifts_the_governing_tail():
    """On a graded book whose 0.99 ES sits on the interior of the loss distribution,
    the active structural challenger (goal rates / settlement geometry shocked toward
    an adverse regime) re-prices the tail HIGHER, so the enabled governing ES exceeds
    the disabled one — proof the challenger is not cosmetic (it gates on the worse of
    the production and structural-input tails). A strong explicit marginal shock makes
    the lift deterministic rather than seed-lucky."""
    model = _graded_structural_book()
    # Shock the total-over marginals UP toward 0.5 (more scoring / heavier tail) and
    # lower the DC rho — a coherent adverse structural regime.
    bands = StructuralChallengerBands(rho_band=0.08, marginal_shock=0.30)
    off = compute_book_risk(
        model, n_samples=120_000, seed=1, structural_cfg=CFG,
        structural_challenger=False,
    )
    on = compute_book_risk(
        model, n_samples=120_000, seed=1, structural_cfg=CFG,
        structural_challenger=True, structural_challenger_bands=bands,
    )
    assert on.governing_model_es_99_cc > off.governing_model_es_99_cc
    # And the production/correlation scenarios are untouched (only the structural
    # challenger axis moved the governing max).
    assert on.production_es_99_cc == off.production_es_99_cc
    assert on.challenger_es_99_cc == off.challenger_es_99_cc


# --------------------------------------------------------------------------- #
# 4. Fail-closed: no cfg / nothing re-inverts ⇒ no challenger, tail unchanged.
# --------------------------------------------------------------------------- #


def test_no_structural_cfg_is_a_no_op():
    """``structural_challenger=True`` with ``structural_cfg=None`` cannot run (there
    is no structural model to perturb); the copula-only snapshot is unchanged."""
    model = _structural_model()
    off = compute_book_risk(model, n_samples=40_000, seed=1)
    on = compute_book_risk(
        model, n_samples=40_000, seed=1, structural_challenger=True,
    )
    assert on.governing_model_es_99_cc == off.governing_model_es_99_cc
    assert on.p_ruin == off.p_ruin


def test_bundle_none_when_nothing_re_inverts():
    """A copula-only book (no structural game) yields no challenger bundle — the
    challenger degrades to a no-op rather than inventing a tail (fail-closed)."""
    # A single ungamed leg: nothing the Dixon-Coles model can invert.
    legs = (LegModel(p=0.40),)
    positions = (ComboPosition((0,), "no", 50, 8000, leg_sides=("yes",)),)
    corr = np.eye(1)
    model = BookModel(
        legs, positions, corr, corr.copy(), corr.copy(),
        {"KXWCCORNERS-26JUL15ENGARG-9": 0}, {0: None}, False,
    )
    bundle = _structural_challenger_bundle(
        model, CFG, DEFAULT_STRUCTURAL_CHALLENGER_BANDS
    )
    assert bundle is None
    # And end-to-end the governing tail is unchanged whether or not it is enabled.
    off = compute_book_risk(model, n_samples=20_000, seed=1, structural_cfg=CFG)
    on = compute_book_risk(
        model, n_samples=20_000, seed=1, structural_cfg=CFG,
        structural_challenger=True,
    )
    assert on.governing_model_es_99_cc == off.governing_model_es_99_cc


def test_inactive_bands_yield_no_bundle():
    """A zero-width bands set produces no bundle at all (the challenger cannot even
    build), so there is nothing to sample — the cheapest fail-closed no-op."""
    model = _structural_model()
    assert _structural_challenger_bundle(model, CFG, StructuralChallengerBands()) is None


# --------------------------------------------------------------------------- #
# 5. The perturbation is monotone-conservative and clamped.
# --------------------------------------------------------------------------- #


def test_challenger_config_shifts_each_input_conservatively():
    """Each structural constant moves in its documented tail-fattening direction:
    dc_rho DOWN, et_factor UP, half_share UP, pens toward 0.5, and force_knockout
    reclassifies every game as knockout."""
    ch = _challenger_structural_cfg(CFG, DEFAULT_STRUCTURAL_CHALLENGER_BANDS)
    assert ch.dc_rho < CFG.dc_rho                 # more low-score mass
    assert ch.et_factor > CFG.et_factor           # more extra-time scoring
    assert ch.half_share > CFG.half_share         # heavier first half
    # pens moves toward the max-entropy 0.5 (CFG is already 0.5 ⇒ stays 0.5).
    assert ch.pens_win_a == 0.5
    # force_knockout ⇒ every ticker (all start with "") classifies as knockout.
    assert ch.knockout_series == ("",)


def test_challenger_config_is_clamped_to_valid_ranges():
    """Large bands clamp to valid ranges (et_factor <= 0.60, half_share <= 0.55,
    pens in [0, 0.5] from below) — the challenger never produces a degenerate,
    non-physical config."""
    wild = StructuralChallengerBands(
        rho_band=1.0, et_factor_band=1.0, pens_band=1.0, half_share_band=1.0,
    )
    ch = _challenger_structural_cfg(CFG, wild)
    assert ch.et_factor <= 0.60
    assert ch.half_share <= 0.55
    assert ch.pens_win_a == 0.5   # already at max entropy, cannot go past


def test_marginal_shock_widens_toward_half_and_clamps():
    """``_shock_marginals`` widens each leg toward 0.5 by the shock fraction (a
    feed-error proxy) and clamps to the open unit interval; a zero shock returns
    None (an exact no-op on this axis)."""
    model = _structural_model(p_arg=0.90, p_eng=0.10)
    assert _shock_marginals(model, 0.0) is None
    shocked = _shock_marginals(model, 0.5)
    assert shocked is not None
    # 0.90 → 0.70 (halfway to 0.5), 0.10 → 0.30 — both nearer 0.5 than before.
    assert abs(shocked[0] - 0.70) < 1e-9
    assert abs(shocked[1] - 0.30) < 1e-9
    for v in shocked.values():
        assert 0.001 <= v <= 0.999
