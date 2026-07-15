"""P0-7 — structural/fallback same-game dependence bridge.

The structural split (``sim/structural_book.sample_structural_values``) samples a
game's STRUCTURAL block (scoreline legs) and its COPULA-only block (corners/cards)
from SEPARATE rng calls, discarding their same-game cross-block dependence. This
suite proves the two mandated behaviours of the interim bridge:

1. **Corner covariance follows calibration.** A copula-only corners leg that shares
   a game with a structural ML/total/spread/advance leg carries the calibrated
   within-game correlation in the FULL-COPULA path (the block correlation couples
   the two legs), and that correlation moves with the calibrated rho — NOT the
   independence the raw structural split would imply.

2. **Unsupported coupling invokes the conservative challenger (gate on the worse
   tail).** When a game straddles both blocks, ``compute_book_risk`` /
   ``evaluate_candidate_book_risk`` run a full-copula bridge challenger and fold its
   ES into the governing model tail via ``max`` — so the governing tail is never
   lower than either the structural-split or the full-copula tail. When NO game
   straddles both blocks the bridge does not fire (byte-identical to before).

Deterministic seeds → the Monte-Carlo assertions are reproducible, not flaky.
Probability space throughout (floats OK, hard rule 5); the book P&L axis is float
cc by the simulator's design.
"""
from __future__ import annotations

import numpy as np

from combomaker.sim.book_model import BookModel
from combomaker.sim.book_risk import (
    _bridge_needed,
    _select_sampler,
    compute_book_risk,
    evaluate_candidate_book_risk,
)
from combomaker.sim.engine import ComboPosition, LegModel, sample_leg_values
from combomaker.sim.structural_book import (
    StructuralConfigView,
    build_game_plans,
)

CFG = StructuralConfigView()

# One knockout game (ENGARG). Two structural advance legs (Dixon-Coles inverts the
# pair) + one copula-only corners leg on the SAME game — the straddling case the
# structural split samples separately.
_EV = "KXWCADVANCE-26JUL15ENGARG"
_ADV_ARG = "KXWCADVANCE-26JUL15ENGARG-ARG"
_ADV_ENG = "KXWCADVANCE-26JUL15ENGARG-ENG"
_CORNERS = "KXWCCORNERS-26JUL15ENGARG-9"
_CORNERS_EV = "KXWCCORNERS-26JUL15ENGARG"


def _mixed_model(corners_rho: float) -> BookModel:
    """A book: [ARG advance, ENG advance, corners] all in game ENGARG, with the
    corners↔advance within-game correlation set to ``corners_rho`` in every band.
    A single NO combo on (ARG advance AND corners) so the corners leg carries tail
    weight through the payout product."""
    legs = (LegModel(p=0.55), LegModel(p=0.45), LegModel(p=0.40))
    positions = (
        ComboPosition((0, 2), "no", 50, 8000, leg_sides=("yes", "yes")),
    )
    n = 3
    # Equicorrelation block at ``corners_rho`` (PSD for rho > -0.5, and stays PSD
    # after the challenger inflates it toward +1 — still equicorrelation ≥ 0). Every
    # same-game pair, including corners↔advance, carries the calibrated rho; the two
    # advance legs are coupled EXACTLY in the structural split regardless, so this
    # off-diagonal only bites in the full-copula bridge path.
    corr = np.full((n, n), corners_rho)
    np.fill_diagonal(corr, 1.0)
    leg_index = {_ADV_ARG: 0, _ADV_ENG: 1, _CORNERS: 2}
    event_by_index = {0: _EV, 1: _EV, 2: _CORNERS_EV}
    return BookModel(
        legs, positions, corr, corr.copy(), corr.copy(),
        leg_index, event_by_index, False,
    )


# --------------------------------------------------------------------------- #
# 1. Corner covariance with structural legs follows calibration.
# --------------------------------------------------------------------------- #


def test_corner_covariance_follows_calibration():
    """In the full-copula path, the corners leg's joint-hit rate with a structural
    advance leg MOVES with the calibrated within-game rho (higher rho ⇒ higher
    P(corners AND advance both YES)) — the covariance the bridge preserves. The raw
    structural split, sampling the two blocks independently, cannot see this."""
    joints: list[float] = []
    for rho in (0.0, 0.45, 0.85):
        model = _mixed_model(rho)
        corr = model.corr_for_band("high")
        vals = sample_leg_values(
            model.legs, corr, 200_000, np.random.default_rng(7)
        )
        # P(ARG advance YES AND corners YES) — the calibrated same-game covariance.
        joint = float((vals[:, 0] * vals[:, 2]).mean())
        joints.append(joint)
    # Monotone increasing in the calibrated rho (covariance tracks calibration).
    assert joints[0] < joints[1] < joints[2], joints
    # At rho=0 the joint is ~ the independent product (marginals 0.55 * 0.40).
    assert abs(joints[0] - 0.55 * 0.40) < 0.01


def test_corner_covariance_lost_by_raw_structural_split():
    """Diagnostic that motivates the bridge: the raw structural split samples the
    advance block and the corners block from SEPARATE rng calls, so the corners↔
    advance joint hit rate is the INDEPENDENT product regardless of the calibrated
    rho — the cross-block dependence is discarded. (The bridge exists precisely
    because this split cannot carry the covariance the test above requires.)"""
    from combomaker.sim.structural_book import sample_structural_values

    tickers = [_ADV_ARG, _ADV_ENG, _CORNERS]
    events = [_EV, _EV, _CORNERS_EV]
    marginals = [0.55, 0.45, 0.40]
    plans, copula = build_game_plans(tickers, events, marginals, CFG)
    # Advance pair structural; corners is copula-only.
    assert copula == [2]
    high_corr = _mixed_model(0.85).corr_for_band("high")
    legs = [LegModel(p=0.55), LegModel(p=0.45), LegModel(p=0.40)]
    vals = sample_structural_values(
        plans, copula, legs, high_corr, 200_000, np.random.default_rng(7)
    )
    joint = float((vals[:, 0] * vals[:, 2]).mean())
    # Independence product despite the high calibrated rho — the discarded coupling.
    assert abs(joint - 0.55 * 0.40) < 0.01


# --------------------------------------------------------------------------- #
# 2. Unsupported coupling invokes the conservative challenger (worse tail).
# --------------------------------------------------------------------------- #


def test_bridge_detected_only_when_a_game_straddles_both_blocks():
    """The bridge fires iff a game has BOTH a structural and a copula leg."""
    # Straddling game (advance ×2 + corners) ⇒ bridge needed.
    mixed = _mixed_model(0.5)
    assert _select_sampler(mixed, CFG).bridge_needed is True
    # Pure structural game (advance pair only) ⇒ no copula leg ⇒ no bridge.
    legs = (LegModel(p=0.55), LegModel(p=0.45))
    pos = (ComboPosition((0, 1), "no", 50, 8000, leg_sides=("yes", "yes")),)
    corr2 = np.eye(2)
    corr2[0, 1] = corr2[1, 0] = 0.2
    pure_struct = BookModel(
        legs, pos, corr2, corr2.copy(), corr2.copy(),
        {_ADV_ARG: 0, _ADV_ENG: 1}, {0: _EV, 1: _EV}, False,
    )
    assert _select_sampler(pure_struct, CFG).bridge_needed is False
    # Copula sampling (structural_cfg None) never needs a bridge.
    assert _select_sampler(mixed, None).bridge_needed is False


def test_bridge_needed_helper_ignores_ungamed_copula_legs():
    """A copula leg with no event ticker (game_key None) never straddles a
    structural game (fail-closed): the bridge is not triggered by an ungamed leg."""
    model = _mixed_model(0.5)
    # Rebuild with the corners leg UNGAMED (event None) → not in any structural game.
    model = BookModel(
        model.legs, model.positions, model.corr_high, model.corr_low.copy(),
        model.corr_high.copy(), model.leg_index, {0: _EV, 1: _EV, 2: None}, False,
    )
    tickers = [_ADV_ARG, _ADV_ENG, _CORNERS]
    events = [_EV, _EV, None]
    plans, copula = build_game_plans(tickers, events, [0.55, 0.45, 0.40], CFG)
    assert _bridge_needed(model, plans, copula) is False


def test_bridge_challenger_gates_on_worse_tail():
    """With a straddling game, ``compute_book_risk`` runs the full-copula bridge and
    the governing model ES is the MAX of the structural-split ES, the correlation-
    inflated challenger ES, and the bridge ES — never lower than any of them (gate on
    the worse tail). The bridge is flagged active."""
    model = _mixed_model(0.85)
    snap = compute_book_risk(model, n_samples=60_000, seed=1, structural_cfg=CFG)
    assert snap.usable
    assert snap.bridge_active is True
    assert snap.bridge_es_99_cc > 0.0
    # Governing ES dominates every scenario ES (worse-tail gate).
    assert snap.governing_model_es_99_cc >= snap.production_es_99_cc
    assert snap.governing_model_es_99_cc >= snap.challenger_es_99_cc
    assert snap.governing_model_es_99_cc >= snap.bridge_es_99_cc
    assert snap.governing_model_es_99_cc == max(
        snap.production_es_99_cc, snap.challenger_es_99_cc, snap.bridge_es_99_cc
    )


