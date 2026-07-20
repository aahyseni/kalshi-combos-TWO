"""P1.6 — tape-derived structural parity.

RISK_ENGINE_AUDIT_ACTION_PLAN.txt P1 item 6:

    "Add tape-derived parity for regulation/advance, halves/full-time, spread
     windows, multiple scorers, NO legs, real tickers, and enabled series."

The existing ``test_structural_book_mc.py`` proves the risk sampler reproduces
``dixon_coles.joint_probability`` for every leg type, but it does so from a
hand-built ``ModelParams`` and directly-constructed leg specs (``Advance(Team.A)``
etc.). That leaves one seam untested: the path from a REAL Kalshi RFQ ticker
string, through the PUBLIC structural API (``parse_match`` / ``parse_leg`` /
``invert``), to a fitted model whose sampled joint must still match the priced
joint. A rename, a mis-parse, or a settlement-window drift in the pricer could
break that end-to-end contract while the synthetic-param test stays green.

This file closes that gap. Every case here starts from a verbatim tape ticker
(the same shapes the ``pricing.structural`` docstring and the SGP tests record
from the 2026-07 World Cup RFQ tape), parses it through ``structural_api``,
inverts the game's marginals to a model, then asserts the MC sample+settle joint
equals the analytic ``joint_probability`` to Monte-Carlo tolerance — exercised
across each named category:

  * regulation/advance  — ``KXWCGAME`` (regulation-time moneyline, no ET) vs
                          ``KXWCADVANCE`` (ET + shootout) on the same knockout tie,
                          settling to DIFFERENT windows off the SAME model.
  * halves/full-time    — ``KXWC1HTOTAL`` (45' half-scoreline) jointly with
                          ``KXWCTOTAL`` (regulation full-time) on one game.
  * spread windows      — ``KXWCSPREAD`` team-margin leg (regulation window).
  * multiple scorers    — two ``KXWCGOAL`` legs on the same team (shared
                          multinomial goal allocation), and one cross-team.
  * NO legs             — the joint settled with a leg on the NO side
                          (side=False), which must still match analytic.
  * real tickers        — every case above uses a verbatim tape ticker string.
  * enabled series      — KXWC (soccer knockout) inverts + samples structurally;
                          a non-Dixon-Coles series (KXMLB) fails CLOSED to the
                          copula instead of being silently mis-inverted by the
                          soccer scoreline model.

Deterministic RNG seeds keep the Monte-Carlo assertions reproducible.
"""
from __future__ import annotations

import numpy as np
import pytest

from combomaker.pricing import structural_api as api
from combomaker.pricing.dixon_coles import (
    LegSpec,
    MatchFormat,
    ModelParams,
    joint_probability,
    marginal_probability,
)
from combomaker.sim.structural_book import (
    StructuralConfigView,
    build_game_plans,
    sample_game_values,
)

# ---------------------------------------------------------------------------
# Verbatim tape tickers (2026-07 World Cup RFQ tape; see pricing.structural
# docstring + the SGP test suite for provenance). One knockout tie, ENG v ARG,
# carries a leg of every representable family; MEX v ENG carries the 1H total.
# ---------------------------------------------------------------------------
_ENGARG_EVENT = "26JUL15ENGARG"
_MEXENG_EVENT = "26JUL05MEXENG"

_TK_GAME_ENG = "KXWCGAME-26JUL15ENGARG-ENG"      # regulation moneyline (no ET)
_TK_ADV_ENG = "KXWCADVANCE-26JUL15ENGARG-ENG"    # advance (ET + shootout)
_TK_ADV_ARG = "KXWCADVANCE-26JUL15ENGARG-ARG"    # advance, other team (exact mutex)
_TK_BTTS = "KXWCBTTS-26JUL15ENGARG-BTTS"         # both teams score (regulation)
_TK_TOTAL3 = "KXWCTOTAL-26JUL15ENGARG-3"         # >=3 goals full-time (regulation)
_TK_SPREAD = "KXWCSPREAD-26JUL10ESPBEL-ESP2"     # ESP margin >=2 (regulation window)
_TK_GOAL_ENG = "KXWCGOAL-26JUL15ENGARG-ENGX-1"   # ENG player scores >=1
_TK_GOAL_ARG = "KXWCGOAL-26JUL15ENGARG-ARGX-1"   # ARG player scores >=1
_TK_GOAL_ENG2 = "KXWCGOAL-26JUL15ENGARG-ENGY-1"  # 2nd ENG scorer (same team)

