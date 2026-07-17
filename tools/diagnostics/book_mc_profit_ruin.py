"""Offline book MC — P(profit) and P(ruin) of the CURRENT committed book.

Operator ask (2026-07-17): "run MC against our current book and check percent
chance of profit and ruin."

Rebuilds the live book the SAME way the bot does (hard rule 8 — live modules
imported, never reimplemented; keep in sync with quote_app._rehydrate_exposure_book
and lifecycle._build_book_risk_inputs):
  positions  <- Store.held_positions (DB fills ∩ open games; reconcile was CLEAN
                0-mismatch at the 02:20:47Z relaunch, so DB == exchange)
  marginals  <- freshest leg_mids_cc from the live decisions tape (the bot is
                actively quoting these games, so mids are seconds-to-minutes old)
  rho        <- the pricer's own sgp_within_game_rho_provider(engine.sgp_params)
  structural <- StructuralConfigView from the armed yaml (same DC constants)
  equity     <- REST get_balance (read-only) + modeled cost basis (P1-3 basis)
  MC         <- sim.book_risk.compute_book_risk (the exact live function)

Read-only everywhere: sqlite mode=ro, one GET /portfolio/balance.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.ops.config import load_config
from combomaker.ops.dotenv import load_dotenv
from combomaker.ops.persistence import Store
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.conventions import Side
from combomaker.core.conventions import Conventions
from combomaker.pricing.grouping import game_key
from combomaker.pricing.legtypes import set_pricing_aliases
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import compute_book_risk, modeled_cost_basis_cc
from combomaker.sim.structural_book import StructuralConfigView

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
DB = "data/combomaker-prod-live-wc.sqlite3"
OPEN_GAMES = {"26JUL18FRAENG", "26JUL19ESPARG"}  # the live slate
N_SAMPLES = 400_000  # offline: buy a tighter tail than the live 100k default


def latest_marginals(conn: sqlite3.Connection, since: str) -> dict[str, float]:
    """Freshest YES mid per leg ticker from quote decisions' leg_mids_cc."""
    mids: dict[str, float] = {}
    rows = conn.execute(
        "select context_json from decisions where at >= ? and kind='quote_sent' "
        "order by at desc limit 20000",
        (since,),
    )
    for (cj,) in rows:
        try:
            ctx = json.loads(cj)
        except (TypeError, ValueError):
            continue
        for ticker, cc in (ctx.get("leg_mids_cc") or {}).items():
            mids.setdefault(ticker, float(cc) / 10_000.0)  # newest-first wins
    return mids


