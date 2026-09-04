"""PROTOTYPE (rule 8, tools/ first): derived structural fit bar + marginal-
consistent hybrid for the soccer btts x total pair (build 2026-09-04, item B).

What the live path does today (deep dive explain_soccer-btts-over.txt):
  * ``dixon_coles.invert`` REJECTS an exactly-identified 2-leg fit whose
    residual exceeds a hand-set 0.005, while an over-identified fit is never
    rejected at all (the 0.05 bar documented in fit_challenge is not enforced
    for DC). A club btts x over-2.5 pair cannot be represented by a Poisson
    scoreline to better than ~0.6-1.8pp, so 4/4 bare club pairs fell to the
    copula at the stale WC blend 0.70.

What this prototype does instead (the spec to port):
  1. ONE hard REJECT bar for every DC system (exact and over-identified alike:
     ``fit_challenge.REJECT_OVERIDENTIFIED`` = the pre-existing 0.05).
  2. A DERIVED accept bar = the market's own resolution of the identifying
     constraints: the SUM of the team-level legs' ``belief.uncertainty`` (the
     quantity structural._price already perturbs). residual <= sum -> ACCEPT;
     sum < residual <= hard -> CHALLENGE (price + misfit widening + record);
     residual > hard -> REJECT (copula, recorded). Wider books -> looser bar.
     FLOOR: the accept bar is never stricter than the pre-existing over-
     identified regime's own accept boundary, CHALLENGE_FRACTION x
     REJECT_OVERIDENTIFIED (= 0.025, both pre-existing fit_challenge anchors;
     an exact pair is never held to a tighter bar than the same legs inside a
     triple). Both edges are existing anchors; the only live input is the
     books' resolution. Below the hard bar every verdict is priceable, so
     quote-ability never depends on the label.
  3. SYMMETRIC exactly-identified btts x total pair: the DC best-fit lands on
     balanced lambdas and misses BOTH marginals (0.6-1.8pp on the club fills),
     so pricing its cell would quote a joint inconsistent with the leg books.
     Instead derive the pair's latent rho from the DC best-fit (the Gaussian-
     copula rho reproducing the DC cell at the fitted lambdas) and price the
     MARKET marginals through the copula at that rho; band = the dc_rho band
     re-derived + leg perturbation + misfit widening. Applied on EVERY
     priceable verdict (ACCEPT and CHALLENGE), not only CHALLENGE: at residual
     0 the hybrid IS the DC cell (to 1e-10), and on the four recorded club
     fills the two differ by <= 0.06c, so a verdict-conditional route would
     only add a cliff at the bar. Non-symmetric systems (an orienting leg, or
     3+ legs) keep the DC cell exactly as today.

Validation performed here (read-only, no live module edited):
  * replay of the 26 recorded fill contexts (decisions.context_json leg mids,
    pulled read-only by rowid-range search — see the deep dive): the OLD route
    reproduces the recorded fair (proves the harness), then the promoted and
    the NEW route are computed and written into the parity fixture that the
    post-port live test (tests/test_structural_fit_bar.py) must match to the
    cent;
  * held-out-season backtest (football-data top-5 EU; validate_structural_oos
    loader): log-loss + both-cell calibration for independence, copula 0.70,
    copula 0.746, DC exact and the hybrid, with a paired game-level bootstrap.

Usage (from the repo root, PYTHONPATH=src):
  python tools/proto_structural_fit_bar.py \
      --contexts tests/fixtures/structural_fit_bar_contexts26_raw.json \
      --write-fixture tests/fixtures/structural_fit_bar_contexts26.json
  python tools/proto_structural_fit_bar.py \
      --fixture tests/fixtures/structural_fit_bar_contexts26.json
  python tools/proto_structural_fit_bar.py --backtest [--history data/history] [--out s.json]

The raw contexts fixture was pulled READ-ONLY from the live store (mode=ro):
rfqs by rfq_id (legs_json, target_cost_cc, contracts_centi) joined to the
decisions row of kind 'quote_sent' (context_json: leg_mids_cc, fair_cc, bids)
for the 26 filled RFQs whose quote context survived the 8/19 store collapse.
NOTE: after the port (build 2026-09-04) the 'old' columns this script prints
come from the CURRENT live modules (== new); the committed fixture was written
PRE-port and is the parity oracle tests/test_structural_fit_bar.py asserts.
Keep proto_invert / derived_verdict / implied_pair_rho / hybrid_price in sync
with dixon_coles.invert / fit_challenge.classify_fit / structural.* (rule 8c;
the fixture parity test is the drift guard).
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import brentq, least_squares

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from combomaker.ops.config import CorrelationConfig, StructuralConfig  # noqa: E402
from combomaker.pricing.copula import (  # noqa: E402
    clamp_to_frechet,
    gaussian_copula_joint_prob,
)
from combomaker.pricing.dixon_coles import (  # noqa: E402
    Advance,
    Btts,
    LegSpec,
    MatchFormat,
    ModelParams,
    PlayerScores,
    StructuralError,
    TotalOver,
    joint_probability,
    marginal_probability,
)
from combomaker.pricing.fit_challenge import (  # noqa: E402
    CHALLENGE_FRACTION,
    REJECT_OVERIDENTIFIED,
)
from combomaker.pricing.joint import price_joint_matrices  # noqa: E402
from combomaker.pricing.legs import LegBelief  # noqa: E402
from combomaker.pricing.legtypes import classify_leg, resolve_pricing_alias  # noqa: E402
from combomaker.pricing.relationships import RelationshipKind, classify_legs  # noqa: E402
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation  # noqa: E402
from combomaker.pricing.structural import (  # noqa: E402
    StructuralPricer,
    _parse_leg,
    _parse_match,
    structural_applicable,
)
from combomaker.rfq.models import RfqLeg  # noqa: E402

_LAM_MIN, _LAM_MAX = 0.05, 6.0
HARD_BAR = REJECT_OVERIDENTIFIED  # the ONE hard bar (pre-existing constant)
# The pre-existing over-identified regime's ACCEPT boundary (fit_challenge):
# the floor under the derived accept bar. Keep in sync with
# fit_challenge.derived_accept_bar (covered by the parity fixture).
ACCEPT_FLOOR = CHALLENGE_FRACTION * REJECT_OVERIDENTIFIED
CLUB_MEASURED_BTTS_TOTAL = 0.746  # NOTES.md:308 / results_soccer.md:262, n=8,982
SHIPPED_BTTS_TOTAL = 0.70         # git 470f24b WC blend


# ----------------------------------------------------------------- verdict


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str          # accept | challenge | reject
    residual: float
    accept_bar: float     # derived: sum of identifying legs' uncertainty
    hard_bar: float


def derived_verdict(residual: float, resolution: float) -> Verdict:
    """ACCEPT within the books' own resolution (floored at the over-identified
    regime's accept boundary), CHALLENGE up to the hard bar, REJECT above it.
    ``resolution`` = sum of belief.uncertainty over the identifying (team-
    level) constraints. Keep in sync with fit_challenge.classify_fit
    (resolution mode) — covered by the parity fixture."""
    accept_bar = min(max(resolution, ACCEPT_FLOOR), HARD_BAR)
    if not math.isfinite(residual) or residual < 0.0 or residual > HARD_BAR:
        v = "reject"
    elif residual > accept_bar:
        v = "challenge"
    else:
        v = "accept"
    return Verdict(v, residual, accept_bar, HARD_BAR)


# ----------------------------------------------------------------- inversion


def proto_invert(
    targets: list[tuple[LegSpec, float]],
    *,
    dc_rho: float,
    et_factor: float,
    fmt: MatchFormat,
    max_goals: int,
    pens: float,
    half_share: float,
    warm: tuple[float, float] | None = None,
) -> tuple[ModelParams, float, bool]:
    """Least-squares (lam_a, lam_b) from team-level legs — the same solver as
    dixon_coles.invert — with the UNIFORM hard bar (no exact-system 0.005)."""
    if any(isinstance(s, PlayerScores) for s, _ in targets):
        raise StructuralError("prototype handles team-level legs only")
    for spec, p in targets:
        if not 0.001 <= p <= 0.999:
            raise StructuralError(f"marginal {p} out of invertible range for {spec}")
    if len(targets) < 2:
        raise StructuralError("fewer than 2 team-level legs")

    def mp(x: np.ndarray) -> ModelParams:
        return ModelParams(
            lam_a=float(np.exp(x[0])), lam_b=float(np.exp(x[1])), dc_rho=dc_rho,
            et_factor=et_factor, match_format=fmt, max_goals=max_goals,
            pens_win_a=pens, half_share=half_share,
        )

    def res(x: np.ndarray) -> np.ndarray:
        p = mp(x)
        return np.array([marginal_probability(p, s) - t for s, t in targets])

    lb, ub = math.log(_LAM_MIN), math.log(_LAM_MAX)
    x0 = np.clip(np.log(np.array(warm or (1.3, 1.3), dtype=np.float64)), lb, ub)
    fit = least_squares(res, x0=x0, bounds=(np.full(2, lb), np.full(2, ub)),
                        xtol=1e-12, ftol=1e-12, gtol=1e-12)
    params = mp(np.asarray(fit.x))
    residual = float(np.abs(res(np.asarray(fit.x))).max())
    if residual > HARD_BAR:
        raise StructuralError(
            f"inversion residual {residual:.4f} exceeds the hard bar {HARD_BAR}"
        )
    return params, residual, len(targets) == 2


def implied_pair_rho(params: ModelParams, a: LegSpec, b: LegSpec) -> float:
    """The Gaussian-copula rho reproducing the DC cell P(a & b) at the MODEL
    marginals — the pair's structural-implied latent correlation."""
    ma, mb = marginal_probability(params, a), marginal_probability(params, b)
    cell = joint_probability(params, [(a, True), (b, True)], {})
    lo, hi = -0.999, 0.999

    def f(rho: float) -> float:
        return gaussian_copula_joint_prob([ma, mb], np.array([[1.0, rho], [rho, 1.0]])) - cell

    flo, fhi = f(lo), f(hi)
    if flo >= 0.0:
        return lo
    if fhi <= 0.0:
        return hi
    return float(brentq(f, lo, hi, xtol=1e-10))


