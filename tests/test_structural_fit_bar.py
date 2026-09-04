"""Build 2026-09-04 item B — soccer btts|total promote (0.70 -> 0.746) + the
DERIVED structural fit bar + the marginal-consistent hybrid for the symmetric
btts x total pair + structural-fit telemetry.

Report: docs/reports/2026-09-04-build-soccer-btts-total-fit-bar.md
Prototype (rule 8, tools/ first): tools/proto_structural_fit_bar.py
Parity fixture: tests/fixtures/structural_fit_bar_contexts26.json — the 26
recorded fill contexts (decisions.context_json leg mids + rfqs legs, pulled
READ-ONLY from the live store) with the OLD live fair (reproduces the recorded
fair to <= 0.013c), the promoted-only fair and the NEW (prototype) fair.

Covered here:
  1. derived bar units — wider books -> looser bar, floor = the over-identified
     regime's accept boundary, cap = the ONE hard bar; legacy mode unchanged;
  2. hybrid rho derivation — the copula rho reproducing the DC cell; the
     hybrid IS the DC cell at residual 0 and is marginal-consistent otherwise;
  3. CHALLENGE / REJECT paths — priced + widened + recorded vs refused with the
     residual carried on the record; the derived quantity FLOWS (wider books
     turn a CHALLENGE into an ACCEPT); non-symmetric shapes keep the DC cell;
  4. 26-context parity — live == prototype to the cent (and to 1e-9) on every
     context; EXACTLY 0.00c on every combo without a same-game btts|total pair;
  5. tie x total / tie x btts pickoff guard — pinned UNCHANGED (declines on
     REJECT with the record; prices structurally below the bar);
  6. telemetry — the record rides the JointEstimate through pickling (the
     ProcessPool boundary) onto the quote; the store migration adds the
     family/route/reason columns idempotently; the lifecycle recorder counts
     and enqueues once per RFQ;
  7. quote-ability — every club btts x total shape in the fixture returns a
     non-zero NO bid at its recorded target_cost size through the real engine.
"""

from __future__ import annotations

import json
import math
import pickle
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import PricingConfig, StructuralConfig
from combomaker.ops.persistence import Store
from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import (
    Btts,
    MatchFormat,
    ModelParams,
    TotalOver,
    invert,
    joint_probability,
    marginal_probability,
)
from combomaker.pricing.engine import PricingEngine, _same_game_tie_total
from combomaker.pricing.fit_challenge import (
    CHALLENGE_FRACTION,
    REJECT_EXACT,
    REJECT_OVERIDENTIFIED,
    ROUTE_COPULA,
    ROUTE_DECLINED,
    ROUTE_HYBRID,
    ROUTE_REJECT,
    ROUTE_STRUCTURAL,
    FitVerdict,
    StructuralFitRecord,
    classify_fit,
    derived_accept_bar,
)
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.structural import (
    StructuralPricer,
    hybrid_joint,
    implied_pair_rho,
    is_symmetric_btts_total,
    leg_family,
)
from combomaker.rfq.lifecycle import QuoteLifecycle
from combomaker.rfq.models import Rfq, RfqLeg
from tests.test_feed import snapshot_env
from tests.test_filters import Harness
from tests.test_pricing_engine import seed_event

FIXTURE = Path(__file__).parent / "fixtures" / "structural_fit_bar_contexts26.json"

GAME = "26AUG17NCXLEO"
BTTS_T = f"KXLIGAMXBTTS-{GAME}-BTTS"
OVER_T = f"KXLIGAMXTOTAL-{GAME}-3"
TIE_T = "KXLALIGAGAME-26AUG15ALAGET-TIE"
TIE_OVER_T = "KXLALIGATOTAL-26AUG15ALAGET-3"
FLOOR = CHALLENGE_FRACTION * REJECT_OVERIDENTIFIED  # 0.025, pre-existing anchors


def belief(p: float, unc: float = 0.005) -> LegBelief:
    return LegBelief(p=p, uncertainty=unc, source="test")


