"""ARG-champion x Messi-1+ fair-vs-field decomposition (2026-07-19, pre-final).

Operator question: the field quotes yes:KXMENWORLDCUP-26-AR +
yes:KXWCGOAL-26JUL19ESPARG-ARGLMESSI10-1 at ~26.7c YES; our engine sells the
NO at 76.4-76.8 (YES ask 23.2-23.6) and one taker has bought ~$300 of it.
Decompose WHY our fair is low and locate the parameter.

MEASUREMENT ONLY (rule 8): imports and calls the LIVE pricing modules —
PricingEngine.compute_joint (the exact live dispatch: structural try_price ->
copula fallback), classify_legs, KalshiBookSource belief math on real
orderbook mirrors — plus the shipped Dixon-Coles model for the structural
cross-check. Edits nothing. REST usage is market-data GETs only (orderbooks,
trades); no RFQs are created. DB access is mode=ro.

Run: .venv/Scripts/python.exe tools/diagnostics/argmessi_fair_vs_field_20260719.py
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import numpy as np

from combomaker.core.clock import SystemClock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents, cc_from_dollars_str
from combomaker.core.quantity import qty_from_fp_str
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiApiError, KalshiRestClient
from combomaker.marketdata.metadata import MetadataCache
from combomaker.marketdata.orderbook import OrderbookMirror
from combomaker.ops.config import load_config
from combomaker.ops.dotenv import load_dotenv
from combomaker.pricing import dixon_coles as dc
from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
    MatchFormat,
    PlayerScores,
    Team,
    TeamWin,
    TotalOver,
    invert,
    joint_probability,
    marginal_probability,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import KalshiBookSource
from combomaker.pricing.markup import MarkupPolicy
from combomaker.pricing.relationships import classify_legs
from combomaker.rfq.models import Rfq, RfqLeg

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
DB = "data/combomaker-prod-live-wc.sqlite3"

CHAMP_AR = "KXMENWORLDCUP-26-AR"
CHAMP_ES = "KXMENWORLDCUP-26-ES"
MESSI = "KXWCGOAL-26JUL19ESPARG-ARGLMESSI10-1"
GAME_ARG = "KXWCGAME-26JUL19ESPARG-ARG"
GAME_ESP = "KXWCGAME-26JUL19ESPARG-ESP"
GAME_TIE = "KXWCGAME-26JUL19ESPARG-TIE"
BTTS = "KXWCBTTS-26JUL19ESPARG-BTTS"
TOTALS = [f"KXWCTOTAL-26JUL19ESPARG-{n}" for n in (2, 3, 4)]
COMBOS = [
    "KXMVESPORTSMULTIGAMEEXTENDED-S202609506CD2247-39F452DE826",
    "KXMVECROSSCATEGORY-S20263AB06A8E27D-39F452DE826",
]
FIELD_ASK = 0.267          # operator-observed field consensus (~3.74x)
FIELD_MAKER_MARKUP = 0.015  # typical tight-maker markup (7/14 price discovery)


class _StaticFeed:
    """Orderbook mirrors built from REST snapshots — the same OrderbookMirror
    the live feed maintains, so KalshiBookSource runs its EXACT belief math.
    Duck-types OrderbookFeed.book (KeyError on unknown ticker, per contract)."""

    def __init__(self) -> None:
        self.books: dict[str, OrderbookMirror] = {}

    def book(self, ticker: str) -> OrderbookMirror:
        return self.books[ticker]


def _levels(raw: object) -> list:
    """REST ``orderbook_fp`` side -> mirror levels via the SAME wire parsers the
    live WS feed uses (feed._parse_levels: cc_from_dollars_str + qty_from_fp_str)."""
    if not raw:
        return []
    return [(cc_from_dollars_str(str(p)), qty_from_fp_str(str(c))) for p, c in raw]  # type: ignore[union-attr]


async def _mirror(
    rest: KalshiRestClient, clock: SystemClock, ticker: str
) -> OrderbookMirror | None:
    try:
        r = await rest.get_orderbook(ticker)
    except KalshiApiError as exc:
        print(f"  [book] {ticker}: {exc}")
        return None
    ob = r.get("orderbook_fp") or {}
    m = OrderbookMirror(ticker, clock)
    m.apply_snapshot(_levels(ob.get("yes_dollars")), _levels(ob.get("no_dollars")))
    return m


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """Gaussian-copula rho that reproduces joint p_ab at marginals (p_a, p_b)."""
    def joint(rho: float) -> float:
        return gaussian_copula_joint_prob([p_a, p_b], np.array([[1.0, rho], [rho, 1.0]]))
    lo, hi = -0.99, 0.99
    if p_ab <= joint(lo):
        return lo
    if p_ab >= joint(hi):
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if joint(mid) < p_ab:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def copula_fair(p_a: float, p_b: float, rho: float) -> float:
    return gaussian_copula_joint_prob([p_a, p_b], np.array([[1.0, rho], [rho, 1.0]]))


async def main() -> None:
    load_dotenv()
    cfg = load_config(
        Path("config/prod-live-wc.local.yaml"), env="prod", mode="quote", confirm_live=True
    )
    clock = SystemClock()
    conv = Conventions(
        verified=True, source="ground_truth_fixture",
        maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
        maker_pays_own_bid=True, maker_is_taker_on_fill=False,
        combo_no_pays_complement=True,
    )
    feed = _StaticFeed()
    # Engine construction installs the armed pricing aliases (champion->advance).
    engine = PricingEngine(feed, None, conv, cfg.pricing)  # type: ignore[arg-type]

    signer = RequestSigner(Credentials.for_env("prod"), clock)
    async with KalshiRestClient(KALSHI, signer) as rest:  # type: ignore[arg-type]
        metadata = MetadataCache(rest, clock)

        # ---- 1. LIVE leg books ------------------------------------------------
        wanted = [CHAMP_AR, CHAMP_ES, MESSI, GAME_ARG, GAME_ESP, GAME_TIE, BTTS, *TOTALS]
        for t in wanted:
            m = await _mirror(rest, clock, t)
            if m is not None:
                feed.books[t] = m

        src = KalshiBookSource(feed)  # the live belief math (microprice+spread+thin)
        beliefs_all = {t: src.marginal(t) for t in feed.books}
        print("=" * 100)
        print("LIVE LEG BOOKS (KalshiBookSource microprice beliefs)")
        for t, b in beliefs_all.items():
            top = feed.books[t].top()
            bid = None if top.yes_bid_cc is None else int(top.yes_bid_cc) / 100
            ask = None if top.yes_ask_cc is None else int(top.yes_ask_cc) / 100
            if b is None:
                print(f"  {t:46} bid {bid} ask {ask}  -> NO BELIEF (one-sided/crossed)")
            else:
                print(f"  {t:46} bid {bid:5} ask {ask:5}  p={b.p:.4f} unc={b.uncertainty:.4f}")

        b_champ = beliefs_all.get(CHAMP_AR)
        b_messi = beliefs_all.get(MESSI)
        if b_champ is None or b_messi is None:
            raise SystemExit("missing champion or Messi belief — cannot reproduce fair")
        p_a, p_m = b_champ.p, b_messi.p
        b_es = beliefs_all.get(CHAMP_ES)
        if b_es is not None:
            print(f"  champion family sum AR+ES = {p_a + b_es.p:.4f} "
                  f"(renormalized P(ARG) = {p_a / (p_a + b_es.p):.4f})")

        # ---- 2. our fair through the LIVE engine dispatch --------------------
        legs = (
            RfqLeg(CHAMP_AR, "KXMENWORLDCUP-26", "yes", None),
            RfqLeg(MESSI, "KXWCGOAL-26JUL19ESPARG", "yes", None),
        )
        rfq = Rfq(
            rfq_id="diag-argmessi", market_ticker=COMBOS[0],
            event_ticker=COMBOS[0].rsplit("-", 1)[0], contracts=None,
            target_cost_cc=CentiCents(100_000), created_ts="",
            mve_collection_ticker="KXMVESPORTSMULTIGAMEEXTENDED-R", legs=legs, raw={},
        )
        relationship = classify_legs(rfq.legs, metadata)
        beliefs = [b_champ, b_messi]
        sides = ["yes", "yes"]
        joint = engine.compute_joint(rfq, beliefs, sides, relationship)
        print("\n" + "=" * 100)
        print("OUR FAIR — LIVE ENGINE PATH (PricingEngine.compute_joint on live books)")
        print(f"  relationship: {relationship.kind} groups={relationship.same_event_groups}")
        for n in relationship.notes:
            print(f"    rel note: {n}")
        if not hasattr(joint, "p"):
            raise SystemExit(f"engine declined: {joint}")
        print(f"  fair YES = {joint.p:.4f}  ({joint.p * 100:.2f}c)   "
              f"uncertainty {joint.uncertainty:.4f}")
        print(f"  frechet [{joint.frechet_lo:.4f}, {joint.frechet_hi:.4f}]")
        for n in joint.notes:
            print(f"    note: {n}")
        markup = MarkupPolicy.from_config(cfg.pricing.markup)
        sport, mk_cc = markup.markup_for([leg.market_ticker for leg in rfq.legs],
                                         fair_cc=int(round(joint.p * 10_000)))
        ask = joint.p + mk_cc / 10_000
        print(f"  markup: sport={sport} {mk_cc / 100:.1f}c -> YES ask ~{ask * 100:.2f}c "
              f"(NO bid ~{100 - ask * 100:.2f})")

        # decomposition of the copula fair
        our_cond = joint.p / p_a
        field_cond = FIELD_ASK / p_a
        field_fair = FIELD_ASK - FIELD_MAKER_MARKUP
        print("\n  DECOMPOSITION (conditional space)")
        print(f"    P(ARG champion)      market mid   = {p_a:.4f}")
        print(f"    P(Messi 1+)          market mid   = {p_m:.4f}   (full game incl ET)")
        print(f"    OUR joint            {joint.p:.4f} -> P(Messi|champ) = {our_cond:.4f}")
        print(f"    FIELD ask 26.7c      {FIELD_ASK:.4f} -> P(Messi|champ) = {field_cond:.4f}")
        print(f"    FIELD fair est       {field_fair:.4f} -> "
              f"P(Messi|champ) = {field_fair / p_a:.4f}")
        print(f"    independence         {p_a * p_m:.4f}")

        # rho ladder through the LIVE copula function
        print("\n  GAUSSIAN-COPULA RHO LADDER (live gaussian_copula_joint_prob at live marginals)")
        for label, rho in (("shipped advance|player_goal:same", 0.45),
                           ("shipped + band (0.45+0.15)", 0.60),
                           ("0.70", 0.70), ("0.80", 0.80), ("0.90", 0.90)):
            f = copula_fair(p_a, p_m, rho)
            print(f"    rho {rho:.2f} ({label:32}) -> fair {f:.4f} "
                  f"({f * 100:.2f}c) cond {f / p_a:.4f}")
        rho_field_ask = implied_rho(p_a, p_m, FIELD_ASK)
        rho_field_fair = implied_rho(p_a, p_m, field_fair)
        print(f"    rho matching FIELD ask 26.7c  = {rho_field_ask:.3f}")
        print(f"    rho matching FIELD fair ~{field_fair * 100:.1f}c = {rho_field_fair:.3f}")

        # ---- 3. Dixon-Coles structural cross-check ---------------------------
        # The combo alone is under-identified (1 team-level leg) — that is WHY
        # the live path fell back to the copula. Identify (lam_a, lam_b) from
        # the final's OTHER live books and read the SAME joint off the shipped
        # scoreline model (the engine's own preferred machinery).
        sc = cfg.pricing.structural
        adv = Advance(team=Team.B)                       # blob ESPARG: A=ESP, B=ARG
        ps = PlayerScores(team=Team.B, min_goals=1, include_et=True)

        def belief_p(t: str) -> float | None:
            b = beliefs_all.get(t)
            return None if b is None else b.p

        sets: list[tuple[str, list[tuple[object, float]]]] = []
        p_t3 = belief_p(TOTALS[1])
        if p_t3 is not None:
            sets.append(("advance+total3 (exact)",
                         [(adv, p_a), (TotalOver(3, include_et=False), p_t3)]))
        p_btts = belief_p(BTTS)
        if p_t3 is not None and p_btts is not None:
            sets.append(("advance+total3+btts (lsq)",
                         [(adv, p_a), (TotalOver(3, include_et=False), p_t3),
                          (Btts(include_et=False), p_btts)]))
        p_ml = belief_p(GAME_ARG)
        p_tie = belief_p(GAME_TIE)
        extra: list[tuple[object, float]] = []
        if p_ml is not None:
            extra.append((TeamWin(team=Team.B, include_et=False), p_ml))
        if p_tie is not None:
            extra.append((Draw(), p_tie))
        if p_t3 is not None and extra:
            sets.append(("advance+total3+game-ml/tie (lsq)",
                         [(adv, p_a), (TotalOver(3, include_et=False), p_t3), *extra]))

        print("\n" + "=" * 100)
        print("STRUCTURAL CROSS-CHECK — shipped Dixon-Coles, "
              "identified from the final's other books")
        print(f"  (dc_rho={sc.dc_rho} et_factor={sc.et_factor} pens={sc.pens_win_prob} "
              f"max_goals={sc.max_goals}; PlayerScores include_et=True, Advance incl pens)")
        struct_fairs: list[float] = []
        for label, team_targets in sets:
            targets = [*team_targets, (ps, p_m)]
            try:
                model = invert(
                    targets,  # type: ignore[arg-type]
                    dc_rho=sc.dc_rho, et_factor=sc.et_factor,
                    match_format=MatchFormat.KNOCKOUT, max_goals=sc.max_goals,
                    pens_win_a=sc.pens_win_prob, half_share=sc.half_share,
                )
            except dc.StructuralError as exc:
                print(f"  {label}: DECLINED ({exc})")
                continue
            q = model.shares[len(targets) - 1]
            pj = joint_probability(model.params, [(adv, True), (ps, True)], {1: q})
            pa_m = marginal_probability(model.params, adv)
            pm_m = marginal_probability(model.params, ps, share=q)
            cond = pj / pa_m
            rho_eq = implied_rho(pa_m, pm_m, pj)
            struct_fairs.append(pj)
            print(f"  {label}")
            print(f"    lam ESP={model.params.lam_a:.3f} ARG={model.params.lam_b:.3f} "
                  f"residual={model.residual:.4f}  Messi share q={q:.3f}")
            print(f"    model P(champ)={pa_m:.4f} P(Messi)={pm_m:.4f}  "
                  f"JOINT={pj:.4f} ({pj * 100:.2f}c)  cond={cond:.4f}  implied rho={rho_eq:.3f}")
            # narrative decomposition off the model's own state enumeration
            states = dc._states(model.params)
            adv_ind = dc._team_indicator(states, adv, model.params)
            n_arg = states.b90 + states.b_et
            w = states.w
            p_champ_m = float((w * adv_ind).sum())
            e_goals = float((w * n_arg).sum())
            e_goals_c = float((w * adv_ind * n_arg).sum()) / p_champ_m
            p_messi_state = 1.0 - np.power(1.0 - q, n_arg)
            p_champ_0 = float((w * adv_ind * (n_arg == 0)).sum())
            print(f"    E[ARG goals]={e_goals:.3f}  E[ARG goals|champ]={e_goals_c:.3f}  "
                  f"P(champ with ARG scoreless)={p_champ_0:.4f} "
                  f"({p_champ_0 / p_champ_m:.1%} of champ states — 0-0 pens dilution)")
            cond_check = float((w * adv_ind * p_messi_state).sum()) / p_champ_m
            print(f"    P(Messi|champ) check={cond_check:.4f}")

        if struct_fairs:
            lo, hi = min(struct_fairs), max(struct_fairs)
            print(f"\n  STRUCTURAL fair range: {lo * 100:.2f}c - {hi * 100:.2f}c "
                  f"(vs copula {joint.p * 100:.2f}c, field ask {FIELD_ASK * 100:.1f}c)")
            print(f"  structural-implied rho range: "
                  f"{implied_rho(p_a, p_m, lo):.3f} - {implied_rho(p_a, p_m, hi):.3f}")

        # what per-goal Messi share IN ARG-WIN STATES would the field's
        # conditional require? (DC thinning holds q constant; the field needs
        # q to RISE conditional on the win — quantify by how much.)
        if sets:
            model = invert(
                [*sets[0][1], (ps, p_m)],  # type: ignore[arg-type]
                dc_rho=sc.dc_rho, et_factor=sc.et_factor,
                match_format=MatchFormat.KNOCKOUT, max_goals=sc.max_goals,
                pens_win_a=sc.pens_win_prob, half_share=sc.half_share,
            )
            states = dc._states(model.params)
            adv_ind = dc._team_indicator(states, adv, model.params)
            n_arg = states.b90 + states.b_et
            w = states.w
            p_champ_m = float((w * adv_ind).sum())

            def cond_at_share(qc: float) -> float:
                return float((w * adv_ind * (1.0 - np.power(1.0 - qc, n_arg))).sum()) / p_champ_m

            q0 = model.shares[len(sets[0][1])]
            print(f"\n  SHARE-IN-WIN-STATES REQUIRED (set A grid; unconditional q={q0:.3f})")
            for label, target in (("our copula cond 0.514", 0.5144),
                                  ("field fair est cond", (FIELD_ASK - FIELD_MAKER_MARKUP) / p_a),
                                  ("field ask cond", FIELD_ASK / p_a)):
                lo_q, hi_q = 0.01, 0.94
                for _ in range(60):
                    mid = (lo_q + hi_q) / 2
                    if cond_at_share(mid) < target:
                        lo_q = mid
                    else:
                        hi_q = mid
                print(f"    conditional {target:.4f} ({label:22}) needs q_win = {lo_q:.3f}")

        # ---- 4. combo market prints (who else traded?) -----------------------
        print("\n" + "=" * 100)
        print("COMBO MARKET PRINTS (GET /markets/trades)")
        for ticker in COMBOS:
            try:
                trades = (await rest.get_trades(ticker=ticker, limit=1000)).get("trades", [])
            except KalshiApiError as exc:
                print(f"  {ticker}: {exc}")
                continue
            by_price: dict[float, float] = {}
            for t in trades:
                yes_c = float(t.get("yes_price_dollars") or 0) * 100
                ct = float(t.get("count_fp") or 0)
                by_price[yes_c] = by_price.get(yes_c, 0.0) + ct
            total_ct = sum(by_price.values())
            print(f"  {ticker}: {len(trades)} prints, {total_ct:.0f}ct")
            for yes_c in sorted(by_price):
                ours = "  <- OUR price band" if 23.1 <= yes_c <= 23.95 else ""
                print(f"    yes {yes_c:5.2f}c  x {by_price[yes_c]:8.2f}ct{ours}")
            if trades:
                newest, oldest = trades[0], trades[-1]
                print(f"    window: {oldest.get('created_time')} .. {newest.get('created_time')}")
                cheap = [t for t in trades
                         if float(t.get("yes_price_dollars") or 1) * 100 < 24.0]
                if cheap:
                    print(f"    sub-24c prints: {len(cheap)} "
                          f"({cheap[-1].get('created_time')} .. {cheap[0].get('created_time')})")

    # ---- 5. our fill cluster (mode=ro DB) + P&L consequence ------------------
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "select at, combo_ticker, contracts_centi, price_cc, expected_edge_cc "
        "from fills where combo_ticker like '%39F452DE826%' order by at"
    ).fetchall()
    ct_total = sum(r[2] for r in rows) / 100.0
    cost_cc = sum(r[2] * r[3] for r in rows) / 100.0  # centi-ct * cc -> cc*ct/100
    avg_no = cost_cc / ct_total / 100.0 if ct_total else 0.0
    booked_edge_cc = sum(r[4] or 0 for r in rows)
    print("\n" + "=" * 100)
    print(f"OUR FILL CLUSTER on -39F452DE826: {len(rows)} fills, {ct_total:.2f}ct NO, "
          f"avg NO price {avg_no:.2f}c, premium ${cost_cc * ct_total and cost_cc / 10_000 * 1:.2f}")
    print(f"  booked expected edge (engine fair): ${booked_edge_cc / 10_000:.2f}")
    for label, p_true in (("field ask 26.7c", FIELD_ASK),
                          ("field fair est", FIELD_ASK - FIELD_MAKER_MARKUP),
                          ("structural hi", max(struct_fairs) if struct_fairs else FIELD_ASK)):
        pnl_cc = sum(r[2] * ((10_000 - p_true * 10_000) - r[3]) for r in rows) / 100.0
        print(f"  if TRUE fair YES = {p_true * 100:.2f}c ({label}): "
              f"expected P&L = ${pnl_cc / 10_000:+.2f}")


asyncio.run(main())