def hybrid_price(rho: float, marginals: list[float], yes: list[bool]) -> float:
    """Copula joint of the SELECTED sides at latent rho (NO = sign flip)."""
    m = [p if y else 1.0 - p for p, y in zip(marginals, yes, strict=True)]
    s = np.array([1.0 if y else -1.0 for y in yes])
    corr = np.array([[1.0, rho], [rho, 1.0]]) * np.outer(s, s)
    return clamp_to_frechet(gaussian_copula_joint_prob(m, corr), m)


def is_symmetric_btts_total(specs: list[LegSpec]) -> bool:
    return len(specs) == 2 and {type(s) for s in specs} == {Btts, TotalOver}


# ----------------------------------------------------------------- pricer


@dataclass(frozen=True, slots=True)
class ProtoEstimate:
    p: float
    uncertainty: float
    verdict: Verdict
    route: str            # structural | hybrid
    rho: float | None
    lam: tuple[float, float]


def proto_price(
    legs: list[RfqLeg],
    beliefs: list[LegBelief],
    sides: list[str],
    cfg: StructuralConfig,
    pricer: StructuralPricer,
) -> ProtoEstimate:
    """Mirror of structural._price under the derived bar + hybrid."""
    matches = []
    for leg in legs:
        parts = resolve_pricing_alias(leg.market_ticker).split("-")
        m = _parse_match(parts[1])
        if m is None:
            raise StructuralError(f"unparseable game code {parts[1]!r}")
        matches.append(m)
    match = matches[0]
    if any(m != match for m in matches):
        raise StructuralError("legs reference different matches")
    fmt = pricer._match_format(legs[0].market_ticker)  # noqa: SLF001
    specs: list[LegSpec] = []
    for leg in legs:
        spec = _parse_leg(leg.market_ticker, match, fmt=fmt)
        if isinstance(spec, str):
            raise StructuralError(f"{leg.market_ticker}: {spec}")
        specs.append(spec)
    constraints = [(s, b.p) for s, b in zip(specs, beliefs, strict=True)]
    yes = [side == "yes" for side in sides]
    selected = [(s, y) for s, y in zip(specs, yes, strict=True)]
    symmetric = is_symmetric_btts_total(specs)
    warm: tuple[float, float] | None = None

    base_params, residual, exact = proto_invert(
        constraints, dc_rho=cfg.dc_rho, et_factor=cfg.et_factor, fmt=fmt,
        max_goals=cfg.max_goals, pens=cfg.pens_win_prob, half_share=cfg.half_share,
    )
    warm = (base_params.lam_a, base_params.lam_b)
    resolution = math.fsum(b.uncertainty for b in beliefs)  # all legs team-level here
    verdict = derived_verdict(residual, resolution)
    use_hybrid = symmetric and exact  # every priceable verdict (see module doc)

    def price(params: ModelParams, targets: list[tuple[LegSpec, float]]) -> float:
        if use_hybrid:
            rho = implied_pair_rho(params, specs[0], specs[1])
            return hybrid_price(rho, [t for _, t in targets], yes)
        return float(joint_probability(params, selected, {}))

    def solve(targets: list[tuple[LegSpec, float]], **kw: float) -> float:
        params, _, _ = proto_invert(
            targets, dc_rho=kw.get("dc_rho", cfg.dc_rho),
            et_factor=kw.get("et_factor", cfg.et_factor), fmt=fmt,
            max_goals=cfg.max_goals, pens=kw.get("pens", cfg.pens_win_prob),
            half_share=cfg.half_share, warm=warm,
        )
        return price(params, targets)

    p = price(base_params, constraints)
    leg_unc = 0.0
    for i, b in enumerate(beliefs):
        deltas = []
        for shifted in (b.p + b.uncertainty, b.p - b.uncertainty):
            bumped = list(constraints)
            bumped[i] = (specs[i], min(0.999, max(0.001, shifted)))
            try:
                deltas.append(abs(solve(bumped) - p))
            except StructuralError:
                continue
        if not deltas:
            raise StructuralError(f"marginal band of leg {i} leaves the invertible range")
        leg_unc += max(deltas)
    probes: list[float] = []
    for rho in (cfg.dc_rho - cfg.dc_rho_band, cfg.dc_rho + cfg.dc_rho_band):
        try:
            probes.append(solve(constraints, dc_rho=rho))
        except StructuralError:
            continue
    if fmt is MatchFormat.KNOCKOUT:
        for et in (cfg.et_factor_low, cfg.et_factor_high):
            try:
                probes.append(solve(constraints, et_factor=et))
            except StructuralError:
                continue
    form_unc = max((abs(fp - p) for fp in probes), default=0.0)
    pens_unc = 0.0
    if any(isinstance(s, Advance) for s in specs):
        pp = []
        for pw in (cfg.pens_win_prob - cfg.pens_band, cfg.pens_win_prob + cfg.pens_band):
            try:
                pp.append(solve(constraints, pens=pw))
            except StructuralError:
                continue
        pens_unc = max((abs(x - p) for x in pp), default=0.0)
    misfit = residual * cfg.misfit_uncertainty_scale
    marg = [b.p if y else 1.0 - b.p for b, y in zip(beliefs, yes, strict=True)]
    rho_i = implied_pair_rho(base_params, specs[0], specs[1]) if symmetric else None
    return ProtoEstimate(
        p=clamp_to_frechet(p, marg),
        uncertainty=leg_unc + form_unc + pens_unc + misfit,
        verdict=verdict,
        route="hybrid" if use_hybrid else "structural",
        rho=rho_i,
        lam=(base_params.lam_a, base_params.lam_b),
    )