def leg(ticker: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(ticker, ticker.rsplit("-", 1)[0], side, None)


def pricer(**overrides: object) -> StructuralPricer:
    return StructuralPricer(StructuralConfig(enabled=True, **overrides))  # type: ignore[arg-type]


def pair(pb: float, pt: float, *, line: int = 3, sides: tuple[str, str] = ("yes", "yes"),
         unc: float = 0.005) -> tuple[list[RfqLeg], list[LegBelief], list[str]]:
    legs = [leg(BTTS_T, sides[0]), leg(f"KXLIGAMXTOTAL-{GAME}-{line}", sides[1])]
    return legs, [belief(pb, unc), belief(pt, unc)], list(sides)


class _Ev:
    """No event metadata (the prototype replay's stand-in): grouping by GAME
    key is ticker-derived, so btts + total of one game still share a group."""

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        return None


# --- 1. derived bar ----------------------------------------------------------


def test_floor_is_the_overidentified_regimes_accept_boundary() -> None:
    # Two 1c-spread club books (0.005 + 0.005 = 0.01) resolve less than the
    # floor -> the floor binds; the floor is CHALLENGE_FRACTION of the ONE hard
    # bar = the boundary the pre-existing over-identified regime accepted up to.
    assert derived_accept_bar(0.0) == pytest.approx(FLOOR)
    assert derived_accept_bar(0.01) == pytest.approx(FLOOR)
    assert derived_accept_bar(FLOOR) == pytest.approx(FLOOR)


def test_wider_books_loosen_the_bar_monotone_and_capped() -> None:
    bars = [derived_accept_bar(r) for r in (0.0, 0.01, 0.025, 0.03, 0.04, 0.05, 0.08, 1.0)]
    assert bars == sorted(bars)
    assert derived_accept_bar(0.03) == pytest.approx(0.03)   # the books' resolution flows
    assert derived_accept_bar(0.04) == pytest.approx(0.04)
    # never past the hard bar
    assert derived_accept_bar(0.08) == pytest.approx(REJECT_OVERIDENTIFIED)
    # A broken resolution never LOOSENS the bar (fail-closed to the floor).
    assert derived_accept_bar(float("nan")) == pytest.approx(FLOOR)
    assert derived_accept_bar(-1.0) == pytest.approx(FLOOR)


def test_derived_verdicts_accept_challenge_reject() -> None:
    c = classify_fit(0.02, exactly_identified=True, resolution=0.01)
    assert c.verdict is FitVerdict.ACCEPT and c.reject_bar == REJECT_OVERIDENTIFIED
    assert c.challenge_bar == pytest.approx(FLOOR) and c.resolution == 0.01
    challenged = classify_fit(0.03, exactly_identified=True, resolution=0.01)
    assert challenged.verdict is FitVerdict.CHALLENGE
    # Wider books: the same 0.03 misfit is within what the books resolve.
    assert classify_fit(0.03, exactly_identified=True, resolution=0.04).verdict is FitVerdict.ACCEPT
    over = classify_fit(0.0501, exactly_identified=True, resolution=0.04)
    assert over.verdict is FitVerdict.REJECT
    at_bar = classify_fit(0.05, exactly_identified=True, resolution=0.0)
    assert at_bar.verdict is FitVerdict.CHALLENGE
    # ONE hard bar: identification does not change it in derived mode.
    assert classify_fit(0.03, exactly_identified=False, resolution=0.01).reject_bar == 0.05
    for bad in (float("nan"), float("inf"), -1.0):
        c = classify_fit(bad, exactly_identified=True, resolution=0.01)
        assert c.verdict is FitVerdict.REJECT and not c.priceable
    assert "resolution=" in classify_fit(0.01, exactly_identified=True, resolution=0.01).note()


def test_legacy_mode_is_unchanged_for_the_other_inverters() -> None:
    c = classify_fit(0.004, exactly_identified=True)
    assert c.verdict is FitVerdict.CHALLENGE and c.reject_bar == REJECT_EXACT
    assert c.resolution is None
    assert classify_fit(0.03, exactly_identified=False).verdict is FitVerdict.CHALLENGE


# --- 2. hybrid rho -----------------------------------------------------------


def _params(lam_a: float, lam_b: float) -> ModelParams:
    return ModelParams(lam_a=lam_a, lam_b=lam_b, dc_rho=-0.05, et_factor=1 / 3,
                       match_format=MatchFormat.GROUP)


def test_implied_rho_reproduces_the_dc_cell() -> None:
    b, t = Btts(include_et=False), TotalOver(3, include_et=False)
    params = _params(1.5, 1.2)
    rho = implied_pair_rho(params, b, t)
    ma, mb = marginal_probability(params, b), marginal_probability(params, t)
    cell = joint_probability(params, [(b, True), (t, True)], {})
    back = gaussian_copula_joint_prob([ma, mb], np.array([[1.0, rho], [rho, 1.0]]))
    assert abs(back - cell) < 1e-8
    # The deep dive's worked example: lam (1.5, 1.2) -> implied latent rho ~0.79.
    assert 0.77 < rho < 0.81
    # Lopsided games carry a LOWER latent rho (self-adapting, never a constant).
    assert implied_pair_rho(_params(2.5, 0.8), b, t) < rho


def test_hybrid_is_the_dc_cell_at_zero_residual() -> None:
    # NCXCRA shape from the fixture: btts YES x over-3.5 NO — the fit solves
    # exactly (residual ~0), so the hybrid must reproduce the DC cell.
    legs, beliefs, sides = pair(0.58, 0.3461, line=4, sides=("yes", "no"))
    est, reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None, reason
    model = invert([(Btts(include_et=False), 0.58), (TotalOver(4, include_et=False), 0.3461)],
                   dc_rho=-0.05, et_factor=0.3333, match_format=MatchFormat.GROUP)
    cell = joint_probability(model.params, [(Btts(include_et=False), True),
                                            (TotalOver(4, include_et=False), False)], {})
    assert model.residual < 1e-9
    assert abs(est.p - cell) < 1e-8
    assert fit.route == ROUTE_HYBRID and fit.challenge.verdict is FitVerdict.ACCEPT
    assert fit.family == "btts|total" and fit.n_legs == 2
    assert est.fit is fit


def test_hybrid_prices_market_marginals_on_an_infeasible_pair() -> None:
    # NCXLEO 8/17 (recorded mids): the DC best-fit misses BOTH marginals by
    # ~0.8pp; the hybrid keeps the MARKET marginals and the fit's latent rho.
    pb, pt = 0.5916, 0.5378
    legs, beliefs, sides = pair(pb, pt)
    est, reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None, reason
    model = invert([(Btts(include_et=False), pb), (TotalOver(3, include_et=False), pt)],
                   dc_rho=-0.05, et_factor=0.3333, match_format=MatchFormat.GROUP)
    assert 0.005 < model.residual < FLOOR      # the OLD 0.005 bar refused this pair
    rho = implied_pair_rho(model.params, Btts(include_et=False), TotalOver(3, include_et=False))
    assert abs(est.p - hybrid_joint(rho, [pb, pt], [True, True])) < 1e-12
    assert est.p <= min(pb, pt) + 1e-12       # marginal-consistent (Frechet)
    assert fit.challenge.verdict is FitVerdict.ACCEPT and fit.route == ROUTE_HYBRID
    assert any("hybrid rho_i=" in n for n in est.notes)
    # It sits ABOVE the shipped copula@0.70 joint the pair used to fall to
    # (the deep dive's +1.85..+2.27c) — computed here, not pinned.
    old = gaussian_copula_joint_prob([pb, pt], np.array([[1.0, 0.70], [0.70, 1.0]]))
    assert est.p - old > 0.018


def test_symmetric_pair_detection_is_exactly_btts_x_total() -> None:
    b, t = Btts(include_et=False), TotalOver(3, include_et=False)
    assert is_symmetric_btts_total([b, t]) and is_symmetric_btts_total([t, b])
    assert not is_symmetric_btts_total([b, b]) and not is_symmetric_btts_total([b, t, t])


# --- 3. CHALLENGE / REJECT paths ---------------------------------------------


def test_challenge_band_prices_widens_and_records() -> None:
    # btts 0.68 x over-2.5 0.58: residual 0.0432 — above the floor (0.025), under
    # the hard bar -> CHALLENGE: priced (hybrid), misfit in the width, recorded.
    legs, beliefs, sides = pair(0.68, 0.58)
    est, reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None, reason
    assert fit.challenge.verdict is FitVerdict.CHALLENGE and fit.challenge.should_widen
    assert 0.04 < fit.challenge.residual < 0.05
    assert fit.challenge.challenge_bar == pytest.approx(FLOOR)
    assert fit.challenge.resolution == pytest.approx(0.01)
    assert fit.route == ROUTE_HYBRID and fit.challenge.exactly_identified
    assert est.residual == fit.challenge.residual
    assert est.uncertainty >= fit.challenge.residual * StructuralConfig().misfit_uncertainty_scale
    assert any("verdict=challenge" in n for n in est.notes)


def test_wider_books_turn_the_same_challenge_into_an_accept() -> None:
    # The derived quantity FLOWS: with 2.5c of resolution per leg (Σ 0.05 = the
    # hard bar) the same 0.0432 misfit is within what the books can resolve.
    legs, beliefs, sides = pair(0.68, 0.58, unc=0.025)
    est, _reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None
    assert fit.challenge.verdict is FitVerdict.ACCEPT
    assert fit.challenge.challenge_bar == pytest.approx(REJECT_OVERIDENTIFIED)


def test_reject_over_the_hard_bar_carries_the_residual() -> None:
    legs, beliefs, sides = pair(0.70, 0.58)   # residual 0.0559
    est, reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is None and reason is not None and "hard bar" in reason
    assert fit is not None and fit.route == ROUTE_REJECT
    assert fit.challenge.verdict is FitVerdict.REJECT and not fit.challenge.priceable
    assert 0.05 < fit.challenge.residual < 0.06
    assert fit.reason == reason and fit.family == "btts|total"
    # try_price (the 2-tuple API) is unchanged for every existing caller.
    assert pricer().try_price(legs, beliefs, sides) == (None, reason)


def test_unrepresentable_combo_records_a_sentinel_reject() -> None:
    legs = [leg("KXWC2HTOTAL-26JUL10ENGNOR-2"), leg("KXWCTOTAL-26JUL10ENGNOR-3")]
    est, reason, fit = pricer().try_price_with_fit(
        legs, [belief(0.4), belief(0.55)], ["yes", "yes"]
    )
    assert est is None and reason is not None and "period leg" in reason
    assert fit is not None and fit.challenge.verdict is FitVerdict.REJECT
    assert fit.challenge.residual == -1.0 and fit.route == ROUTE_REJECT
    assert fit.reason == reason


def test_non_symmetric_exact_pair_keeps_the_dc_cell() -> None:
    legs = [leg(f"KXLIGAMXGAME-{GAME}-NCX"), leg(BTTS_T)]
    beliefs, sides = [belief(0.55), belief(0.60)], ["yes", "yes"]
    est, _reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None
    assert fit.route == ROUTE_STRUCTURAL and fit.family == "btts|moneyline"
    assert not any("hybrid" in n for n in est.notes)


def test_overidentified_system_uses_the_same_derived_bar() -> None:
    legs = [leg(f"KXLIGAMXGAME-{GAME}-NCX"), leg(BTTS_T), leg(OVER_T)]
    beliefs, sides = [belief(0.65), belief(0.55), belief(0.66)], ["yes", "yes", "yes"]
    est, reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None, reason
    assert not fit.challenge.exactly_identified and fit.challenge.reject_bar == 0.05
    assert fit.challenge.resolution == pytest.approx(0.015)
    assert fit.challenge.challenge_bar == pytest.approx(FLOOR)
    assert fit.route == ROUTE_STRUCTURAL and fit.family == "btts|moneyline|total"
    assert fit.challenge.verdict in (FitVerdict.ACCEPT, FitVerdict.CHALLENGE)


def test_leg_family_is_sorted_and_order_independent() -> None:
    assert leg_family([leg(OVER_T), leg(BTTS_T)]) == "btts|total"
    assert leg_family([leg(BTTS_T), leg(OVER_T)]) == "btts|total"


# --- 4. parity on the 26 recorded contexts ----------------------------------


def _fixture() -> dict[str, Any]:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _rfq_for(c: dict[str, Any], legs: list[RfqLeg]) -> Rfq:
    return Rfq.from_ws({
        "id": c["rfq_id"],
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-08-17T10:00:00Z",
        "contracts_fp": (
            "0.00" if c.get("target_cost_cc")
            else f"{(c.get('contracts_centi') or 100) / 100:.2f}"
        ),
        "target_cost_dollars": (
            f"{c['target_cost_cc'] / 10_000:.2f}" if c.get("target_cost_cc") else None
        ),
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": lg.market_ticker, "side": lg.side, "event_ticker": lg.event_ticker}
            for lg in legs
        ],
    })


