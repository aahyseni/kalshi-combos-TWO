"""P1.5 — public parse/invert/sample/settle structural API.

``pricing.structural_api`` is the declared, PUBLIC contract the risk MC
(``sim/structural_book.py``) depends on instead of reaching into the private
internals of ``pricing.structural`` / ``pricing.dixon_coles``. These tests pin
the two properties that make that safe:

1. Identity parity — every public name IS the same object as the private
   original it re-exports, so the analytic-vs-simulated structural parity the
   book test asserts cannot diverge from a re-export (zero math added).
2. The whole parse -> invert -> sample -> settle round trip is reachable and
   correct through ONLY the public surface, matching the pricer's own joint.
3. The risk sampler module no longer imports the private pricing names, so the
   seam stays public going forward (a regression fence, not just a snapshot).
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from combomaker.pricing import dixon_coles as dc
from combomaker.pricing import structural as st
from combomaker.pricing import structural_api as api
from combomaker.pricing.dixon_coles import MatchFormat, Team, joint_probability


def test_public_names_are_the_private_originals() -> None:
    """The API adds no math: each public name is the identical object."""
    assert api.parse_leg is st._parse_leg
    assert api.parse_match is st._parse_match
    assert api.Match is st._Match
    assert api.states is dc._states
    assert api.States is dc._States
    assert api.team_goals is dc._team_goals
    assert api.team_indicator is dc._team_indicator
    assert api.half_indicator is dc._half_indicator
    assert api.invert is dc.invert


def test_all_exports_are_importable() -> None:
    for name in api.__all__:
        assert hasattr(api, name), name


def test_parse_match_and_leg_through_public_api() -> None:
    """parse: a real KXWC game code and moneyline ticker resolve via the API."""
    match = api.parse_match("26JUL15ENGARG")
    assert match is not None
    spec = api.parse_leg(
        "KXWCGAME-26JUL15ENGARG-ENG", match, fmt=MatchFormat.KNOCKOUT
    )
    # A representable leg returns a LegSpec dataclass, not a decline reason string.
    assert not isinstance(spec, str)


def test_invert_sample_settle_round_trip_matches_priced_joint() -> None:
    """parse -> invert -> sample -> settle end to end through ONLY the public API,
    and the sampled joint matches ``dixon_coles.joint_probability`` (the pricer's
    own analytic joint) to Monte-Carlo tolerance."""
    fmt = MatchFormat.KNOCKOUT
    match = api.parse_match("26JUL15ENGARG")
    assert match is not None

    # parse two team-level legs (>=2 needed for identification).
    win_a = api.parse_leg("KXWCGAME-26JUL15ENGARG-ENG", match, fmt=fmt)
    btts = api.parse_leg("KXWCBTTS-26JUL15ENGARG-BTTS", match, fmt=fmt)
    assert not isinstance(win_a, str)
    assert not isinstance(btts, str)

    # invert to a fitted model that reproduces the target marginals.
    model = api.invert(
        [(win_a, 0.55), (btts, 0.60)],
        dc_rho=-0.05,
        et_factor=0.35,
        match_format=fmt,
    )
    params = model.params

    # sample: enumerate weighted terminal states, then draw + settle 0/1.
    rng = np.random.default_rng(20260715)
    states = api.states(params)
    n = 200_000
    idx = rng.choice(states.w.size, size=n, p=states.w)
    sampled = api.States(
        w=np.ones(n, dtype=np.float64),
        a90=states.a90[idx], b90=states.b90[idx],
        a_et=states.a_et[idx], b_et=states.b_et[idx],
        a_1h=states.a_1h[idx], b_1h=states.b_1h[idx],
    )
    # settle each leg via the public settle helpers.
    v_win = api.team_indicator(sampled, win_a, params)
    v_btts = api.team_indicator(sampled, btts, params)

    mc_win = float(v_win.mean())
    mc_btts = float(v_btts.mean())
    mc_joint = float((v_win * v_btts).mean())

    an_win = joint_probability(params, [(win_a, True)], {})
    an_btts = joint_probability(params, [(btts, True)], {})
    an_joint = joint_probability(params, [(win_a, True), (btts, True)], {})

    tol = 0.006
    assert abs(mc_win - an_win) < tol
    assert abs(mc_btts - an_btts) < tol
    assert abs(mc_joint - an_joint) < tol
    # inversion actually hit the targets it was given.
    assert abs(an_win - 0.55) < 0.02
    assert abs(an_btts - 0.60) < 0.02


def test_team_goals_public_helper() -> None:
    """settle-support: team_goals returns per-state integer goal counts."""
    match = api.parse_match("26JUL15ENGARG")
    assert match is not None
    win_a = api.parse_leg("KXWCGAME-26JUL15ENGARG-ENG", match, fmt=MatchFormat.KNOCKOUT)
    assert not isinstance(win_a, str)
    model = api.invert(
        [(win_a, 0.55), (api.Btts(), 0.60)],
        dc_rho=-0.05, et_factor=0.35, match_format=MatchFormat.KNOCKOUT,
    )
    states = api.states(model.params)
    goals = api.team_goals(states, Team.A, include_et=False)
    assert goals.shape == states.w.shape
    assert goals.dtype == np.int64
    assert (goals >= 0).all()


def _imported_names_from_private_pricing(source: str) -> set[str]:
    """Names imported from ``pricing.structural`` / ``pricing.dixon_coles`` that
    are private (leading underscore)."""
    tree = ast.parse(source)
    private: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "combomaker.pricing.structural",
            "combomaker.pricing.dixon_coles",
        ):
            for alias in node.names:
                if alias.name.startswith("_"):
                    private.add(f"{node.module}.{alias.name}")
    return private


def test_risk_sampler_does_not_import_private_pricing_internals() -> None:
    """Regression fence: ``sim/structural_book.py`` must reach the structural
    model only through the public API, never the private pricing internals."""
    path = (
        Path(st.__file__).resolve().parents[1]
        / "sim"
        / "structural_book.py"
    )
    private = _imported_names_from_private_pricing(path.read_text(encoding="utf-8"))
    assert private == set(), (
        "risk sampler imports private pricing internals; route through "
        f"pricing.structural_api instead: {sorted(private)}"
    )