# ----------------------------------------------------------------- replay


class _Ev:
    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        return None


def sgp_params(c: CorrelationConfig, btts_total: float | None = None) -> SgpParams:
    tbl = {s: dict(t) for s, t in c.pair_rho_by_sport.items()}
    if btts_total is not None:
        tbl["soccer"]["btts|total"] = btts_total
    return SgpParams(
        pair_rho=dict(c.pair_rho), default_rho=c.same_event_rho,
        cross_event_rho=c.cross_event_rho, typed_uncertainty=c.typed_rho_uncertainty,
        untyped_uncertainty=c.untyped_rho_uncertainty,
        pair_uncertainty=dict(c.pair_rho_uncertainty), pair_rho_by_sport=tbl,
        oriented_curve={k: list(v) for k, v in c.oriented_curve.items()},
        oriented_curve_uncertainty=dict(c.oriented_curve_uncertainty),
    )


def copula_p(legs: list[RfqLeg], bel: list[LegBelief], sides: list[str],
             groups: Any, params: SgpParams) -> float:
    s = build_sgp_correlation(legs, groups, params, marginals=[b.p for b in bel])
    return price_joint_matrices(bel, sides, s.corr, s.corr_low, s.corr_high).p


def has_same_game_btts_total(legs: list[RfqLeg], groups: Any) -> bool:
    types = [classify_leg(leg.market_ticker) for leg in legs]
    for g in groups:
        if len(g) < 2:
            continue
        names = {str(types[i]) for i in g}
        if {"btts", "total"} <= names:
            return True
    return False


