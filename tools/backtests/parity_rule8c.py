"""RULE-8c PARITY CHECK (2026-07-11, WIRE-5): the backtest harness mirrors
(wc_backtest / mlb_backtest ``_build_pricer``) vs the LIVE
``PricingEngine.price`` on a stratified sample of REAL tape combos — exact
equality to the centi-cent (``cc_from_prob(harness_fair) == quote.fair_cc``).

Strata (>= 200 total): CONTAINMENT-multi collapse plans, containment windows
(NESTED_BAND incl. S2/S3/S12/S33-ny + ladder bands), WIRE-4
conditionals-embedded, S41 (tb x hrr-1) carriers, bare 2-leg containments,
plain OK combos (copula/structural regression control), plus
disposition-agreement strata (UNKNOWN declines, farmable impossibles).

The engine is fed synthetic two-sided books whose equal-size microprice is the
4dp-quantized tape marginal (yes_bid = m-1c, no_bid = (1-m)-1c), so both paths
price the SAME leg marginals; the harness receives the engine's OWN
KalshiBookSource beliefs converted to selected space (its tape contract).

READ-ONLY on caches; writes parity_rule8c.json next to this script's out dir.

Usage:
    uv run python tools/backtests/parity_rule8c.py <out_json>
"""
from __future__ import annotations

import asyncio
import json
import pickle
import random
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from combomaker.core.clock import FakeClock  # noqa: E402
from combomaker.core.conventions import DOC_ASSUMED  # noqa: E402
from combomaker.core.money import cc_from_prob  # noqa: E402
from combomaker.marketdata.feed import OrderbookFeed  # noqa: E402
from combomaker.marketdata.grid import PriceGrid  # noqa: E402
from combomaker.marketdata.metadata import EventMeta, MarketMeta, MetadataCache  # noqa: E402
from combomaker.ops.config import PricingConfig  # noqa: E402
from combomaker.pricing.engine import PricingEngine  # noqa: E402
from combomaker.pricing.legs import KalshiBookSource  # noqa: E402
from combomaker.pricing.quote import ConstructedQuote, NoQuote  # noqa: E402
from combomaker.pricing.relationships import RelationshipKind, classify_legs  # noqa: E402
from combomaker.rfq.models import Rfq, RfqLeg  # noqa: E402

CACHES = {
    "wc": Path(r"C:\Users\aahys\.claude\jobs\24844262\tmp\ph4\wc\wc_fixed_printed"),
    "mlb": Path(r"C:\Users\aahys\.claude\jobs\24844262\tmp\ph4\mlb"),
    "mixed": Path(r"C:\Users\aahys\.claude\jobs\24844262\tmp\ph4\mixed\mixed_fixed"),
}


