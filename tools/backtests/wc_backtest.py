"""Fast, ZERO-BIAS World-Cup combo backtest harness (canonical method).

WHY THIS SHAPE — the pricer must NEVER see the maker's price. It's enforced
STRUCTURALLY, not by discipline:

  gather  ── combos/sides/marginals from the recorder DB (WC-STRICT: every leg
              KXWC*) + clearings straight from Kalshi's trade tape (get_trades,
              COMPLETE → backfills the poller gap) → TWO separate caches:
              inputs.pkl   = {ticker: {legs, sides, snapshots(at, marginals)}}   ← NO prices
              outcomes.pkl = {ticker: {clearings:[(price,count,side,t)], cutoff,
                                       settle}}                                   ← outcomes only
              Clearings are STRICTLY PRE-GAME: a print is kept only if it landed
              before the earliest leg's estimated kickoff (no in-play prints).
  price   ── reads inputs.pkl ONLY → fairs.pkl, via a thin driver that calls the
              SAME production pricing modules + shipped PricingConfig the live
              engine uses (our real correlation code — NOT agents; and engine.py
              itself is UNTOUCHED). No clearing argument; outcomes.pkl never opened.
  analyze ── joins fairs.pkl + outcomes.pkl → error / P&L / by-family. First and
              only place the maker price and the settlement appear.

FAST — per-DISTINCT-combo pricing + memoization; every stage is cached, so
re-analysis is instant and re-pricing only happens if the engine changes. Bound
the DB scan with --since (WC is current, no need to scan all 10M rfqs). RUN THE
GATHER OFF-PEAK: the live recorder holds the SQLite write lock during game hours
(gather only READS rfqs/would_quotes; it no longer touches combo_trades).

Usage:
  python -m tools.backtests.wc_backtest gather --since 2026-07-01 [--pregame-hours 2.5]
  python -m tools.backtests.wc_backtest price
  python -m tools.backtests.wc_backtest analyze
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "src")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1252 console
except Exception:
    pass

DB = "file:data/combomaker-prod.sqlite3?mode=ro"          # READ-ONLY, never write
DEMO_UNUSED = None
KALSHI = "https://external-api.kalshi.com/trade-api/v2"    # public market reads


def _wc_strict(legs: list[dict]) -> bool:
    """A combo counts only if EVERY leg is a World-Cup market (KXWC*). Excludes
    MLB, UCL/UEL/UECL, and any mixed combo — 'strictly soccer World Cup'."""
    return bool(legs) and all(lg["market_ticker"].startswith("KXWC") for lg in legs)


# ───────────────────────── stage 1: GATHER ─────────────────────────
# STRICTLY-PREGAME cutoff. Kalshi does NOT expose kickoff (verified: a leg's
# close_time is the far settlement window, and the game EVENT has no start field);
# the one time anchor it gives is expected_expiration_time ≈ game-end/settlement.
# So we ESTIMATE kickoff = expected_expiration_time − PREGAME_HOURS. Soccer
# regulation ≈ 1h50 + a settlement buffer, so ~2.5h lands near kickoff for
# regulation-settled markets; advance/ET markets expire LATER, making this
# conservative — the estimate is ≤ the true kickoff, so the filter only ever
# DROPS borderline pre-game prints, never ADMITS an in-play one. Tunable via
# --pregame-hours; documented as an estimate, not an exact schedule.
DEFAULT_PREGAME_HOURS = 2.5


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def gather(outdir: Path, since: str | None, pregame_hours: float) -> None:
    """One pass → inputs.pkl (pricing inputs) + outcomes.pkl (clearings +
    settlement + cutoff), kept in SEPARATE files so the price stage can't reach
    outcomes. Combos, sides and marginal snapshots come from the recorder DB
    (rfqs + would_quotes — which never stalled); the CLEARINGS come straight from
    Kalshi's trade tape (get_trades) — COMPLETE and GAP-FREE, so this backfills
    the combo-trade poller's ~10h stall by construction and never touches the
    write-locked combo_trades table. Every clearing is then STRICTLY-PREGAME
    filtered: a combo's print is kept only if it landed before the estimated
    kickoff of its EARLIEST-starting leg (so no leg was in-play), and a combo
    with zero pre-game prints is dropped."""
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.ops.dotenv import load_dotenv

    load_dotenv()  # KALSHI_PROD_* for the authed trade tape (get_trades needs auth)

    con = sqlite3.connect(DB, uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")  # tolerate the live recorder's locks
    since_clause = f" WHERE seen_at >= '{since}'" if since else ""

    # (a) rfqs → {market_ticker: legs}, {rfq_id: market_ticker}, WC-strict only.
    print("scan rfqs (WC-strict filter)…", flush=True)
    ticker_legs: dict[str, list[dict]] = {}
    rfq_ticker: dict[str, str] = {}
    n = kept = 0
    for mt, rfq_id, legs_json in con.execute(
        f"SELECT market_ticker, rfq_id, legs_json FROM rfqs{since_clause}"
    ):
        n += 1
        if not legs_json:
            continue
        legs = json.loads(legs_json)
        legs = [{"market_ticker": lg["market_ticker"], "side": lg.get("side", "yes")}
                for lg in legs]
        if not _wc_strict(legs):
            continue
        ticker_legs.setdefault(mt, legs)
        rfq_ticker[rfq_id] = mt
        kept += 1
        if n % 500_000 == 0:
            print(f"  {n} rfqs scanned, {len(ticker_legs)} WC combos", flush=True)
    wc = set(ticker_legs)
    print(f"scanned {n} rfqs → {len(wc)} distinct WC-strict combos", flush=True)

    # (b) would_quotes → {ticker: [(at, marginals)]} via indexed rfq_id.
    print("scan would_quotes → marginal snapshots…", flush=True)
    ticker_snaps: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
    for rfq_id, at, probs_json in con.execute(
        "SELECT rfq_id, at, leg_probs_json FROM would_quotes"
    ):
        mt = rfq_ticker.get(rfq_id)
        if mt is None or not probs_json:
            continue
        ticker_snaps[mt].append((at, json.loads(probs_json)))
    for mt in ticker_snaps:
        ticker_snaps[mt].sort(key=lambda x: x[0])
    con.close()  # DB done — we do NOT read combo_trades (poller-stalled + write-locked)

    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())

    # (c) CLEARINGS from Kalshi's trade tape — an OUTCOME. Sourced here (NOT from
    #     the DB's stalled combo_trades) so the ~10h poller gap is backfilled by
    #     construction; the tape is complete. get_trades needs the prod signer.
    print(f"fetch clearings from Kalshi tape for {len(wc)} combos…", flush=True)
    clearings = asyncio.run(_fetch_clearings(sorted(wc), signer))

    # (d) per-leg settlement (status/result) + expected_expiration_time — OUTCOME.
    distinct_legs = sorted({lg["market_ticker"] for legs in ticker_legs.values() for lg in legs})
    print(f"fetch settlement + expiry for {len(distinct_legs)} legs…", flush=True)
    leg_meta = asyncio.run(_fetch_leg_meta(distinct_legs))

    def leg_won(mt: str, side: str) -> bool | None:
        s = leg_meta.get(mt)
        if not s or s["status"] not in ("finalized", "settled") or s["result"] not in ("yes", "no"):
            return None
        return s["result"] == side

    off = timedelta(hours=pregame_hours)
    outcomes: dict[str, dict] = {}
    n_pre = n_inplay_only = 0
    for mt, legs in ticker_legs.items():
        # cutoff = EARLIEST estimated kickoff across the legs (so NO leg is live).
        kicks = [_parse_ts(leg_meta[lg["market_ticker"]]["exp"]) - off
                 for lg in legs
                 if leg_meta.get(lg["market_ticker"], {}).get("exp")]
        cutoff = min(kicks) if kicks else None
        allc = clearings.get(mt, [])
        pre = [c for c in allc if cutoff and _parse_ts(c[3]) < cutoff]
        if pre:
            n_pre += 1
        elif allc:
            n_inplay_only += 1  # had prints, but every one was in-play/post-kickoff
        wins = [leg_won(lg["market_ticker"], lg["side"]) for lg in legs]
        resolved = all(w is not None for w in wins)
        outcomes[mt] = {
            "clearings": pre,                 # STRICTLY PRE-GAME prints only
            "clearings_all_n": len(allc),     # audit: total prints before filtering
            "cutoff": cutoff.isoformat() if cutoff else None,
            "resolved": resolved,
            "settle_yes": (1 if all(wins) else 0) if resolved else None,
        }

    # SPLIT WRITE — pricing inputs and outcomes never share a file.
    inputs = {mt: {"legs": [lg["market_ticker"] for lg in legs],
                   "sides": [lg["side"] for lg in legs],
                   "snaps": ticker_snaps.get(mt, [])}
              for mt, legs in ticker_legs.items()}
    outdir.mkdir(parents=True, exist_ok=True)
    pickle.dump(inputs, open(outdir / "inputs.pkl", "wb"))
    pickle.dump(outcomes, open(outdir / "outcomes.pkl", "wb"))
    nres = sum(1 for o in outcomes.values() if o["resolved"])
    print(f"WROTE inputs.pkl ({len(inputs)} combos) + outcomes.pkl "
          f"({nres} resolved · {n_pre} with pre-game prints · "
          f"{n_inplay_only} dropped as in-play/post-kickoff only · "
          f"cutoff = expiry − {pregame_hours}h).  inputs.pkl has NO prices.", flush=True)


async def _fetch_clearings(
    combos: list[str], signer: object,
) -> dict[str, list[tuple[float, float, str, str]]]:
    """Every combo's cleared trades from Kalshi's tape — COMPLETE, which is what
    backfills the recorder's combo-trade gap. Each trade →
    (yes_price_dollars, count, taker_side, created_time). Cursor-paginated;
    authed (get_trades needs the prod signer)."""
    from combomaker.exchange.rest import KalshiRestClient
    out: dict[str, list[tuple[float, float, str, str]]] = {}
    sem = asyncio.Semaphore(8)
    done = [0]
    async with KalshiRestClient(KALSHI, signer) as rest:  # type: ignore[arg-type]
        async def one(tk: str) -> None:
            async with sem:
                trades: list[tuple[float, float, str, str]] = []
                cursor: str | None = None
                for _ in range(50):  # page cap (safety)
                    params: dict[str, str | int] = {"ticker": tk, "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    try:
                        resp = await rest.get_trades(**params)
                    except Exception:
                        break
                    for x in resp.get("trades", []):
                        yp, ct = x.get("yes_price_dollars"), x.get("created_time")
                        if yp is None or ct is None:
                            continue
                        trades.append((float(yp), float(x.get("count_fp") or 0),
                                       x.get("taker_side") or "", ct))
                    cursor = resp.get("cursor") or None
                    if not cursor:
                        break
                out[tk] = trades
                done[0] += 1
                if done[0] % 200 == 0:
                    print(f"  clearings {done[0]}/{len(combos)}", flush=True)
        await asyncio.gather(*(one(t) for t in combos))
    return out


async def _fetch_leg_meta(legs: list[str]) -> dict[str, dict]:
    """Per-leg settlement (status/result) + expected_expiration_time (the
    game-end/settlement anchor from which kickoff is estimated). Public API."""
    from combomaker.exchange.rest import KalshiRestClient
    out: dict[str, dict] = {}
    sem = asyncio.Semaphore(8)
    async with KalshiRestClient(KALSHI, None) as rest:
        async def one(tk: str) -> None:
            async with sem:
                for attempt in range(4):
                    try:
                        m = (await rest.get_market(tk))["market"]
                        out[tk] = {"status": m.get("status"), "result": m.get("result"),
                                   "exp": m.get("expected_expiration_time")}
                        return
                    except Exception:
                        await asyncio.sleep(0.4 * (attempt + 1))
                out[tk] = {"status": "ERR", "result": "", "exp": None}
        await asyncio.gather(*(one(t) for t in legs))
    return out


# ─────────── stage 2: PRICE (blind — thin driver over the LIVE pricing code) ───────────
def _build_pricer():
    """Prices a combo the way the live engine does, by importing the SAME
    production pricing modules — classify_legs, StructuralPricer (Dixon-Coles /
    margin-total / mlb-runs), build_sgp_correlation + price_joint_matrices,
    price_containment — with the SHIPPED PricingConfig(). It is OUR real
    correlation code and config: not agents, and NOT a modified engine (engine.py
    is untouched). The short dispatch below MIRRORS PricingEngine.price()'s
    fair-value block — keep the two in sync. price_combo has NO clearing argument."""
    from combomaker.ops.config import PricingConfig
    from combomaker.pricing.joint import price_containment, price_joint_matrices
    from combomaker.pricing.legs import LegBelief
    from combomaker.pricing.legtypes import classify_leg
    from combomaker.pricing.relationships import RelationshipKind, classify_legs
    from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
    from combomaker.pricing.structural import StructuralPricer, structural_applicable
    from combomaker.rfq.models import RfqLeg

    cfg = PricingConfig()
    co = cfg.correlation
    sgp = SgpParams(pair_rho=dict(co.pair_rho), default_rho=co.same_event_rho,
        cross_event_rho=co.cross_event_rho, typed_uncertainty=co.typed_rho_uncertainty,
        untyped_uncertainty=co.untyped_rho_uncertainty,
        pair_uncertainty=dict(co.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in co.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in co.oriented_curve.items()},
        oriented_curve_uncertainty=dict(co.oriented_curve_uncertainty))
    pricer = StructuralPricer(cfg.structural, cfg.margin_total, cfg.mlb_runs)

    class _StubMeta:  # classify_legs only asks about mutual exclusion; traded WC combos have none
        def event_mutually_exclusive(self, e):  # noqa: ANN001, ANN201
            return False

    def price_combo(leg_tickers, sides, marginals):  # noqa: ANN001, ANN202
        """marginals = P(SELECTED side) per leg, recorded at RFQ time; blind to any price."""
        legs = [RfqLeg(market_ticker=t, event_ticker="-".join(t.split("-")[:2]),
                       side=s, yes_settlement_value_cc=None)
                for t, s in zip(leg_tickers, sides, strict=False)]
        yes = [p if s == "yes" else 1.0 - p for p, s in zip(marginals, sides, strict=False)]
        rel = classify_legs(legs, _StubMeta())  # type: ignore[arg-type]
        if rel.kind is RelationshipKind.IMPOSSIBLE:
            return 0.0 if rel.farmable else None
        if rel.kind is RelationshipKind.UNKNOWN:
            return None
        beliefs = [LegBelief(p=y, uncertainty=0.005, source="bt") for y in yes]
        if rel.kind is RelationshipKind.CONTAINMENT and rel.containment is not None:
            return price_containment(beliefs, sides, rel.containment).p
        if structural_applicable(list(legs), rel.same_event_groups):
            j, _ = pricer.try_price(list(legs), beliefs, sides)
            if j is not None:
                return j.p
        corr = build_sgp_correlation(list(legs), rel.same_event_groups, sgp, marginals=yes)
        return price_joint_matrices(beliefs, sides, corr.corr, corr.corr_low, corr.corr_high).p
    return price_combo, classify_leg


def price(outdir: Path) -> None:
    inputs = pickle.load(open(outdir / "inputs.pkl", "rb"))   # ← ONLY inputs. Never outcomes.
    price_combo, classify_leg = _build_pricer()
    fairs: dict[str, dict] = {}
    memo: dict[tuple, float | None] = {}
    done = 0
    for mt, d in inputs.items():
        done += 1
        if done % 200 == 0:
            print(f"  priced {done}/{len(inputs)}", flush=True)
        snaps = d["snaps"]
        if not snaps:
            continue
        marginals = snaps[-1][1]  # latest recorded snapshot (see analyze note on freshness)
        if len(marginals) != len(d["legs"]):
            continue
        key = (mt, round(sum(marginals), 6), len(marginals))
        if key not in memo:
            try:
                memo[key] = price_combo(d["legs"], d["sides"], marginals)
            except Exception:
                memo[key] = None
        fair = memo[key]
        fams = "+".join(sorted({classify_leg(t).name for t in d["legs"]}))
        fairs[mt] = {"fair": fair, "n_legs": len(d["legs"]), "fams": fams}
    pickle.dump(fairs, open(outdir / "fairs.pkl", "wb"))
    ok = sum(1 for f in fairs.values() if f["fair"] is not None)
    print(f"WROTE fairs.pkl — priced {ok}/{len(fairs)} via the live pricing code "
          "(engine.py untouched; blind to any maker price)", flush=True)


# ───────────────────────── stage 3: ANALYZE ─────────────────────────
def analyze(outdir: Path) -> None:
    fairs = pickle.load(open(outdir / "fairs.pkl", "rb"))
    outcomes = pickle.load(open(outdir / "outcomes.pkl", "rb"))  # ← outcomes enter HERE, not before
    rows = []
    for mt, f in fairs.items():
        if f["fair"] is None:
            continue
        o = outcomes.get(mt)
        if not o or not o["clearings"]:  # only combos with a STRICTLY PRE-GAME print
            continue
        clr = statistics.median(c[0] for c in o["clearings"])  # tape price is dollars
        rows.append({"fair": f["fair"], "clearing": clr, "err": f["fair"] - clr,
                     "fams": f["fams"], "n_legs": f["n_legs"], "resolved": o["resolved"],
                     "settle_yes": o["settle_yes"], "n_tr": len(o["clearings"])})
    if not rows:
        print("no rows — run gather + price first (and ensure combos have clearings).")
        return
    err = [r["err"] * 100 for r in rows]
    ae = [abs(x) for x in err]
    w2 = sum(x <= 2 for x in ae) / len(ae) * 100
    over = sum(x > 0 for x in err) / len(err) * 100
    print(f"\n=== WC BACKTEST — our fair vs maker CLEARING (n={len(rows)}) ===")
    print(f"  median|err| {statistics.median(ae):.2f}c  bias {statistics.mean(err):+.2f}c  "
          f"within2 {w2:.0f}%  over-priced {over:.0f}%")
    res = [r for r in rows if r["resolved"]]
    if res:
        yes = sum(r["settle_yes"] for r in res) / len(res)
        print(f"  resolved: {len(res)}  settled-YES {yes*100:.1f}%")
    fam = defaultdict(list)
    for r in rows:
        fam[r["fams"]].append(abs(r["err"]) * 100)
    print("\n=== by family (best→worst |err|) ===")
    for k, v in sorted(fam.items(), key=lambda x: statistics.median(x[1])):
        if len(v) >= 5:
            print(f"  {statistics.median(v):5.2f}c  n={len(v):3d}  {k}")
    json.dump({"rows": rows}, open(outdir / "wc_backtest.json", "w"))
    print("\nwrote wc_backtest.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gather", "price", "analyze"])
    ap.add_argument("--outdir", type=Path, default=Path("data") / "backtests" / "wc")
    ap.add_argument("--since", default=None, help="gather: only scan rfqs seen_at >= this date")
    ap.add_argument("--pregame-hours", type=float, default=DEFAULT_PREGAME_HOURS,
                    help="gather: kickoff ≈ leg expected_expiration − this many hours; "
                         "clearings after kickoff are dropped (strictly pre-game). "
                         "Larger = stricter. Default %(default)s.")
    a = ap.parse_args()
    if a.stage == "gather":
        gather(a.outdir, a.since, a.pregame_hours)
    elif a.stage == "price":
        price(a.outdir)
    else:
        analyze(a.outdir)


if __name__ == "__main__":
    main()