_TK_1H_TOTAL = "KXWC1HTOTAL-26JUL05MEXENG-1"     # >=1 goal in the FIRST HALF
_TK_FT_TOTAL = "KXWCTOTAL-26JUL05MEXENG-3"       # >=3 goals full-time, same game

_TK_SPREAD_EVENT = "26JUL10ESPBEL"

# Non-Dixon-Coles series (real MLB tape shape): must fail closed to the copula.
_TK_MLB_HOME = "KXMLBGAME-26JUL071835MILSTL-STL"
_TK_MLB_AWAY = "KXMLBGAME-26JUL071835MILSTL-MIL"
_MLB_EVENT = "KXMLBGAME-26JUL071835MILSTL"

_FMT = MatchFormat.KNOCKOUT
_CFG = StructuralConfigView()

N = 200_000
TOL = 0.006  # ~4.5σ at p≈0.5, n=200k; deterministic seed makes it non-flaky

# A reference knockout model. Tape marginals for the team-level (symmetric)
# constraints are read OFF this model via ``marginal_probability`` so the invert
# targets are self-consistent with a real Poisson scoreline — the exact-system
# residual gate (dixon_coles.invert) rejects two contradictory team marginals, so
# hand-picked numbers would spuriously decline. Player-scorer marginals are given
# explicitly (a scorer's marginal is a free share, not a scoreline quantity).
_REF = ModelParams(
    lam_a=1.35, lam_b=1.05, dc_rho=_CFG.dc_rho, et_factor=_CFG.et_factor,
    match_format=_FMT, pens_win_a=_CFG.pens_win_a, half_share=_CFG.half_share,
)


def _parse(ticker: str, event: str) -> LegSpec:
    """Parse a tape ticker to its spec through the PUBLIC API (never the private
    pricing internals). Fails the test loudly if the pricer declines a shape the
    parity harness claims is representable."""
    match = api.parse_match(event)
    assert match is not None, f"tape game code did not parse: {event!r}"
    spec = api.parse_leg(ticker, match, fmt=_FMT)
    assert not isinstance(spec, str), f"{ticker}: pricer declined -> {spec!r}"
    return spec


def _invert(
    targets: list[tuple[LegSpec, float]],
) -> tuple[ModelParams, dict[int, float]]:
    """Invert tape marginals to a fitted model through the PUBLIC ``invert``.

    The scorer thinning shares come from the model's OWN fit to the scorer
    marginals (not overridden), so ``joint_probability`` and ``sample_game_values``
    read the identical (params, shares) — the parity is on the tape-inverted model,
    not a hand-set share."""
    model = api.invert(
        targets,
        dc_rho=_CFG.dc_rho,
        et_factor=_CFG.et_factor,
        match_format=_FMT,
        max_goals=_CFG.max_goals,
        pens_win_a=_CFG.pens_win_a,
        half_share=_CFG.half_share,
    )
    return model.params, dict(model.shares)


def _mc_joint(
    params: ModelParams,
    specs: list[LegSpec],
    shares: dict[int, float],
    sides: list[bool],
    seed: int,
) -> float:
    """Sample+settle the game and return the MC joint P(all legs settle on their
    chosen side) — NO legs (side=False) settle on 1 - value."""
    rng = np.random.default_rng(seed)
    vals = sample_game_values(params, specs, shares, N, rng)
    cols = vals.copy()
    for j, yes in enumerate(sides):
        if not yes:
            cols[:, j] = 1.0 - cols[:, j]
    return float(np.prod(cols, axis=1).mean())


def _marginals(specs: list[LegSpec], explicit: dict[int, float] | None) -> list[float]:
    """Invert targets: team-level marginals read off ``_REF`` (self-consistent so
    the exact-system residual gate accepts them); explicit[j] overrides leg j (a
    scorer's marginal, which is a free share the model does not pin)."""
    explicit = explicit or {}
    return [
        explicit[j] if j in explicit else marginal_probability(_REF, spec)
        for j, spec in enumerate(specs)
    ]


def _assert_parity(
    specs: list[LegSpec],
    sides: list[bool],
    *,
    seed: int,
    explicit_marginals: dict[int, float] | None = None,
    name: str = "",
) -> None:
    """The single parity gate: tape specs -> invert -> {analytic, MC} agree."""
    marginals = _marginals(specs, explicit_marginals)
    params, shares = _invert(list(zip(specs, marginals, strict=True)))
    analytic = joint_probability(params, list(zip(specs, sides, strict=True)), shares)
    mc = _mc_joint(params, specs, shares, sides, seed)
    assert abs(mc - analytic) < TOL, f"{name}: MC={mc:.5f} analytic={analytic:.5f}"