async def _engine(cfg: PricingConfig | None = None) -> tuple[PricingEngine, Harness]:
    h = Harness()
    h.with_meta("KXMVE-C1")
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, cfg or PricingConfig()), h


def _replay_live(
    engine: PricingEngine, c: dict[str, Any], unc: float
) -> JointEstimate | NoQuote | None:
    legs = [RfqLeg(r["market_ticker"], r["event_ticker"], r["side"], None) for r in c["legs"]]
    beliefs = [LegBelief(p=c["leg_mids_cc"][lg.market_ticker] / 10_000, uncertainty=unc,
                         source="fixture") for lg in legs]
    sides = [lg.side for lg in legs]
    rel = classify_legs(legs, _Ev())
    if rel.kind is not RelationshipKind.OK:
        return None
    return engine.compute_joint(_rfq_for(c, legs), beliefs, sides, rel)


async def test_parity_live_equals_prototype_to_the_cent_on_26_contexts() -> None:
    fx = _fixture()
    engine, _h = await _engine()
    n_checked = n_hybrid = 0
    for c in fx["contexts"]:
        joint = _replay_live(engine, c, fx["uncertainty"])
        if c["old_p"] is None:
            assert joint is None, c["rfq_id"]      # the classifier-UNKNOWN 6-leg row
            continue
        assert isinstance(joint, JointEstimate), c["rfq_id"]
        n_checked += 1
        # To the cent (fair_cc) AND bit-level: the port is a mirror, not a re-fit.
        assert round(joint.p * 10_000) == round(c["new_p"] * 10_000), c["rfq_id"]
        assert abs(joint.p - c["new_p"]) < 1e-9, (c["rfq_id"], joint.p, c["new_p"])
        v = c["verdict"]
        if v and "residual" in v:
            assert joint.fit is not None
            assert joint.fit.challenge.verdict.value == v["verdict"]
            assert abs(joint.fit.challenge.residual - v["residual"]) < 1e-9
            assert abs(joint.fit.challenge.challenge_bar - v["accept_bar"]) < 1e-12
            assert joint.uncertainty == pytest.approx(v["uncertainty"], abs=1e-9)
            if c["new_route"] == "hybrid":
                n_hybrid += 1
                assert joint.fit.route == ROUTE_HYBRID
                legs = [RfqLeg(r["market_ticker"], r["event_ticker"], r["side"], None)
                        for r in c["legs"]]
                specs = [Btts(include_et=False) if "BTTS" in lg.market_ticker
                         else TotalOver(int(lg.market_ticker.rsplit("-", 1)[1]), include_et=False)
                         for lg in legs]
                model = invert([(s, c["leg_mids_cc"][lg.market_ticker] / 10_000)
                                for s, lg in zip(specs, legs, strict=True)],
                               dc_rho=-0.05, et_factor=0.3333, match_format=MatchFormat.GROUP)
                assert abs(implied_pair_rho(model.params, specs[0], specs[1]) - v["rho"]) < 1e-9
        elif v and v.get("verdict") == "reject":
            assert joint.fit is not None and joint.fit.route == ROUTE_COPULA
            assert joint.fit.challenge.verdict is FitVerdict.REJECT
    assert n_checked == 25 and n_hybrid == 5