def load_contexts(path: Path, pairs_path: Path | None) -> list[dict[str, Any]]:
    raw = json.load(open(path, encoding="utf-8"))
    pairs = {}
    if pairs_path is not None:
        pairs = {o["ticker"]: o for o in json.load(open(pairs_path, encoding="utf-8"))}
    out = []
    for rid, d in raw.items():
        ctx = d["quote_ctx"]
        mids = ctx["leg_mids_cc"]
        legs = d.get("legs")
        o = pairs.get(d["market_ticker"], {})
        rows = []
        if legs:
            for x in legs:
                side = x.get("side") or o.get("sides", {}).get(x["market_ticker"])
                rows.append({
                    "market_ticker": x["market_ticker"],
                    "event_ticker": x.get("event_ticker") or x["market_ticker"].rsplit("-", 1)[0],
                    "side": side,
                })
        else:
            for t in o.get("legs", []):
                rows.append({"market_ticker": t, "event_ticker": t.rsplit("-", 1)[0],
                             "side": o.get("sides", {}).get(t)})
        if not rows or any(r["side"] not in ("yes", "no") for r in rows):
            print(f"skip {rid[:8]}: sides unknown")
            continue
        if any(r["market_ticker"] not in mids for r in rows):
            print(f"skip {rid[:8]}: a leg has no recorded mid")
            continue
        out.append({
            "rfq_id": rid,
            "market_ticker": d["market_ticker"],
            "quote_at": d.get("quote_at"),
            "target_cost_cc": d.get("target_cost_cc"),
            "contracts_centi": d.get("contracts_centi"),
            "legs": rows,
            "leg_mids_cc": {r["market_ticker"]: int(mids[r["market_ticker"]]) for r in rows},
            "recorded_fair_cc": int(ctx["fair_cc"]),
            "recorded_no_bid_cc": int(ctx["no_bid_cc"]),
            "recorded_yes_bid_cc": int(ctx["yes_bid_cc"]),
        })
    return out


