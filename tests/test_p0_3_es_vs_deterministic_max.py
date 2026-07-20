"""P0-3 — separate model ES from the deterministic maximum loss.

The audit defect: ``operative_es = max(model ES, challenger ES, deterministic
maximum)`` let the deterministic all-hit maximum dominate every sampled ES,
collapsing the CVaR gate into a premium-at-risk cap. The fix reports the SAMPLED
model tail (``governing_model_es_99_cc = max(production, challenger)``) and the
DETERMINISTIC maximum (``deterministic_max_loss_cc``) as SEPARATE fields, and
gates each INDEPENDENTLY.

Mandatory tests for the item:
  1. model ES and the deterministic maximum remain distinct numbers;
  2. a hedge lowers the model ES WITHOUT lowering the deterministic backstop;
  3. each gate fires independently.
"""

from __future__ import annotations

from fractions import Fraction

from combomaker.core.reasons import ReasonCode
from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits, threshold_cc
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import _deterministic_all_hit_loss_cc, compute_book_risk

from tests.test_limits_caps import CONVENTIONS, LOOSE, MARG, FakeBookRisk

BANKROLL_2K = 20_000_000  # $2,000.00 in centi-cents


def _leg(ticker: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


def _pos(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    price_cc: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=our_side,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
    )


# --- MANDATORY TEST 1: model ES and the deterministic maximum are distinct -------


def test_model_es_and_deterministic_max_are_distinct_numbers() -> None:
    """The sampled model ES and the deterministic all-hit maximum are reported on
    separate fields and are NOT the same number for a hedged, non-degenerate book
    (the old code max'd them together, so the ES could never sit below the
    maximum). Here the deterministic maximum strictly exceeds the sampled model ES
    because same-game hedging keeps the sampled tail well inside the all-hit
    worst case."""
    legs = (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1"))
    # A NO-seller and an opposing YES buyer on the SAME parlay: the sampled joint
    # tail is hedged (they cannot both lose fully at once), so the sampled ES sits
    # strictly below the sum-of-premiums all-hit maximum.
    p_no = _pos("no", legs, our_side=Side.NO, contracts=100, price_cc=3_000)
    p_yes = _pos("yes", legs, our_side=Side.YES, contracts=100, price_cc=7_000)
    m = build_book_model(
        [p_no, p_yes],
        marginals=lambda t: 0.6,
        within_game_rho=lambda a, b: (0.1, 0.3, 0.5),
    )
    snap = compute_book_risk(m, n_samples=80_000, seed=5, band="point")

    # Distinct fields carry distinct axes.
    assert snap.governing_model_es_99_cc == max(
        snap.production_es_99_cc, snap.challenger_es_99_cc
    )
    assert snap.deterministic_max_loss_cc == _deterministic_all_hit_loss_cc(m)
    # And they are genuinely different numbers: the hedge holds the sampled tail
    # strictly below the deterministic all-hit maximum.
    assert snap.governing_model_es_99_cc < snap.deterministic_max_loss_cc


# --- MANDATORY TEST 2: a hedge lowers model ES but NOT the deterministic backstop -


def test_hedge_lowers_model_es_without_lowering_deterministic_backstop() -> None:
    """Adding an offsetting (opposite-side, same-parlay) position LOWERS the
    sampled model ES (the joint-loss tail is hedged) while RAISING — never
    lowering — the deterministic maximum (it only ever adds premium). This is the
    whole point of P0-3: the deterministic maximum must not silence the ES, and a
    hedge that helps the tail must be visible on the ES axis."""
    legs = (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1"))
    base = _pos("no", legs, our_side=Side.NO, contracts=100, price_cc=3_000)

    m_unhedged = build_book_model(
        [base],
        marginals=lambda t: 0.6,
        within_game_rho=lambda a, b: (0.1, 0.3, 0.5),
    )
    snap_unhedged = compute_book_risk(m_unhedged, n_samples=80_000, seed=9, band="point")

    hedge = _pos("yes", legs, our_side=Side.YES, contracts=100, price_cc=7_000)
    m_hedged = build_book_model(
        [base, hedge],
        marginals=lambda t: 0.6,
        within_game_rho=lambda a, b: (0.1, 0.3, 0.5),
    )
    snap_hedged = compute_book_risk(m_hedged, n_samples=80_000, seed=9, band="point")

    # The hedge LOWERS the sampled model tail.
    assert snap_hedged.governing_model_es_99_cc < snap_unhedged.governing_model_es_99_cc
    # But the deterministic backstop does NOT drop (it only adds premium).
    assert (
        snap_hedged.deterministic_max_loss_cc
        >= snap_unhedged.deterministic_max_loss_cc
    )
    # Concretely, the deterministic maximum rose by exactly the hedge premium.
    assert snap_hedged.deterministic_max_loss_cc == (
        snap_unhedged.deterministic_max_loss_cc
        + float(hedge.entry_price_cc) * (hedge.contracts // 100)
    )


# --- MANDATORY TEST 3: each gate fires independently -----------------------------


def _check(risk: FakeBookRisk) -> list[ReasonCode]:
    limits = RiskLimits(
        **{
            **LOOSE,
            "caps_shadow_mode": False,
            "portfolio_cvar_frac": Fraction(15, 100),
            "portfolio_det_max_frac": Fraction(15, 100),
        }  # type: ignore[arg-type]
    )
    breaches = LimitChecker(limits).check(
        ExposureBook(CONVENTIONS),
        MARG,
        DailyPnl(),
        risk_bankroll_cc=BANKROLL_2K,
        book_risk=risk,  # type: ignore[arg-type]
    )
    return [b.reason for b in breaches]


def test_model_es_gate_fires_alone() -> None:
    """The model-ES gate fires on a high sampled ES WITHOUT the deterministic-max
    gate firing (deterministic maximum under its ceiling)."""
    thr = threshold_cc(Fraction(15, 100), BANKROLL_2K)
    risk = FakeBookRisk(
        usable=True,
        governing_model_es_99_cc=float(thr + 1),  # over the CVaR ceiling
        deterministic_max_loss_cc=0.0,  # well under the det-max ceiling
    )
    reasons = _check(risk)
    assert ReasonCode.SKIP_PORTFOLIO_CVAR in reasons
    assert ReasonCode.SKIP_PORTFOLIO_DET_MAX not in reasons


def test_deterministic_max_gate_fires_alone() -> None:
    """The deterministic-max gate fires on a high all-hit maximum WITHOUT the
    model-ES gate firing (sampled ES under its ceiling) — the exact case the old
    max-of-three collapsed into a single number."""
    thr = threshold_cc(Fraction(15, 100), BANKROLL_2K)
    risk = FakeBookRisk(
        usable=True,
        governing_model_es_99_cc=0.0,  # well under the CVaR ceiling
        deterministic_max_loss_cc=float(thr + 1),  # over the det-max ceiling
    )
    reasons = _check(risk)
    assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons
    assert ReasonCode.SKIP_PORTFOLIO_CVAR not in reasons


def test_both_gates_fire_together_when_both_exceed() -> None:
    thr = threshold_cc(Fraction(15, 100), BANKROLL_2K)
    risk = FakeBookRisk(
        usable=True,
        governing_model_es_99_cc=float(thr + 1),
        deterministic_max_loss_cc=float(thr + 1),
    )
    reasons = _check(risk)
    assert ReasonCode.SKIP_PORTFOLIO_CVAR in reasons
    assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons


def test_unusable_snapshot_fails_both_gates_closed() -> None:
    """An UNKNOWN/empty snapshot fails BOTH tail axes closed (an unmeasured joint
    tail and an unmeasured deterministic maximum are each never safe)."""
    risk = FakeBookRisk(
        usable=False, governing_model_es_99_cc=0.0, deterministic_max_loss_cc=0.0
    )
    reasons = _check(risk)
    assert ReasonCode.SKIP_PORTFOLIO_CVAR in reasons
    assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons
