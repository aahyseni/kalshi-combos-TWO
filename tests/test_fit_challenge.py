"""P1-4: persist structural inversion residuals; reject/challenge inconsistent fits.

Covers the three pieces of the item:
  1. ``classify_fit`` maps a residual to ACCEPT / CHALLENGE / REJECT with the
     correct hard bar per identification regime, and fails CLOSED on a bad
     residual (never a convenient accept).
  2. The challenge thresholds MIRROR the constants the live (pristine) inverters
     actually enforce — a drift guard, since the classifier duplicates them.
  3. ``JointEstimate.residual`` defaults to 0.0 (copula path) and carries the
     real inversion residual on the structural path.
  4. ``Store.record_structural_fit`` durably persists the fit + verdict.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.ops.persistence import Store
from combomaker.pricing.dixon_coles import (
    Btts,
    MatchFormat,
    Team,
    TeamWin,
    invert,
)
from combomaker.pricing.fit_challenge import (
    CHALLENGE_FRACTION,
    REJECT_EXACT,
    REJECT_OVERIDENTIFIED,
    FitVerdict,
    classify_fit,
)
from combomaker.pricing.joint import JointEstimate

# --- 1. classify_fit verdicts -------------------------------------------------

def test_clean_fit_accepts() -> None:
    c = classify_fit(0.0001, exactly_identified=True)
    assert c.verdict is FitVerdict.ACCEPT
    assert c.priceable and not c.should_widen
    assert c.reject_bar == REJECT_EXACT


def test_elevated_but_below_reject_is_challenged_not_accepted() -> None:
    # Between the challenge bar (0.5 * 0.005 = 0.0025) and the reject bar 0.005.
    c = classify_fit(0.004, exactly_identified=True)
    assert c.verdict is FitVerdict.CHALLENGE
    assert c.priceable  # still priceable...
    assert c.should_widen  # ...but must widen, never ordinary width


def test_over_reject_bar_is_rejected() -> None:
    c = classify_fit(0.02, exactly_identified=True)  # > 0.005 exact bar
    assert c.verdict is FitVerdict.REJECT
    assert not c.priceable


def test_overidentified_regime_uses_larger_bar() -> None:
    # 0.02 is REJECT for an exact system but a CHALLENGE for an over-identified
    # one (bar 0.05, challenge floor 0.025)... actually 0.02 < 0.025 so ACCEPT.
    assert classify_fit(0.02, exactly_identified=False).verdict is FitVerdict.ACCEPT
    assert classify_fit(0.03, exactly_identified=False).verdict is FitVerdict.CHALLENGE
    assert classify_fit(0.06, exactly_identified=False).verdict is FitVerdict.REJECT
    assert classify_fit(0.02, exactly_identified=True).verdict is FitVerdict.REJECT


def test_fails_closed_on_bad_residual() -> None:
    for bad in (float("nan"), float("inf"), -0.001):
        c = classify_fit(bad, exactly_identified=True)
        assert c.verdict is FitVerdict.REJECT, bad
        assert not c.priceable


def test_boundary_at_reject_bar_accepts_at_and_rejects_above() -> None:
    # residual exactly at the reject bar is NOT over it → priceable (challenge).
    assert classify_fit(REJECT_EXACT, exactly_identified=True).verdict is FitVerdict.CHALLENGE
    assert classify_fit(
        REJECT_EXACT + 1e-9, exactly_identified=True
    ).verdict is FitVerdict.REJECT


def test_challenge_bar_is_fraction_of_reject_bar() -> None:
    c = classify_fit(0.0, exactly_identified=True)
    assert c.challenge_bar == REJECT_EXACT * CHALLENGE_FRACTION


# --- 2. threshold parity with the live inverters ------------------------------

def test_thresholds_mirror_live_inverter_constants() -> None:
    """The classifier duplicates the reject bars; assert they equal the literals
    the pristine inverters enforce, so the two cannot drift silently.

    PIN CHANGED 2026-09-04 (build item B, docs/reports/2026-09-04-build-
    soccer-btts-total-fit-bar.md): dixon_coles.invert now enforces ONE hard
    bar for every system — REJECT_OVERIDENTIFIED — and NO exact-system 0.005
    bar. Measured reason: the 0.005 bar rejected 4/4 recorded club btts x
    over-2.5 pairs (residuals 0.0062 / 0.0078 / 0.0102 / 0.0164 — a Poisson
    scoreline cannot reproduce that market shape) while the SAME game's
    triple passed at 0.0195; the accept/challenge split is now derived from
    the leg books (classify_fit, resolution mode). margin_total / mlb_runs
    keep their legacy bars (out of scope) and are still mirrored below.

    Review fix 2026-09-04: dixon_coles binds the bar BY IMPORT from this
    module, so the DC half asserts the bound value (and that no float literal
    bar remains in the inverter), not a source-text match."""
    from combomaker.pricing import dixon_coles

    assert dixon_coles.REJECT_OVERIDENTIFIED is REJECT_OVERIDENTIFIED
    dc_src = inspect.getsource(invert)
    assert "residual > REJECT_OVERIDENTIFIED" in dc_src
    assert re.search(r"residual > 0\.\d+", dc_src) is None

    from combomaker.pricing import margin_total, mlb_runs

    mt_src = inspect.getsource(margin_total.invert_means)
    assert f"residual > {REJECT_OVERIDENTIFIED}" in mt_src
    assert f"residual > {REJECT_EXACT}" in mt_src

    mlb_src = inspect.getsource(mlb_runs.invert_runs)
    assert f"residual > {REJECT_OVERIDENTIFIED}" in mlb_src
    assert f"residual > {REJECT_EXACT}" in mlb_src


# --- 3. JointEstimate.residual ------------------------------------------------

def test_joint_estimate_residual_defaults_to_zero() -> None:
    je = JointEstimate(p=0.3, uncertainty=0.01, frechet_lo=0.0, frechet_hi=1.0, notes=())
    assert je.residual == 0.0


def test_real_inversion_reports_residual_that_classifies_clean() -> None:
    # Two orienting exact constraints solve to ~0 residual → ACCEPT.
    legs = [
        (TeamWin(team=Team.A), 0.55),
        (Btts(), 0.60),
    ]
    model = invert(
        legs, dc_rho=0.0, et_factor=1.0 / 3.0, match_format=MatchFormat.GROUP
    )
    assert model.residual < REJECT_EXACT
    c = classify_fit(model.residual, exactly_identified=True)
    assert c.verdict is FitVerdict.ACCEPT


# --- 4. persistence -----------------------------------------------------------

async def test_record_structural_fit_roundtrips(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "fits.sqlite3", FakeClock())
    try:
        challenge = classify_fit(0.004, exactly_identified=True)  # CHALLENGE
        await store.record_structural_fit(
            rfq_id="rfq_1",
            model="dixon_coles",
            n_legs=3,
            tickers=("KXWCGAME-26JUL06ENGNOR-ENG", "KXWCBTTS-26JUL06ENGNOR"),
            challenge=challenge,
        )
        assert await store.count("structural_fits") == 1

        rows = await store._db.execute_fetchall(
            "SELECT model, n_legs, exactly_identified, residual, verdict,"
            " reject_bar, challenge_bar, tickers_json FROM structural_fits"
        )
        (model, n_legs, exact, residual, verdict, reject_bar, chal_bar, tickers), = rows
        assert model == "dixon_coles"
        assert n_legs == 3
        assert exact == 1
        assert abs(residual - 0.004) < 1e-12
        assert verdict == "challenge"
        assert abs(reject_bar - REJECT_EXACT) < 1e-12
        assert abs(chal_bar - REJECT_EXACT * CHALLENGE_FRACTION) < 1e-12
        assert "ENGNOR" in tickers
    finally:
        await store.close()