def replay(contexts: list[dict[str, Any]], unc: float) -> list[dict[str, Any]]:
    corr = CorrelationConfig()
    scfg = StructuralConfig()
    pricer = StructuralPricer(scfg)
    old_params = sgp_params(corr, SHIPPED_BTTS_TOTAL)
    new_params = sgp_params(corr, CLUB_MEASURED_BTTS_TOTAL)
    results = []
    for c in contexts:
        legs = [RfqLeg(r["market_ticker"], r["event_ticker"], r["side"], None) for r in c["legs"]]
        bel = [LegBelief(p=c["leg_mids_cc"][lg.market_ticker] / 10_000, uncertainty=unc,
                         source="fixture") for lg in legs]
        sides = [lg.side for lg in legs]
        rel = classify_legs(legs, _Ev())
        groups = rel.same_event_groups
        rec: dict[str, Any] = dict(c)
        rec["relationship"] = str(rel.kind)
        rec["same_game_btts_total"] = has_same_game_btts_total(legs, groups)
        if rel.kind is not RelationshipKind.OK:
            rec.update(old_route="unpriceable", old_p=None, promoted_p=None,
                       new_route="unpriceable", new_p=None, verdict=None)
            results.append(rec)
            continue
        applicable = structural_applicable(legs, groups)
        # OLD live route (the modules as they are today: 0.005 exact bar, 0.70)
        if applicable:
            j, reason = pricer.try_price(legs, bel, sides)
            if j is not None:
                old_route, old_p = "structural", j.p
            else:
                old_route, old_p = f"copula(fallback: {reason})", copula_p(
                    legs, bel, sides, groups, old_params)
        else:
            old_route, old_p = "copula", copula_p(legs, bel, sides, groups, old_params)
        # PROMOTED only (step 1): copula table at 0.746, routing unchanged
        if old_route == "structural":
            promoted_p = old_p
        else:
            promoted_p = copula_p(legs, bel, sides, groups, new_params)
        # NEW route (steps 1+2)
        verdict: dict[str, Any] | None = None
        if applicable:
            try:
                est = proto_price(legs, bel, sides, scfg, pricer)
                new_route, new_p = est.route, est.p
                verdict = {
                    "verdict": est.verdict.verdict, "residual": est.verdict.residual,
                    "accept_bar": est.verdict.accept_bar, "hard_bar": est.verdict.hard_bar,
                    "rho": est.rho, "lam_a": est.lam[0], "lam_b": est.lam[1],
                    "uncertainty": est.uncertainty,
                }
            except StructuralError as exc:
                new_route, new_p = f"copula(reject: {exc})", copula_p(
                    legs, bel, sides, groups, new_params)
                verdict = {"verdict": "reject", "reason": str(exc)}
        else:
            new_route, new_p = "copula", copula_p(legs, bel, sides, groups, new_params)
        rec.update(old_route=old_route, old_p=old_p, promoted_p=promoted_p,
                   new_route=new_route, new_p=new_p, verdict=verdict)
        results.append(rec)
    return results


def print_replay(results: list[dict[str, Any]]) -> None:
    print("\n=== 26-context replay (fair = model P(combo YES), cents) ===")
    print(f"{'rfq':8s} {'n':>2s} {'pair':4s} {'recorded':>8s} {'old':>8s} {'d':>6s} "
          f"{'promoted':>8s} {'new':>8s} {'shift':>7s}  route(old -> new)")
    max_repro = 0.0
    for r in results:
        if r["old_p"] is None:
            print(f"{r['rfq_id'][:8]} {len(r['legs']):2d} {'-':4s} "
                  f"{r['recorded_fair_cc']/100:8.2f} {'n/a':>8s}  ({r['relationship']})")
            continue
        old_cc = r["old_p"] * 100
        d = old_cc - r["recorded_fair_cc"] / 100
        max_repro = max(max_repro, abs(d))
        shift = (r["new_p"] - r["old_p"]) * 100
        v = r["verdict"] or {}
        vs = ""
        if v:
            vs = f" [{v.get('verdict')} resid={v.get('residual', float('nan')):.4f}"
            if v.get("rho") is not None:
                vs += f" rho_i={v['rho']:.3f}"
            vs += "]"
        tag = "BxT" if r["same_game_btts_total"] else ""
        print(f"{r['rfq_id'][:8]} {len(r['legs']):2d} {tag:4s} "
              f"{r['recorded_fair_cc']/100:8.2f} {old_cc:8.2f} {d:+6.2f} "
              f"{r['promoted_p']*100:8.2f} {r['new_p']*100:8.2f} {shift:+7.2f}  "
              f"{r['old_route'][:40]} -> {r['new_route'][:40]}{vs}")
    print(f"max |old - recorded| = {max_repro:.3f}c")
    non = [r for r in results if r["old_p"] is not None and not r["same_game_btts_total"]]
    worst = max((abs(r["new_p"] - r["old_p"]) * 100 for r in non), default=0.0)
    print(f"blast radius: {len(non)} combos WITHOUT a same-game btts|total pair, "
          f"max |new - old| = {worst:.4f}c")


