"""DNP scalar guard — era replay A/B proof (2026-08-06 build).

Replays the FULL settled live-era corpus (the 2026-08-06 exchange-truth pull:
992 settlements with legs, selected sides and leg mids at fill) through the
REAL ``PricingEngine.price`` twice — flag OFF vs flag ON — and proves:

  1. Every combo OUT of the guard's scope (anything not single-player-driven)
     prices BYTE-IDENTICALLY (yes/no bids, fair, width dict, or the same
     NoQuote reason).
  2. Every small-Δ same-player combo (the era's normal book) is IDENTICAL TO
     THE CENT.
  3. The 3 sniped Marte tickets floor their implied YES ask at the DNP
     settlement value (17/20/20¢ — the sniper pays >= the settle).
  4. A randomized-replay sweep (mid jitter re-drawn per seed over the same
     leg sets) holds (1) under perturbed inputs, not just the tape's exact
     numbers.

Rule 8: imports and drives the live engine/config only — edits nothing.

Usage (PYTHONPATH must carry BOTH <worktree>/src and <worktree> for the
tests fakes):
    python -m tools.backtests.dnp_scalar_replay <master.json>
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import DOC_ASSUMED
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import EventMeta, MarketMeta, MetadataCache
from combomaker.ops.config import DnpScalarConfig, PricingConfig, load_config
from combomaker.pricing.dnp_scalar import (
    DNP_SCALAR_FAMILIES,
    single_player_scope,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legtypes import classify_leg
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_feed import FakeWs, snapshot_env

NOW = datetime(2026, 8, 5, 18, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]

# The 3 sniped tickets (forensics §1) and their exchange scalar settles (¢).
SNIPED_SETTLES_C = {
    "KXMVECROSSCATEGORY-S202666D375DC4A8-E8BE6BD67FD": 20,
    "KXMVECROSSCATEGORY-S2026F97C96E80E0-97F5BF3F0AC": 20,
    "KXMVECROSSCATEGORY-S20262E7569D7618-A50F06E3000": 17,
}


def _pricing_config(enabled: bool) -> PricingConfig:
    """The SHIPPED prod pricing config (real correlation tables, sell-only,
    markup) with only the dnp_scalar flag toggled — the exact A/B the live
    arming line would flip."""
    cfg = load_config(REPO_ROOT / "config" / "prod.yaml").pricing
    return cfg.model_copy(update={"dnp_scalar": DnpScalarConfig(enabled=enabled)})


async def _seed(
    mids: dict[str, float], combo_ticker: str
) -> tuple[OrderbookFeed, MetadataCache, FakeClock]:
    clock = FakeClock(start=NOW)
    ws = FakeWs()
    feed = OrderbookFeed(ws, clock)
    metadata = MetadataCache(None, clock)  # type: ignore[arg-type]
    feed.watch(list(mids))
    await ws.ack_subscription(0, 5)
    for i, (ticker, mid) in enumerate(mids.items()):
        mid_cc = int(round(mid * 10_000))
        yes_bid = max(mid_cc - 50, 100)
        no_bid = max(10_000 - mid_cc - 50, 100)
        env = snapshot_env(5, i + 1, ticker)
        env["msg"]["yes_dollars_fp"] = [[f"{yes_bid / 10_000:.4f}", "500.00"]]
        env["msg"]["no_dollars_fp"] = [[f"{no_bid / 10_000:.4f}", "500.00"]]
        await ws.deliver(env)
    for ticker in mids:
        event = _event_of(ticker)
        metadata._events[event] = EventMeta(  # noqa: SLF001 (harness seam)
            event_ticker=event,
            mutually_exclusive=False,
            raw={},
            fetched_mono_ns=clock.monotonic_ns(),
        )
    metadata._markets[combo_ticker] = MarketMeta(  # noqa: SLF001 (harness seam)
        ticker=combo_ticker,
        status="active",
        grid=PriceGrid.from_market_payload(
            {
                "ticker": combo_ticker,
                "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
            }
        ),
        event_ticker="E",
        close_time=NOW + timedelta(hours=6),
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    return feed, metadata, clock


def _event_of(market_ticker: str) -> str:
    """Synthesized leg event ticker: the market ticker minus its outcome/rung
    segment (the Kalshi event convention for suffixed markets). Purely a
    replay-harness identity — identical across both A/B arms."""
    parts = market_ticker.split("-")
    return "-".join(parts[:-1]) if len(parts) >= 3 else market_ticker


def _rfq(row: dict) -> Rfq:
    return Rfq.from_ws(
        {
            "id": row.get("rfq_id") or "rfq_replay",
            "market_ticker": row["ticker"],
            "created_ts": "2026-08-05T17:00:00Z",
            "contracts_fp": f"{max(int(row.get('qty_centi') or 100), 1) / 100:.2f}",
            "mve_collection_ticker": row["ticker"].split("-", 1)[0],
            # legs on the tape carry their own per-leg sides ([ticker, side]
            # pairs); the row-level "sides" is the COMBO side we held.
            "mve_selected_legs": [
                {"market_ticker": mt, "side": side, "event_ticker": _event_of(mt)}
                for mt, side in row["legs"]
            ],
        }
    )


def _quote_key(result: ConstructedQuote | NoQuote) -> tuple:
    if isinstance(result, NoQuote):
        return ("noquote", result.reason.value, result.detail)
    return (
        "quote",
        int(result.yes_bid_cc),
        int(result.no_bid_cc),
        int(result.fair_cc),
        tuple(sorted(result.width_components_cc.items())),
        result.farmed,
    )


async def price_both(row: dict, mids: dict[str, float]) -> tuple:
    results = []
    for enabled in (False, True):
        feed, metadata, _ = await _seed(mids, row["ticker"])
        engine = PricingEngine(feed, metadata, DOC_ASSUMED, _pricing_config(enabled))
        results.append(
            engine.price(_rfq(row), time_to_close_s=3 * 3600.0, in_play=False)
        )
    return tuple(results)


def _row_mids(row: dict) -> dict[str, float] | None:
    """Leg mids at fill: the tape stores ``mids`` as ticker → CENTI-CENTS."""
    mids = row.get("mids") or {}
    legs = [mt for mt, _ in row["legs"]]
    if not mids or any(mt not in mids for mt in legs):
        return None
    out: dict[str, float] = {}
    for ticker in legs:
        m = float(mids[ticker])
        if m > 1.0:  # centi-cents on the tape → prob
            m = m / 10_000.0
        out[ticker] = min(max(m, 0.005), 0.995)
    return out


def _is_single_player_row(row: dict) -> bool:
    """Tape-side scope tag (ticker parse only — mirrors the guard's scope
    rule so the report buckets rows the same way the guard does)."""
    fams = [classify_leg(mt) for mt, _ in row["legs"]]
    if not all(f in DNP_SCALAR_FAMILIES for f in fams):
        return False
    ents = set()
    for mt, _ in row["legs"]:
        parts = mt.upper().split("-")
        if len(parts) == 4 and parts[2]:
            ents.add(parts[2])
        else:
            return True  # UNKNOWN entity ⇒ fail-closed in scope
    return len(ents) <= 1


async def main() -> None:
    master = json.loads(Path(sys.argv[1]).read_text())  # noqa: ASYNC240 — one-shot CLI load
    rows = [r for r in master if r.get("legs")]
    n_total = n_skipped = n_identical = n_quoted = 0
    in_scope_rows = []
    diffs = []
    for row in rows:
        mids = _row_mids(row)
        if mids is None:
            n_skipped += 1
            continue
        n_total += 1
        off, on = await price_both(row, mids)
        if isinstance(off, ConstructedQuote):
            n_quoted += 1
        same = _quote_key(off) == _quote_key(on)
        scoped = _is_single_player_row(row)
        if scoped:
            in_scope_rows.append((row, mids, off, on))
        if same:
            n_identical += 1
        elif not scoped:
            diffs.append((row["ticker"], _quote_key(off), _quote_key(on)))
    print(f"rows priced both ways: {n_total} (skipped, no aligned mids: {n_skipped})")
    print(f"rows producing a real quote (flag OFF arm): {n_quoted}")
    print(f"byte-identical: {n_identical}; differing: {n_total - n_identical}")
    print(f"OUT-OF-SCOPE rows that differ (MUST be 0): {len(diffs)}")
    for t, a, b in diffs[:10]:
        print("  DIFF", t, a, b)

    print(f"\nsame-player-driven rows on the tape: {len(in_scope_rows)}")
    n_scope_identical = 0
    for row, mids, off, on in in_scope_rows:
        same_cent = False
        if isinstance(off, NoQuote) and isinstance(on, NoQuote):
            same_cent = _quote_key(off) == _quote_key(on)
        elif isinstance(off, ConstructedQuote) and isinstance(on, ConstructedQuote):
            same_cent = (
                int(off.yes_bid_cc) // 100 == int(on.yes_bid_cc) // 100
                and int(off.no_bid_cc) // 100 == int(on.no_bid_cc) // 100
            )
        settle = SNIPED_SETTLES_C.get(row["ticker"])
        tag = "SNIPED" if settle is not None else "normal"
        ask_off = (
            100 - int(off.no_bid_cc) // 100
            if isinstance(off, ConstructedQuote)
            else None
        )
        ask_on = (
            100 - int(on.no_bid_cc) // 100
            if isinstance(on, ConstructedQuote)
            else None
        )
        # The guard's own scope computation for the printed Δ:
        feed, metadata, _ = await _seed(mids, row["ticker"])
        engine = PricingEngine(feed, metadata, DOC_ASSUMED, _pricing_config(False))
        pre = engine._price_prefix(_rfq(row))  # noqa: SLF001 (report seam)
        floor_c = None
        if hasattr(pre, "beliefs"):
            scope = single_player_scope(_rfq(row).legs, pre.beliefs, pre.sides)
            if scope is not None:
                floor_c = scope.floor_cc // 100
        print(
            f"  [{tag}] {row['ticker'][-14:]}: floor={floor_c}c "
            f"ask off={ask_off}c on={ask_on}c settle={settle} "
            f"cent-identical={same_cent}"
        )
        if settle is not None:
            assert ask_on is None or ask_on >= settle, (
                f"SNIPED ticket still under the settle: {ask_on} < {settle}"
            )
        elif same_cent:
            n_scope_identical += 1
    n_not_sniped = len(in_scope_rows) - sum(
        1 for r, *_ in in_scope_rows if r["ticker"] in SNIPED_SETTLES_C
    )
    print(
        f"non-sniped same-player rows cent-identical: {n_scope_identical}/{n_not_sniped}"
    )

    # Randomized replays: jitter every leg mid ±3 cents (seeded), re-price
    # both ways; every out-of-scope row must stay byte-identical.
    rng = random.Random(20260806)
    n_rand = n_rand_ok = 0
    sample = [r for r in rows if _row_mids(r) is not None]
    rng.shuffle(sample)
    for row in sample[:150]:
        mids = _row_mids(row)
        assert mids is not None
        jittered = {
            t: min(max(m + rng.uniform(-0.03, 0.03), 0.01), 0.99)
            for t, m in mids.items()
        }
        off, on = await price_both(row, jittered)
        if _is_single_player_row(row):
            continue
        n_rand += 1
        if _quote_key(off) == _quote_key(on):
            n_rand_ok += 1
        else:
            print("  RANDOM DIFF", row["ticker"])
    print(f"\nrandomized out-of-scope replays byte-identical: {n_rand_ok}/{n_rand}")


if __name__ == "__main__":
    asyncio.run(main())