async def main() -> None:
    load_dotenv()
    cfg = load_config(
        Path("config/prod-live-wc.local.yaml"),
        env="prod", mode="quote", confirm_live=True,
    )
    clock = SystemClock()
    # Install the armed pricing aliases so game_key resolves champion legs the
    # same way the live bot does (KXMENWORLDCUP-26 -> the final's game).
    set_pricing_aliases(dict(cfg.pricing.leg_pricing_aliases))

    # --- positions, the rehydration way (keep in sync with quote_app) --------
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tickers = [r[0] for r in conn.execute("select distinct combo_ticker from fills")]
    store = await Store.open(Path(DB), clock)
    try:
        held = await store.held_positions(tickers)
    finally:
        await store.close()
    positions: list[OpenPosition] = []
    for h in held:
        legs = tuple(
            LegRef(
                market_ticker=leg["market_ticker"],
                event_ticker=leg.get("event_ticker"),
                side=leg.get("side", "yes"),
            )
            for leg in h["legs"]
        )
        games = {game_key(x.event_ticker) for x in legs if x.event_ticker}
        if not games or not games <= OPEN_GAMES:
            continue  # settled/foreign-slate holdings are not the live book
        positions.append(
            OpenPosition(
                position_id=f"mcdiag:{h['combo_ticker']}",
                combo_ticker=h["combo_ticker"],
                collection=h["collection"],
                our_side=Side.NO if h["our_side"] == "no" else Side.YES,
                contracts=CentiContracts(int(h["contracts_centi"])),
                entry_price_cc=CentiCents(int(h["entry_price_cc"])),
                legs=legs,
                risk_modeled=True,
            )
        )

    # --- marginals from the live tape ----------------------------------------
    mids = latest_marginals(conn, since="2026-07-17T01:00")
    missing = sorted(
        {x.market_ticker for p in positions for x in p.legs} - set(mids)
    )

    # --- the pricer's rho + structural constants ------------------------------
    rho = None
    try:
        from combomaker.pricing.engine import PricingEngine
        from combomaker.sim.within_game_rho import sgp_within_game_rho_provider

        conv = Conventions(
            verified=True, source="ground_truth_fixture",
            maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
            maker_pays_own_bid=True, maker_is_taker_on_fill=False,
            combo_no_pays_complement=True,
        )
        engine = PricingEngine(None, None, conv, cfg.pricing)  # type: ignore[arg-type]
        rho = sgp_within_game_rho_provider(engine.sgp_params)
    except Exception as exc:  # noqa: BLE001 — diagnostic fallback, printed loudly
        print(f"WARN: pricer rho unavailable ({exc!r}) — flat-band fallback")
    _sc = cfg.pricing.structural
    scv = StructuralConfigView(
        dc_rho=_sc.dc_rho, et_factor=_sc.et_factor, pens_win_a=_sc.pens_win_prob,
        half_share=_sc.half_share, max_goals=_sc.max_goals,
        knockout_series=tuple(_sc.knockout_series), enabled=_sc.enabled,
        corners_et_loading=_sc.corners_et_loading,
    )

    # --- live cash (read-only) -------------------------------------------------
    signer = RequestSigner(Credentials.for_env("prod"), clock)
    async with KalshiRestClient(KALSHI, signer) as rest:  # type: ignore[arg-type]
        bal = await rest.get_balance()
    cash_cents = int(bal.get("balance", 0))
    cash_cc = cash_cents * 100

    # --- the exact live MC ------------------------------------------------------
    model = build_book_model(
        positions, marginals=lambda t: mids.get(t), within_game_rho=rho
    )
    equity_cc = cash_cc + int(round(modeled_cost_basis_cc(model)))
    snap = compute_book_risk(
        model,
        n_samples=N_SAMPLES,
        seed=cfg.risk.book_risk_seed if hasattr(cfg.risk, "book_risk_seed") else 0,
        band="high",
        bankroll_cc=cash_cc,  # ruin fractions vs live cash basis
        structural_cfg=scv,
        current_equity_cc=equity_cc,
        ruin_floor_frac=0.70,  # equity < 70% of bankroll = the −30% floor
        ruin_prob_ci_z=1.645,  # 95% one-sided upper bound, like the live cap
    )

    print(f"\n=== CURRENT BOOK ({len(positions)} positions, {N_SAMPLES:,} MC paths, structural={scv.enabled}) ===")
    at_risk = 0
    for p in positions:
        # Long binary either side: max loss = the premium paid.
        loss = int(p.contracts) * int(p.entry_price_cc) // 100
        at_risk += loss
        legs_s = " + ".join(
            f"{x.side}:{x.market_ticker.split('-', 1)[-1][:32]}" for x in p.legs
        )
        print(f"  {p.our_side.value:>3} {int(p.contracts)/100:8.2f}ct @ {int(p.entry_price_cc)/100:6.2f}c  maxloss ${loss/10000:8.2f}  {legs_s[:100]}")
    if missing:
        print(f"WARN missing marginals (model unknown-flagged): {missing}")
    print(f"\ncash ${cash_cc/10000:,.2f} | cost-basis equity ${equity_cc/10000:,.2f} | book worst-case ${at_risk/10000:,.2f}")
    print(f"model unknown={model.unknown}  snapshot usable={snap.usable}")
    print(f"\nP(book profits)              : {snap.p_profit:8.2%}")
    print(f"P(ruin: equity < 70% bank)   : {snap.p_ruin:8.4%}   (95% upper {snap.p_ruin_upper:.4%})")
    print(f"ES99 (structural production) : ${int(snap.es_99_cc)/10000:,.2f}")
    print(f"ES99 (challenger)            : ${int(snap.challenger_es_99_cc)/10000:,.2f}")
    print(f"deterministic max loss       : ${int(snap.deterministic_max_loss_cc)/10000:,.2f}")
    for field in ("mean_pnl_cc", "median_pnl_cc", "p05_pnl_cc", "p95_pnl_cc"):
        if hasattr(snap, field):
            print(f"{field:29}: ${getattr(snap, field)/10000:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