# ---------------------------------------------------------------------------
# regulation / advance — same game, two settlement WINDOWS off one model.
# ---------------------------------------------------------------------------
def test_regulation_moneyline_and_advance_parity() -> None:
    """KXWCGAME (regulation, no ET) and KXWCADVANCE (ET + shootout) parse to
    different windows and both hit MC/analytic parity off the SAME inverted
    knockout model."""
    game = _parse(_TK_GAME_ENG, _ENGARG_EVENT)
    adv = _parse(_TK_ADV_ENG, _ENGARG_EVENT)
    # regulation win excludes ET; advance includes it — distinct specs.
    assert game.include_et is False
    _assert_parity([game, adv], [True, True], seed=101, name="reg_ml_and_advance")


def test_advance_pair_is_exact_mutex_from_tape() -> None:
    """The two tape advance tickers on the SAME tie are exact complements: they
    can never both settle YES, and exactly one settles on every sample (shared
    shootout coin). This is the cross-combo hedge the risk book relies on."""
    adv_eng = _parse(_TK_ADV_ENG, _ENGARG_EVENT)
    adv_arg = _parse(_TK_ADV_ARG, _ENGARG_EVENT)
    params, shares = _invert([(adv_eng, 0.55), (adv_arg, 0.45)])
    rng = np.random.default_rng(7)
    vals = sample_game_values(params, [adv_eng, adv_arg], shares, N, rng)
    a, b = vals[:, 0], vals[:, 1]
    assert float((a * b).mean()) == 0.0        # never both advance
    assert np.all((a + b) == 1.0)              # exactly one advances always


# ---------------------------------------------------------------------------
# halves / full-time — a 45' half leg jointly with a regulation full-time leg.
# ---------------------------------------------------------------------------
def test_first_half_and_full_time_total_parity() -> None:
    """KXWC1HTOTAL (first-half scoreline) and KXWCTOTAL (regulation full-time)
    on one game: the half grid is built and the joint still matches analytic."""
    half = _parse(_TK_1H_TOTAL, _MEXENG_EVENT)
    full = _parse(_TK_FT_TOTAL, _MEXENG_EVENT)
    _assert_parity([half, full], [True, True], seed=202, name="half_and_full_total")


# ---------------------------------------------------------------------------
# spread windows — team-margin leg (regulation window).
# ---------------------------------------------------------------------------
def test_spread_and_moneyline_parity() -> None:
    """KXWCSPREAD (regulation margin >= n) parses to a GoalSpread window and hits
    parity jointly with the moneyline on its own game."""
    spread = _parse(_TK_SPREAD, _TK_SPREAD_EVENT)
    win = _parse("KXWCGAME-26JUL10ESPBEL-ESP", _TK_SPREAD_EVENT)
    assert spread.include_et is False          # regulation-time spread window
    _assert_parity([win, spread], [True, True], seed=303, name="win_and_spread")


# ---------------------------------------------------------------------------
# multiple scorers — shared multinomial goal allocation across scorer legs.
# ---------------------------------------------------------------------------
def test_two_same_team_scorers_parity() -> None:
    """Two KXWCGOAL legs on the SAME team share one multinomial goal allocation;
    the sampled joint must match the analytic joint that models the same sharing."""
    s1 = _parse(_TK_GOAL_ENG, _ENGARG_EVENT)
    s2 = _parse(_TK_GOAL_ENG2, _ENGARG_EVENT)
    # >=2 team-level legs identify (lam_a, lam_b); the moneyline also ORIENTS the
    # scorers (which team the shares attach to). BTTS supplies the second
    # symmetric team constraint; the scorer marginals are given explicitly.
    win = _parse(_TK_GAME_ENG, _ENGARG_EVENT)
    btts = _parse(_TK_BTTS, _ENGARG_EVENT)
    _assert_parity(
        [win, btts, s1, s2], [True, True, True, True],
        seed=404, explicit_marginals={2: 0.30, 3: 0.24},
        name="two_same_team_scorers",
    )