async def test_blast_radius_exactly_zero_without_a_same_game_btts_total_pair() -> None:
    fx = _fixture()
    engine, _h = await _engine()
    moved: list[str] = []
    untouched = 0
    for c in fx["contexts"]:
        if c["old_p"] is None:
            continue
        joint = _replay_live(engine, c, fx["uncertainty"])
        assert isinstance(joint, JointEstimate)
        if not c["same_game_btts_total"]:
            untouched += 1
            assert abs(joint.p - c["old_p"]) < 1e-9, (c["rfq_id"], joint.p, c["old_p"])
            assert round(joint.p * 10_000) == round(c["old_p"] * 10_000)
        elif abs(joint.p - c["old_p"]) > 1e-9:
            moved.append(c["rfq_id"])
    assert untouched == 4
    # Every moved combo carries a same-game btts|total pair (the fixture flag).
    by_id = {c["rfq_id"]: c for c in fx["contexts"]}
    assert all(by_id[r]["same_game_btts_total"] for r in moved)
    assert len(moved) >= 12   # the 4 bare club pairs + the promoted copula pairs


async def test_old_fair_reproduced_the_recorded_fair() -> None:
    # Harness proof (pre-port, from the fixture): the OLD route reproduced the
    # recorded fair to <= 0.013c on every priceable context.
    fx = _fixture()
    worst = max(abs(c["old_p"] * 10_000 - c["recorded_fair_cc"])
                for c in fx["contexts"] if c["old_p"] is not None)
    assert worst <= 1.5   # centi-cents