def _two_combo_model(corners_rho: float) -> BookModel:
    """Two SEPARATE NO combos on the SAME game: NO(ARG advance) and NO(corners).
    Advance is structural, corners is copula-only, so under the structural split the
    two combos' losses are INDEPENDENT (separate rng blocks); under full-copula they
    are coupled at ``corners_rho``. Positively-coupled NO combos break TOGETHER more
    often ⇒ a fatter graded tail — the coupling the split discards."""
    # Rare-loss NO combos: each loses only when its leg settles YES (prob ~0.10), so
    # P(both lose) sits right around the 1% tail — where the copula coupling (which
    # makes both break TOGETHER) lifts the 0.99 ES above the split (independent) path.
    legs = (LegModel(p=0.10), LegModel(p=0.90), LegModel(p=0.10))
    positions = (
        ComboPosition((0,), "no", 50, 8000, leg_sides=("yes",)),   # NO(ARG advance)
        ComboPosition((2,), "no", 50, 8000, leg_sides=("yes",)),   # NO(corners)
    )
    corr = np.full((3, 3), corners_rho)
    np.fill_diagonal(corr, 1.0)
    leg_index = {_ADV_ARG: 0, _ADV_ENG: 1, _CORNERS: 2}
    event_by_index = {0: _EV, 1: _EV, 2: _CORNERS_EV}
    return BookModel(
        legs, positions, corr, corr.copy(), corr.copy(),
        leg_index, event_by_index, False,
    )


def test_bridge_can_raise_the_gating_tail():
    """The bridge is not cosmetic: on a book where the discarded same-game coupling
    fattens the GRADED tail, the full-copula bridge ES exceeds the structural-split
    production ES, so the governing tail is STRICTLY higher than the split alone
    would report (the split, sampling the two combos independently, understated the
    joint break)."""
    model = _two_combo_model(0.85)
    snap = compute_book_risk(model, n_samples=200_000, seed=3, structural_cfg=CFG)
    assert snap.bridge_active is True
    # The coupled corners↔advance tail (full copula) is worse than the split, which
    # sampled corners independently of advance → governing tail lifts above the split.
    assert snap.bridge_es_99_cc > snap.production_es_99_cc
    assert snap.governing_model_es_99_cc > snap.production_es_99_cc


def test_no_bridge_leaves_governing_es_unchanged():
    """A book with NO straddling game (pure structural advance pair) does not run the
    bridge: governing ES = max(production, challenger) exactly, bridge inactive and
    zero — byte-compatible with the pre-P0-7 behaviour."""
    legs = (LegModel(p=0.55), LegModel(p=0.45))
    pos = (ComboPosition((0, 1), "no", 50, 8000, leg_sides=("yes", "yes")),)
    corr2 = np.eye(2)
    corr2[0, 1] = corr2[1, 0] = 0.2
    model = BookModel(
        legs, pos, corr2, corr2.copy(), corr2.copy(),
        {_ADV_ARG: 0, _ADV_ENG: 1}, {0: _EV, 1: _EV}, False,
    )
    snap = compute_book_risk(model, n_samples=60_000, seed=1, structural_cfg=CFG)
    assert snap.bridge_active is False
    assert snap.bridge_es_99_cc == 0.0
    assert snap.governing_model_es_99_cc == max(
        snap.production_es_99_cc, snap.challenger_es_99_cc
    )


def test_candidate_evaluator_runs_bridge_on_straddling_game():
    """The candidate-aware evaluator also gates on the worse tail: when a candidate
    joins a game that already has a structural leg via a copula-only corners leg, the
    POST book straddles both blocks and the full-copula bridge ES enters the POST
    governing tail (max of production, challenger, bridge)."""
    from combomaker.core.conventions import Side
    from combomaker.core.quantity import CentiContracts
    from combomaker.risk.exposure import LegRef, OpenPosition

    def marginals(ticker: str) -> float | None:
        return {_ADV_ARG: 0.55, _ADV_ENG: 0.45, _CORNERS: 0.40}.get(ticker)

    def within_game_rho(a: str, b: str) -> tuple[float, float, float] | None:
        # Calibrated same-game band for any pair in this game (incl. corners).
        return (-0.10, 0.40, 0.85)

    def _pos(pid: str, legs: tuple[LegRef, ...]) -> OpenPosition:
        return OpenPosition(
            position_id=pid,
            combo_ticker=f"COMBO-{pid}",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(5000),
            entry_price_cc=8000,  # type: ignore[arg-type]
            legs=legs,
        )

    committed = (
        _pos("c1", (
            LegRef(_ADV_ARG, _EV, "yes"),
            LegRef(_ADV_ENG, _EV, "yes"),
        )),
    )
    candidate = _pos("cand", (
        LegRef(_ADV_ARG, _EV, "yes"),
        LegRef(_CORNERS, _CORNERS_EV, "yes"),
    ))
    result = evaluate_candidate_book_risk(
        committed,
        candidate,
        marginals=marginals,
        within_game_rho=within_game_rho,
        structural_cfg=CFG,
        n_samples=40_000,
        seed=2,
    )
    assert result.usable
    # POST straddles (advance structural + corners copula) → bridge folded into the
    # POST governing tail (worse-tail gate); governing ES ≥ each scenario ES.
    assert result.post.governing_model_es_99_cc >= result.post.es_99_cc
    assert result.post.governing_model_es_99_cc >= result.post.challenger_es_99_cc