class _Ws:
    """Minimal WsLike: enough for OrderbookFeed snapshots (mirrors the unit
    tests' FakeWs — no reconnects, one subscription ack)."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.subscriptions: list = []

    def on_message(self, msg_type, handler):  # noqa: ANN001, ANN201
        self.handlers.setdefault(msg_type, []).append(handler)

    def on_disconnect(self, handler):  # noqa: ANN001, ANN201
        pass

    def add_subscription(self, channels, *, on_subscribed=None, **kw):  # noqa: ANN001, ANN201, ANN003
        self.subscriptions.append(on_subscribed)

    async def ack(self, index: int, sid: int) -> None:
        cb = self.subscriptions[index]
        assert cb is not None
        await cb(sid)

    async def deliver(self, env) -> None:  # noqa: ANN001
        for h in self.handlers.get(str(env.get("type")), []):
            await h(env)


def _stub_meta_provider():  # noqa: ANN202
    class _P:
        def event_mutually_exclusive(self, e: str) -> bool:
            return False

    return _P()


def _legs_of(leg_tickers, sides):  # noqa: ANN001, ANN202
    return [
        RfqLeg(t, "-".join(t.split("-")[:2]), s, None)
        for t, s in zip(leg_tickers, sides, strict=False)
    ]


def _quantized_yes_cc(marginals_sel, sides):  # noqa: ANN001, ANN202
    """YES-space 4dp-quantized cc per leg from selected-space tape marginals,
    clamped so both synthetic book sides stay on the (0,1) 1c grid."""
    out = []
    for m, s in zip(marginals_sel, sides, strict=False):
        y = m if s == "yes" else 1.0 - m
        y_cc = round(y * 10_000)
        out.append(min(9_800, max(200, y_cc)))
    return out


async def _price_engine(engine_cfg, leg_tickers, sides, yes_cc):  # noqa: ANN001, ANN202
    """One fresh feed/metadata per combo; returns (fair_cc | None, kindstr,
    engine_yes_beliefs) — beliefs are the feed's own microprices (floats)."""
    ws = _Ws()
    clock = FakeClock()
    feed = OrderbookFeed(ws, clock)
    metadata = MetadataCache(None, clock)  # type: ignore[arg-type]
    feed.watch(list(leg_tickers))
    await ws.ack(0, 5)
    for seq, (t, y_cc) in enumerate(zip(leg_tickers, yes_cc, strict=False), start=1):
        yes_bid = (y_cc - 100) / 10_000
        no_bid = (10_000 - y_cc - 100) / 10_000
        await ws.deliver(
            {
                "type": "orderbook_snapshot",
                "sid": 5,
                "seq": seq,
                "msg": {
                    "market_ticker": t,
                    "yes_dollars_fp": [[f"{yes_bid:.4f}", "50.00"]],
                    "no_dollars_fp": [[f"{no_bid:.4f}", "50.00"]],
                },
            }
        )
    combo_ticker = "KXMVE-PARITY"
    metadata._markets[combo_ticker] = MarketMeta(  # noqa: SLF001 (tool seam)
        ticker=combo_ticker,
        status="active",
        grid=PriceGrid.from_market_payload(
            {
                "ticker": combo_ticker,
                "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
            }
        ),
        event_ticker="E",
        close_time=clock.now() + timedelta(seconds=21_600),
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    for t in leg_tickers:  # mirror the harness _StubMeta: nothing is exclusive
        ev = "-".join(t.split("-")[:2])
        metadata._events[ev] = EventMeta(  # noqa: SLF001 (tool seam)
            event_ticker=ev, mutually_exclusive=False, raw={}, fetched_mono_ns=0
        )
    engine = PricingEngine(feed, metadata, DOC_ASSUMED, engine_cfg)
    rfq = Rfq.from_ws(
        {
            "id": "rfq_parity",
            "market_ticker": combo_ticker,
            "created_ts": "2026-07-11T10:00:00Z",
            "contracts_fp": "10.00",
            "mve_collection_ticker": "KXMVEPARITY",
            "mve_selected_legs": [
                {"market_ticker": t, "side": s, "event_ticker": "-".join(t.split("-")[:2])}
                for t, s in zip(leg_tickers, sides, strict=False)
            ],
        }
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    src = KalshiBookSource(feed)
    beliefs = [src.marginal(t) for t in leg_tickers]
    yes_p = [b.p if b is not None else None for b in beliefs]
    if isinstance(result, ConstructedQuote):
        return int(result.fair_cc), ("farmed" if result.farmed else "quote"), yes_p
    assert isinstance(result, NoQuote)
    return None, f"noquote:{result.reason.name}", yes_p


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"C:\Users\aahys\.claude\jobs\24844262\tmp\ph4\wire2\parity_rule8c.json"
    )
    sys.path.insert(0, str(REPO / "tools" / "backtests"))
    import mlb_backtest  # noqa: PLC0415
    import wc_backtest  # noqa: PLC0415

    wc_price, _ = wc_backtest._build_pricer()  # noqa: SLF001
    mlb_price, _pr, _cl = mlb_backtest._build_pricer()  # noqa: SLF001

    inputs: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for name, base in CACHES.items():
        d = pickle.load(open(base / "inputs.pkl", "rb"))
        for mt, rec in d.items():
            usable = rec.get("snaps") and len(rec["snaps"][-1][1]) == len(rec["legs"])
            if mt not in inputs and usable:
                inputs[mt] = rec
                origin[mt] = name
    print(f"combined caches: {len(inputs)} combos with a final pregame snapshot")

    provider = _stub_meta_provider()
    strata: dict[str, list[str]] = {
        "plan_collapse": [], "window_band": [], "conditional_embedded": [],
        "s41_carrier": [], "bare_containment": [], "impossible_farmable": [],
        "impossible_not_farmable": [], "unknown_decline": [], "plain_ok": [],
    }

    def s41_pair(legs) -> bool:  # noqa: ANN001
        tb = {}
        hrr1 = set()
        for t in legs:
            parts = t.upper().split("-")
            if len(parts) != 4 or not parts[3].isdigit():
                continue
            series = parts[0]
            key = (parts[1], parts[2])
            if series == "KXMLBTB":
                tb[key] = int(parts[3])
            elif series == "KXMLBHRR" and int(parts[3]) == 1:
                hrr1.add(key)
        return any(k in hrr1 for k in tb)

    for mt, rec in inputs.items():
        rel = classify_legs(_legs_of(rec["legs"], rec["sides"]), provider)
        if s41_pair(rec["legs"]):
            strata["s41_carrier"].append(mt)
            continue
        if rel.kind is RelationshipKind.CONTAINMENT and rel.conditionals:
            strata["conditional_embedded"].append(mt)
        elif rel.kind is RelationshipKind.CONTAINMENT and rel.containment is not None:
            strata["bare_containment"].append(mt)
        elif rel.kind is RelationshipKind.CONTAINMENT:
            strata["plan_collapse"].append(mt)
        elif rel.kind is RelationshipKind.NESTED_BAND:
            strata["window_band"].append(mt)
        elif rel.kind is RelationshipKind.IMPOSSIBLE and rel.farmable:
            strata["impossible_farmable"].append(mt)
        elif rel.kind is RelationshipKind.IMPOSSIBLE:
            strata["impossible_not_farmable"].append(mt)
        elif rel.kind is RelationshipKind.UNKNOWN:
            strata["unknown_decline"].append(mt)
        else:
            strata["plain_ok"].append(mt)

    print("strata population:", {k: len(v) for k, v in strata.items()})

    rng = random.Random(20260711)
    quota = {
        "plan_collapse": 60, "window_band": 60, "conditional_embedded": 45,
        "s41_carrier": 25, "bare_containment": 20, "impossible_farmable": 8,
        "impossible_not_farmable": 8, "unknown_decline": 10, "plain_ok": 40,
    }
    sample: list[tuple[str, str]] = []
    for stratum, tickers in strata.items():
        pool = sorted(tickers)
        n = min(quota[stratum], len(pool))
        for mt in (pool if len(pool) <= n else rng.sample(pool, n)):
            sample.append((mt, stratum))
    print(f"sampled {len(sample)} combos")

    cfg = PricingConfig()

    async def run() -> tuple[list[dict], int]:
        rows: list[dict] = []
        n_mismatch = 0
        for k, (mt, stratum) in enumerate(sorted(sample), 1):
            rec = inputs[mt]
            legs, sides = list(rec["legs"]), list(rec["sides"])
            marg_sel = list(rec["snaps"][-1][1])
            yes_cc = _quantized_yes_cc(marg_sel, sides)
            fair_cc_engine, disp, engine_yes = await _price_engine(cfg, legs, sides, yes_cc)
            # Harness marginals: the engine's own beliefs, selected space.
            h_marg = [
                (p if s == "yes" else 1.0 - p) if p is not None else None
                for p, s in zip(engine_yes, sides, strict=False)
            ]
            if any(m is None for m in h_marg):
                rows.append({"ticker": mt, "stratum": stratum, "match": False,
                             "why": "engine belief missing"})
                n_mismatch += 1
                continue
            is_mlb_cache = origin[mt] in ("mlb", "mixed")
            try:
                if is_mlb_cache:
                    h_fair, h_path = mlb_price(legs, sides, h_marg, "promoted")
                else:
                    h_fair = wc_price(legs, sides, h_marg)
                    h_path = "wc"
            except Exception as exc:  # noqa: BLE001
                h_fair, h_path = None, f"error: {exc}"
            if h_fair is None and fair_cc_engine is None:
                match = True   # both decline (reasons recorded)
                delta = 0
            elif h_fair is None and disp == "farmed":
                # harness encodes farmable-impossible as fair 0.0 upstream;
                # mlb harness returns (0.0, 'impossible-farmable')
                match = False
                delta = None
            elif h_fair is not None and fair_cc_engine is not None:
                h_cc = int(cc_from_prob(h_fair))
                delta = h_cc - fair_cc_engine
                match = delta == 0
            else:
                match, delta = False, None
            if not match:
                n_mismatch += 1
            rows.append({
                "ticker": mt, "stratum": stratum, "cache": origin[mt],
                "engine_fair_cc": fair_cc_engine, "engine_disp": disp,
                "harness_fair": h_fair, "harness_path": h_path,
                "delta_cc": delta, "match": match,
            })
            if k % 25 == 0:
                print(f"  {k}/{len(sample)} priced, mismatches={n_mismatch}", flush=True)
        return rows, n_mismatch

    rows, n_mismatch = asyncio.run(run())
    by_stratum: dict[str, dict[str, int]] = {}
    for r in rows:
        b = by_stratum.setdefault(r["stratum"], {"n": 0, "match": 0})
        b["n"] += 1
        b["match"] += 1 if r["match"] else 0
    print("\nPARITY RESULT:")
    for s, b in sorted(by_stratum.items()):
        print(f"  {s:24s} {b['match']}/{b['n']}")
    print(f"  TOTAL match {len(rows) - n_mismatch}/{len(rows)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"rows": rows, "by_stratum": by_stratum,
               "total": len(rows), "mismatches": n_mismatch},
              open(out_path, "w"), indent=1)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