# --- 5. tie x total / tie x btts pickoff guard: pinned UNCHANGED ---------------


async def _with_priced_books(h: Harness, mids: dict[str, float], qty: str = "50.00") -> None:
    tickers = list(mids)
    h.feed.watch(tickers)
    await h.ws.ack_subscription(0, 5)
    for i, ticker in enumerate(tickers):
        env = snapshot_env(5, i + 1, ticker)
        mid = mids[ticker]
        yes_bid = math.floor((mid - 0.005) * 100 + 1e-9) / 100
        no_bid = math.floor((1.0 - mid - 0.005) * 100 + 1e-9) / 100
        env["msg"]["yes_dollars_fp"] = [[f"{yes_bid:.4f}", qty]]
        env["msg"]["no_dollars_fp"] = [[f"{no_bid:.4f}", qty]]
        await h.ws.deliver(env)
        seed_event(h, ticker.rsplit("-", 1)[0], exclusive=None)


def _combo(legs: list[tuple[str, str]], **overrides: Any) -> Rfq:
    msg: dict[str, Any] = {
        "id": "rfq_x",
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-08-17T10:00:00Z",
        "contracts_fp": "10.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": t, "side": s, "event_ticker": t.rsplit("-", 1)[0]} for t, s in legs
        ],
    }
    msg.update(overrides)
    return Rfq.from_ws(msg)