def test_cross_team_scorers_parity() -> None:
    """One scorer per team plus the moneyline: independent goal draws across teams
    still match analytic."""
    win = _parse(_TK_GAME_ENG, _ENGARG_EVENT)
    btts = _parse(_TK_BTTS, _ENGARG_EVENT)
    eng = _parse(_TK_GOAL_ENG, _ENGARG_EVENT)
    arg = _parse(_TK_GOAL_ARG, _ENGARG_EVENT)
    _assert_parity(
        [win, btts, eng, arg], [True, True, True, True],
        seed=505, explicit_marginals={2: 0.32, 3: 0.28}, name="cross_team_scorers",
    )


# ---------------------------------------------------------------------------
# NO legs — the joint settled with a leg on the NO side (side=False).
# ---------------------------------------------------------------------------
def test_no_leg_parity() -> None:
    """A leg on the NO side (BTTS NO) combined with a YES total: MC settles the NO
    on 1 - value and still matches the analytic joint that scores (spec, False)."""
    btts = _parse(_TK_BTTS, _ENGARG_EVENT)
    total = _parse(_TK_TOTAL3, _ENGARG_EVENT)
    _assert_parity([btts, total], [False, True], seed=606, name="btts_no_and_total_yes")


def test_all_no_advance_pair_never_both_lose() -> None:
    """Two combos, each a NO on an advance-anchored side (opposite teams). Because
    the advance pair is an exact mutex, the two NO legs can never BOTH lose (a NO
    loses only when its advance settles YES) — the portfolio hedge, straight off
    the tape."""
    adv_eng = _parse(_TK_ADV_ENG, _ENGARG_EVENT)
    adv_arg = _parse(_TK_ADV_ARG, _ENGARG_EVENT)
    params, shares = _invert([(adv_eng, 0.55), (adv_arg, 0.45)])
    rng = np.random.default_rng(9)
    vals = sample_game_values(params, [adv_eng, adv_arg], shares, N, rng)
    # NO(adv_eng) loses iff adv_eng YES; NO(adv_arg) loses iff adv_arg YES.
    both_no_lose = vals[:, 0] * vals[:, 1]
    assert float(both_no_lose.mean()) == 0.0


# ---------------------------------------------------------------------------
# enabled series — KXWC samples structurally; a non-DC series fails closed.
# ---------------------------------------------------------------------------
def test_enabled_series_kxwc_builds_structural_plan() -> None:
    """The enabled World Cup series inverts to a structural game plan (not the
    copula) straight from tape tickers + marginals."""
    tickers = [_TK_ADV_ENG, _TK_ADV_ARG]
    events = ["KXWCADVANCE-26JUL15ENGARG", "KXWCADVANCE-26JUL15ENGARG"]
    plans, copula = build_game_plans(tickers, events, [0.55, 0.45], _CFG)
    assert len(plans) == 1
    assert copula == []
    assert set(plans[0].global_indices) == {0, 1}


def test_disabled_series_falls_back_to_copula_not_missampled() -> None:
    """A non-Dixon-Coles series (MLB) must NOT be silently inverted by the soccer
    scoreline model. ``build_game_plans`` leaves it entirely to the copula — the
    fail-closed contract for an unenabled/unmodeled series."""
    tickers = [_TK_MLB_HOME, _TK_MLB_AWAY]
    events = [_MLB_EVENT, _MLB_EVENT]
    plans, copula = build_game_plans(tickers, events, [0.55, 0.45], _CFG)
    assert plans == []
    assert sorted(copula) == [0, 1]


def test_structural_disabled_config_sends_kxwc_to_copula() -> None:
    """The kill switch: with the structural model disabled, even the enabled KXWC
    tickers fall back to the copula (no structural inversion happens)."""
    tickers = [_TK_ADV_ENG, _TK_ADV_ARG]
    events = ["KXWCADVANCE-26JUL15ENGARG", "KXWCADVANCE-26JUL15ENGARG"]
    plans, copula = build_game_plans(
        tickers, events, [0.55, 0.45], StructuralConfigView(enabled=False)
    )
    assert plans == []
    assert sorted(copula) == [0, 1]


# ---------------------------------------------------------------------------
# public-surface fence — this parity harness reaches the model ONLY through the
# public API (never the private pricing internals), same contract as P1.5.
# ---------------------------------------------------------------------------
def test_parity_uses_public_api_only() -> None:
    import ast
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "combomaker.pricing.structural",
            "combomaker.pricing.dixon_coles",
        ):
            for alias in node.names:
                assert not alias.name.startswith("_"), (
                    f"tape-parity test imports private pricing name {alias.name!r}; "
                    "route through pricing.structural_api"
                )