# ----------------------------------------------------------------- backtest


def backtest(
    history: Path, n_boot: int = 10_000, seed: int = 7, resolution: float = 0.01
) -> dict[str, Any]:
    """Held-out-season scoring of the btts x over-2.5 cell (G1).

    Two tables per subset:
      * AS-IS: the model's own P(btts) as the btts marginal (no closing BTTS
        odds exist in football-data), so the DC cell and the hybrid coincide
        by construction — this validates the DC-implied rho concept at the
        TRUE (1X2-identified) lambdas against outcomes.
      * LIVE-PATH STRESS: the market's btts marginal is emulated as the model
        btts + the Poisson btts gap MEASURED ON TRAIN (realized BTTS rate minus
        the model's mean P(btts); the deep dive's "real BTTS runs above the
        scoreline model"). That makes most pairs INFEASIBLE for the 2-leg fit
        exactly like the recorded club fills, and scores the LIVE 2-leg route
        end to end: proto_invert on (btts, over) -> derived verdict at
        ``resolution`` (= two 1c-spread books) -> balanced-lambda rho -> hybrid
        on the (shifted) market marginals — against outcomes, with the
        copula@0.70 / @0.746 on the SAME marginals as the comparators.
    """
    import validate_structural_oos as V

    V.HISTORY = history
    games = V.load_games()
    train = [g for g in games if g.season < V.TRAIN_BEFORE]
    test = [g for g in games if g.season >= V.TRAIN_BEFORE]
    print(f"games {len(games)} train {len(train)} held-out {len(test)} "
          f"(split at season {V.TRAIN_BEFORE})")
    dc_rho = StructuralConfig().dc_rho
    b, t = Btts(include_et=False), TotalOver(3, include_et=False)
    rng = np.random.default_rng(seed)

    def fitted(subset: list[V.Game]) -> list[tuple[V.Game, ModelParams, float, float]]:
        out = []
        for g in subset:
            params = V.invert_game(g, dc_rho)
            if params is None:
                continue
            pb = joint_probability(params, [(b, True)], {})
            out.append((g, params, pb, g.p_over))
        return out

    def summarize(label: str, names: list[str], LL: list[list[float]],
                  pred: dict[str, float], var: dict[str, float], hits: int,
                  extra: dict[str, Any]) -> dict[str, Any]:
        arr = -np.array(LL)
        n = len(arr)
        mean = arr.mean(0)
        out: dict[str, Any] = {"n": n, "logloss": dict(zip(names, map(float, mean), strict=True))}
        print(f"\n[{label}] n={n}")
        for k, m in zip(names, mean, strict=True):
            z = (hits - pred[k]) / math.sqrt(var[k]) if var[k] > 0 else float("nan")
            print(f"  {k:28s} log-loss/game {m:.5f}   both-cell predicted {pred[k]/n:.4f} "
                  f"(realized {hits/n:.4f}, z={z:+.2f})")
            out.setdefault("both_cell", {})[k] = {
                "predicted": pred[k] / n, "realized": hits / n, "z": z}
        base = names.index("copula 0.746")
        out["bootstrap_vs_0746"] = {}
        print(f"  paired game-level bootstrap ({n_boot}) of log-loss DIFFERENCE vs copula 0.746"
              " (negative = better than 0.746):")
        for k in range(len(names)):
            d = arr[:, k] - arr[:, base]
            idx = rng.integers(0, n, size=(n_boot, n))
            bs = d[idx].mean(1)
            lo, hi = float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))
            out["bootstrap_vs_0746"][names[k]] = {"diff": float(d.mean()), "ci95": [lo, hi]}
            print(f"    {names[k]:28s} diff={d.mean():+.5f}  95% CI [{lo:+.5f},{hi:+.5f}]")
        out.update(extra)
        return out

    def score_as_is(
        rows: list[tuple[V.Game, ModelParams, float, float]], label: str
    ) -> dict[str, Any]:
        names = ["independence", "copula 0.70", "copula 0.746", "DC exact",
                 "hybrid (DC-implied rho)"]
        LL: list[list[float]] = []
        pred: dict[str, float] = collections.defaultdict(float)
        var: dict[str, float] = collections.defaultdict(float)
        hits = 0
        rhos: list[float] = []
        two_leg_dev: list[float] = []
        two_leg_resid: list[float] = []
        t0 = time.perf_counter()
        for g, params, pb, pt in rows:
            ex = V.clamp_frechet2(joint_probability(params, [(b, True), (t, True)], {}), pb, pt)
            ri = implied_pair_rho(params, b, t)
            rhos.append(ri)
            # LIVE-PATH agreement: the 2-leg (btts, over) inversion — the exact
            # system the live pricer solves for a bare pair — must recover the
            # 3-constraint model's implied rho from (p_btts, p_over) alone.
            try:
                p2, r2, _ = proto_invert(
                    [(b, pb), (t, pt)], dc_rho=dc_rho, et_factor=1.0 / 3.0,
                    fmt=MatchFormat.GROUP, max_goals=V.MAX_GOALS, pens=0.5, half_share=0.45,
                )
                two_leg_dev.append(abs(implied_pair_rho(p2, b, t) - ri))
                two_leg_resid.append(r2)
            except StructuralError:
                pass
            cells = [pb * pt, V.copula_pair(pb, pt, 0.70), V.copula_pair(pb, pt, 0.746), ex,
                     V.copula_pair(pb, pt, ri)]
            obs = (g.btts, g.over)
            LL.append([V.cell_ll2(pb, pt, c, *obs) for c in cells])
            hits += int(g.btts and g.over)
            for k, c in zip(names, cells, strict=True):
                pred[k] += c
                var[k] += c * (1 - c)
        print(f"  ({time.perf_counter()-t0:.0f}s)")
        extra = {
            "rho_implied": {"mean": float(np.mean(rhos)), "p10": float(np.percentile(rhos, 10)),
                            "p90": float(np.percentile(rhos, 90))},
            "two_leg_path": {
                "n": len(two_leg_dev),
                "rho_dev_mean": float(np.mean(two_leg_dev)) if two_leg_dev else None,
                "rho_dev_max": float(np.max(two_leg_dev)) if two_leg_dev else None,
                "resid_max": float(np.max(two_leg_resid)) if two_leg_resid else None,
            },
        }
        out = summarize(label, names, LL, pred, var, hits, extra)
        print(f"  per-match DC-implied latent rho: mean {np.mean(rhos):.3f} "
              f"p10 {np.percentile(rhos, 10):.3f} p90 {np.percentile(rhos, 90):.3f}")
        print(f"  2-leg live-path inversion of (model btts, market over): "
              f"n={len(two_leg_dev)} |rho_2leg - rho_3leg| mean {np.mean(two_leg_dev):.4f} "
              f"max {np.max(two_leg_dev):.4f}; max residual {np.max(two_leg_resid):.5f}")
        return out

    def score_stress(rows: list[tuple[V.Game, ModelParams, float, float]], label: str,
                     shift: float) -> dict[str, Any]:
        names = ["independence", "copula 0.70", "copula 0.746", "DC 2-leg best-fit cell",
                 "hybrid (2-leg rho)"]
        LL: list[list[float]] = []
        pred: dict[str, float] = collections.defaultdict(float)
        var: dict[str, float] = collections.defaultdict(float)
        hits = 0
        verdicts: collections.Counter[str] = collections.Counter()
        rhos2: list[float] = []
        resids: list[float] = []
        t0 = time.perf_counter()
        for g, _params, pb, pt in rows:
            pbm = min(0.999, max(0.001, pb + shift))
            try:
                p2, r2, _ = proto_invert(
                    [(b, pbm), (t, pt)], dc_rho=dc_rho, et_factor=1.0 / 3.0,
                    fmt=MatchFormat.GROUP, max_goals=V.MAX_GOALS, pens=0.5, half_share=0.45,
                )
            except StructuralError:
                verdicts["reject"] += 1
                continue
            v = derived_verdict(r2, resolution)
            verdicts[v.verdict] += 1
            resids.append(r2)
            r_i = implied_pair_rho(p2, b, t)
            rhos2.append(r_i)
            dc_cell = V.clamp_frechet2(joint_probability(p2, [(b, True), (t, True)], {}), pbm, pt)
            cells = [pbm * pt, V.copula_pair(pbm, pt, 0.70), V.copula_pair(pbm, pt, 0.746),
                     dc_cell, V.copula_pair(pbm, pt, r_i)]
            obs = (g.btts, g.over)
            LL.append([V.cell_ll2(pbm, pt, c, *obs) for c in cells])
            hits += int(g.btts and g.over)
            for k, c in zip(names, cells, strict=True):
                pred[k] += c
                var[k] += c * (1 - c)
        print(f"  ({time.perf_counter()-t0:.0f}s)")
        n_all = sum(verdicts.values())
        extra = {
            "btts_shift": shift,
            "resolution": resolution,
            "verdicts": {k: v / n_all for k, v in verdicts.items()},
            "residual": {"mean": float(np.mean(resids)), "p90": float(np.percentile(resids, 90)),
                         "max": float(np.max(resids))},
            "rho_2leg": {"mean": float(np.mean(rhos2)), "p10": float(np.percentile(rhos2, 10)),
                         "p90": float(np.percentile(rhos2, 90))},
        }
        out = summarize(label, names, LL, pred, var, hits, extra)
        print(f"  verdicts at resolution {resolution}: "
              + ", ".join(f"{k} {v/n_all:.1%}" for k, v in sorted(verdicts.items())))
        print(f"  2-leg residual mean {np.mean(resids):.4f} p90 {np.percentile(resids, 90):.4f} "
              f"max {np.max(resids):.4f}; balanced-lambda rho mean {np.mean(rhos2):.3f} "
              f"p10 {np.percentile(rhos2, 10):.3f} p90 {np.percentile(rhos2, 90):.3f}")
        return out

    tr, te = fitted(train), fitted(test)
    # Poisson btts gap, MEASURED ON TRAIN ONLY (held-out never touched).
    shift = float(np.mean([g.btts for g, *_ in tr]) - np.mean([pb for _, _, pb, _ in tr]))
    print(f"\nPoisson btts gap measured on TRAIN: realized {np.mean([g.btts for g, *_ in tr]):.4f}"
          f" - model {np.mean([pb for _, _, pb, _ in tr]):.4f} = {shift:+.4f}")
    return {
        "btts_shift_train": shift,
        "held_out": score_as_is(te, "HELD-OUT (season >= 2024), AS-IS marginals"),
        "held_out_stress": score_stress(
            te, f"HELD-OUT, LIVE-PATH STRESS (market btts = model btts {shift:+.4f})", shift),
        "train": score_as_is(tr, "TRAIN (< 2024), AS-IS marginals"),
        "train_stress": score_stress(tr, "TRAIN, LIVE-PATH STRESS", shift),
    }