def test_tie_guard_trigger_function_unchanged() -> None:
    assert _same_game_tie_total([leg(TIE_T), leg(TIE_OVER_T)], ((0, 1),)) is True
    assert _same_game_tie_total([leg(BTTS_T), leg(OVER_T)], ((0, 1),)) is False


async def test_tie_x_total_reject_still_declines_and_is_recorded() -> None:
    # tie 0.35 x over-2.5 0.65: DC residual 0.1068 > the hard bar -> the 8/15
    # pickoff guard DECLINES (never the wrong-signed copula), now WITH the record.
    engine, h = await _engine()
    await _with_priced_books(h, {TIE_T: 0.355, TIE_OVER_T: 0.655})
    result = engine.price(_combo([(TIE_T, "yes"), (TIE_OVER_T, "yes")]), time_to_close_s=100_000)
    assert isinstance(result, NoQuote), result
    assert result.reason is ReasonCode.SKIP_STRUCTURAL_FALLBACK_TIE_TOTAL
    assert result.fit is not None and result.fit.route == ROUTE_DECLINED
    assert result.fit.challenge.verdict is FitVerdict.REJECT
    assert result.fit.challenge.residual > 0.05 and result.fit.family == "moneyline|total"


async def test_tie_x_total_below_the_bar_prices_structurally_not_copula() -> None:
    # tie 0.27 x over-2.5 0.52: residual 0.0027 -> ACCEPT, priced on the DC cell.
    engine, h = await _engine()
    await _with_priced_books(h, {TIE_T: 0.275, TIE_OVER_T: 0.525})
    result = engine.price(_combo([(TIE_T, "yes"), (TIE_OVER_T, "yes")]), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert result.structural_fit is not None
    assert result.structural_fit.route == ROUTE_STRUCTURAL
    assert result.structural_fit.challenge.verdict is FitVerdict.ACCEPT
    assert result.no_bid_cc > 0


# --- 6. telemetry ------------------------------------------------------------


def test_fit_record_survives_the_process_pool_boundary() -> None:
    legs, beliefs, sides = pair(0.5916, 0.5378)
    est, _reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None
    back = pickle.loads(pickle.dumps(est))
    assert back == est and back.fit == fit
    assert isinstance(back.fit, StructuralFitRecord)


async def test_engine_carries_the_record_on_quote_and_on_copula_fallback() -> None:
    engine, h = await _engine()
    await _with_priced_books(h, {BTTS_T: 0.595, OVER_T: 0.535})
    quoted = engine.price(_combo([(BTTS_T, "yes"), (OVER_T, "yes")]), time_to_close_s=100_000)
    assert isinstance(quoted, ConstructedQuote), quoted
    assert quoted.structural_fit is not None
    assert quoted.structural_fit.route == ROUTE_HYBRID
    assert quoted.structural_fit.challenge.verdict is FitVerdict.ACCEPT
    assert quoted.structural_fit.family == "btts|total"

    # A pair over the hard bar falls to the copula and carries the REJECT.
    engine2, h2 = await _engine()
    await _with_priced_books(h2, {BTTS_T: 0.705, OVER_T: 0.585})
    fell = engine2.price(_combo([(BTTS_T, "yes"), (OVER_T, "yes")]), time_to_close_s=100_000)
    assert isinstance(fell, ConstructedQuote), fell
    assert fell.structural_fit is not None
    assert fell.structural_fit.route == ROUTE_COPULA
    assert fell.structural_fit.challenge.verdict is FitVerdict.REJECT
    assert fell.structural_fit.challenge.residual > 0.05
    assert "hard bar" in fell.structural_fit.reason
    assert fell.no_bid_cc > 0   # still QUOTES (caps are the only refusal layer)


async def test_non_soccer_combo_carries_no_record() -> None:
    engine, h = await _engine()
    await h.with_books(["M1", "M2"])
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    rfq = Rfq.from_ws({
        "id": "r", "market_ticker": "KXMVE-C1", "created_ts": "2026-08-17T10:00:00Z",
        "contracts_fp": "10.00", "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
        ],
    })
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote) and result.structural_fit is None