# ----------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contexts", type=Path, help="raw contexts JSON (store probe output)")
    ap.add_argument("--pairs", type=Path, help="m3_pairs_fills.json (sides fallback)")
    ap.add_argument("--fixture", type=Path, help="existing fixture JSON to replay")
    ap.add_argument("--write-fixture", type=Path, help="emit the parity fixture here")
    ap.add_argument("--unc", type=float, default=0.005,
                    help="assumed per-leg belief.uncertainty for the replay (half of a 1c spread)")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--history", type=Path, default=REPO / "data" / "history")
    ap.add_argument("--boot", type=int, default=10_000)
    ap.add_argument("--resolution", type=float, default=0.01,
                    help="sum of the two legs' belief.uncertainty for the stress verdicts"
                         " (two 1c-spread books = 2 x 0.005)")
    ap.add_argument("--out", type=Path, help="write the backtest summary JSON here")
    args = ap.parse_args()

    if args.contexts or args.fixture:
        if args.fixture:
            fx = json.load(open(args.fixture, encoding="utf-8"))
            contexts = fx["contexts"]
            unc = fx.get("uncertainty", args.unc)
        else:
            contexts = load_contexts(args.contexts, args.pairs)
            unc = args.unc
        results = replay(contexts, unc)
        print_replay(results)
        if args.write_fixture:
            fixture = {
                "note": "26 recorded fill contexts (decisions.context_json leg mids, rfqs legs);"
                        " expectations from tools/proto_structural_fit_bar.py (build 2026-09-04)",
                "uncertainty": unc,
                "btts_total_old": SHIPPED_BTTS_TOTAL,
                "btts_total_new": CLUB_MEASURED_BTTS_TOTAL,
                "contexts": [
                    {**{k: r[k] for k in ("rfq_id", "market_ticker", "quote_at", "target_cost_cc",
                                          "contracts_centi", "legs", "leg_mids_cc",
                                          "recorded_fair_cc", "recorded_no_bid_cc",
                                          "recorded_yes_bid_cc")},
                     "relationship": r["relationship"],
                     "same_game_btts_total": r["same_game_btts_total"],
                     "old_route": r["old_route"], "old_p": r["old_p"],
                     "promoted_p": r["promoted_p"],
                     "new_route": r["new_route"], "new_p": r["new_p"],
                     "verdict": r["verdict"]}
                    for r in results
                ],
            }
            args.write_fixture.parent.mkdir(parents=True, exist_ok=True)
            json.dump(fixture, open(args.write_fixture, "w", encoding="utf-8"), indent=1)
            print(f"wrote fixture {args.write_fixture} ({len(results)} contexts)")
    if args.backtest:
        summary = backtest(args.history, n_boot=args.boot, resolution=args.resolution)
        if args.out:
            json.dump(summary, open(args.out, "w", encoding="utf-8"), indent=1)
            print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