_OLD_DDL = """
CREATE TABLE structural_fits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, rfq_id TEXT, model TEXT NOT NULL, n_legs INTEGER NOT NULL,
    exactly_identified INTEGER NOT NULL, residual REAL NOT NULL, verdict TEXT NOT NULL,
    reject_bar REAL NOT NULL, challenge_bar REAL NOT NULL, tickers_json TEXT NOT NULL
);
"""


async def test_store_migrates_old_structural_fits_table_and_records_family_route(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fits.sqlite3"
    with sqlite3.connect(path) as con:   # a store created BEFORE the new columns
        con.executescript(_OLD_DDL)
    store = await Store.open(path, FakeClock())
    try:
        info = await store._db.execute_fetchall("PRAGMA table_info(structural_fits)")
        cols = {r[1] for r in info}
        assert {"family", "route", "reason"} <= cols
        challenge = classify_fit(0.0432, exactly_identified=True, resolution=0.01)
        await store.record_structural_fit(
            rfq_id="rfq_1", model="dixon_coles", n_legs=2, tickers=(BTTS_T, OVER_T),
            challenge=challenge, family="btts|total", route=ROUTE_HYBRID, reason="",
        )
        rows = await store._db.execute_fetchall(
            "SELECT verdict, residual, challenge_bar, family, route, reason FROM structural_fits"
        )
        assert rows == [("challenge", 0.0432, FLOOR, "btts|total", "hybrid", "")]
        assert await store.count("structural_fits") == 1
    finally:
        await store.close()
    # Idempotent: re-opening the migrated store is a no-op.
    store2 = await Store.open(path, FakeClock())
    try:
        assert await store2.count("structural_fits") == 1
    finally:
        await store2.close()


class _Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def inc(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_structural_fit(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def test_lifecycle_recorder_counts_by_verdict_and_family_and_enqueues_once() -> None:
    legs, beliefs, sides = pair(0.5916, 0.5378)
    est, _reason, fit = pricer().try_price_with_fit(legs, beliefs, sides)
    assert est is not None and fit is not None
    quote = ConstructedQuote(yes_bid_cc=0, no_bid_cc=5000, fair_cc=4600,  # type: ignore[arg-type]
                             width_components_cc={}, structural_fit=fit)
    rfq = _combo([(BTTS_T, "yes"), (OVER_T, "yes")])
    self = SimpleNamespace(_metrics=_Metrics(), _store=_FakeStore())
    await QuoteLifecycle._record_structural_fit(self, rfq, quote)  # type: ignore[arg-type]
    assert self._metrics.counters == {
        "structural.fallback.accept": 1,
        "structural.fallback.accept.btts|total": 1,
    }
    (call,) = self._store.calls
    assert call["rfq_id"] == "rfq_x" and call["family"] == "btts|total"
    assert call["route"] == ROUTE_HYBRID and call["challenge"] is fit.challenge
    assert call["tickers"] == (BTTS_T, OVER_T) and call["n_legs"] == 2
    # A decline carries the record too; a record-less result is a no-op.
    self2 = SimpleNamespace(_metrics=_Metrics(), _store=_FakeStore())
    rejected = classify_fit(0.0559, exactly_identified=True, resolution=0.01)
    rec = StructuralFitRecord(rejected, "dixon_coles", "moneyline|total", ROUTE_DECLINED, 2, "x")
    await QuoteLifecycle._record_structural_fit(  # type: ignore[arg-type]
        self2, rfq, NoQuote(ReasonCode.SKIP_STRUCTURAL_FALLBACK_TIE_TOTAL, "d", fit=rec)
    )
    assert self2._metrics.counters["structural.fallback.reject.moneyline|total"] == 1
    assert self2._store.calls[0]["route"] == ROUTE_DECLINED
    self3 = SimpleNamespace(_metrics=_Metrics(), _store=_FakeStore())
    await QuoteLifecycle._record_structural_fit(  # type: ignore[arg-type]
        self3, rfq, NoQuote(ReasonCode.SKIP_MALFORMED_COMBO, "n/a")
    )
    assert self3._metrics.counters == {} and self3._store.calls == []


# --- 7. quote-ability: every club btts x total shape in the fixture quotes ----


async def test_every_club_btts_total_shape_returns_a_nonzero_no_bid_at_recorded_size() -> None:
    fx = _fixture()
    cfg = PricingConfig()
    cfg = cfg.model_copy(update={"quote": cfg.quote.model_copy(update={"sell_parlays_only": True})})
    probed = 0
    for c in fx["contexts"]:
        if not c["same_game_btts_total"] or c["old_p"] is None:
            continue
        if any(r["market_ticker"].startswith("KXWC") for r in c["legs"]):
            continue   # club shapes only (WC July combos are structural triples)
        engine, h = await _engine(cfg)
        await _with_priced_books(
            h,
            {r["market_ticker"]: c["leg_mids_cc"][r["market_ticker"]] / 10_000 for r in c["legs"]},
        )
        legs = [RfqLeg(r["market_ticker"], r["event_ticker"], r["side"], None) for r in c["legs"]]
        rfq = _rfq_for(c, legs)
        assert rfq.target_cost_cc == c["target_cost_cc"] or rfq.contracts is not None
        result = engine.price(rfq, time_to_close_s=100_000)
        assert isinstance(result, ConstructedQuote), (c["rfq_id"], result)
        assert result.yes_bid_cc == 0 and result.no_bid_cc > 0, (c["rfq_id"], result)
        if len(legs) == 2:
            assert result.structural_fit is not None
            assert result.structural_fit.route == ROUTE_HYBRID
        probed += 1
    assert probed >= 12


def test_config_promotes_the_club_measurement_and_keeps_the_band() -> None:
    corr = PricingConfig().correlation
    assert corr.pair_rho_by_sport["soccer"]["btts|total"] == 0.746
    assert corr.pair_rho_uncertainty["soccer:btts|total"] == 0.12
