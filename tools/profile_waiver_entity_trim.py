"""PROFILE (rule 8) — waiver enumeration runtime vs the entity-set trim K.

The Problem-A waiver's exact scoreline enumeration timed out live (>1.8s) on
the ~200-entity resting-quote book (two observed timeouts 2026-07-17 night; the
87ms benchmark was a 20-quote book), declining +EV wins. This script sizes the
``risk.lastlook_waiver_topk_resting`` default/arming value BEFORE the port is
trusted: it rebuilds the LIVE book shape from the live tape (mode=ro reads
ONLY — never a write, connections closed fast per the WAL-starvation lesson)
and times ``state_worst_case_by_game`` on the full set vs
``trim_open_quotes_for_games`` at several K.

Inputs (all read-only):
  * ``decisions`` kind='quote_sent' (latest window): quote per RFQ with its
    ``no_bid_cc`` + ``leg_mids_cc`` map -> the resting-quote set + marginals;
  * ``rfqs.legs_json``: the leg sets (market/event/side);
  * ``fills``: the committed entities (the real filled combos);
  * the LIVE yaml's pricing aliases + structural constants when loadable
    (championship legs must group into their real game), else defaults.

Everything enumerates through the LIVE seams (state_worst_case_by_game /
trim_open_quotes_for_games) — no reimplementation.

Usage:  .venv/Scripts/python.exe tools/profile_waiver_entity_trim.py
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from combomaker.core.conventions import Side
from combomaker.sim.state_worst_case import (
    WorstCaseEntity,
    WorstCaseQuote,
    state_worst_case_by_game,
    trim_open_quotes_for_games,
)
from combomaker.sim.structural_book import StructuralConfigView

DB = Path("data/combomaker-prod-live-wc.sqlite3")
N_QUOTES = 200          # the live resting-slot count
N_COMMITTED = 12        # the live open-position count (latest fills as proxies)
K_SWEEP = (32, 24, 16, 12, 8, 6, 4)
REPS = 5                # min-of (the live bot shares this box — noisy CPU)


def _load_live_pricing() -> StructuralConfigView:
    """Install the LIVE pricing aliases + structural constants (best-effort:
    read-only config load; falls back to defaults so the profile always runs)."""
    try:
        from combomaker.ops.config import load_config
        from combomaker.pricing.legtypes import set_pricing_aliases

        cfg = load_config(
            Path("config/prod-live-wc.local.yaml"),
            env="prod",
            mode="quote",
            confirm_live=True,
        )
        set_pricing_aliases(dict(cfg.pricing.leg_pricing_aliases))
        sc = cfg.pricing.structural
        view = StructuralConfigView(
            dc_rho=sc.dc_rho,
            et_factor=sc.et_factor,
            pens_win_a=sc.pens_win_prob,
            half_share=sc.half_share,
            max_goals=sc.max_goals,
            knockout_series=tuple(sc.knockout_series),
            enabled=sc.enabled,
            corners_et_loading=sc.corners_et_loading,
        )
        print(
            f"live config loaded: {len(cfg.pricing.leg_pricing_aliases)} aliases, "
            f"max_goals={sc.max_goals}"
        )
        return view
    except Exception as exc:  # noqa: BLE001 - profile must run regardless
        print(f"live config unavailable ({exc!r}) — defaults, no aliases")
        return StructuralConfigView()


def _legs(legs_json: str) -> tuple[tuple[str, str | None, str], ...]:
    return tuple(
        (leg["market_ticker"], leg.get("event_ticker"), leg.get("side", "yes"))
        for leg in json.loads(legs_json)
    )


def _pull_live_shape() -> tuple[
    list[WorstCaseEntity], list[WorstCaseQuote], dict[str, float]
]:
    from combomaker.risk.exposure import LegRef

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # Resting quotes: the latest quote per RFQ (newest first) up to N.
        rows = con.execute(
            """
            SELECT d.rfq_id, d.context_json, r.legs_json, r.contracts_centi
            FROM decisions d JOIN rfqs r ON r.rfq_id = d.rfq_id
            WHERE d.kind = 'quote_sent'
            ORDER BY d.id DESC LIMIT ?
            """,
            (N_QUOTES * 3,),
        ).fetchall()
        fills = con.execute(
            """
            SELECT f.raw_json, f.our_side, f.contracts_centi, f.price_cc,
                   r.legs_json
            FROM fills f
            LEFT JOIN rfqs r
              ON r.rfq_id = json_extract(f.raw_json, '$.rfq_id')
            ORDER BY f.id DESC LIMIT ?
            """,
            (N_COMMITTED,),
        ).fetchall()
    finally:
        con.close()  # close FAST — a long-lived reader starves the live WAL

    marginals: dict[str, float] = {}
    quotes: list[WorstCaseQuote] = []
    seen_rfqs: set[str] = set()
    for row in rows:
        if len(quotes) >= N_QUOTES or row["rfq_id"] in seen_rfqs:
            continue
        seen_rfqs.add(row["rfq_id"])
        ctx = json.loads(row["context_json"])
        no_bid_cc = int(ctx.get("no_bid_cc", 0))
        if no_bid_cc <= 0 or not row["legs_json"]:
            continue
        for ticker, mid in (ctx.get("leg_mids_cc") or {}).items():
            marginals.setdefault(ticker, max(0.01, min(0.99, mid / 10_000.0)))
        legs = tuple(
            LegRef(m, e, s) for m, e, s in _legs(row["legs_json"])
        )
        contracts = int(row["contracts_centi"] or 100)
        quotes.append(
            WorstCaseQuote(
                quote_id=f"q:{row['rfq_id']}",
                hypotheticals=(
                    WorstCaseEntity(
                        entity_id=f"q:{row['rfq_id']}:no",
                        our_side=Side.NO,
                        contracts_centi=contracts,
                        entry_price_cc=no_bid_cc,
                        legs=legs,
                    ),
                ),
            )
        )

    entities: list[WorstCaseEntity] = []
    for i, row in enumerate(fills):
        if not row["legs_json"]:
            continue
        legs = tuple(LegRef(m, e, s) for m, e, s in _legs(row["legs_json"]))
        entities.append(
            WorstCaseEntity(
                entity_id=f"pos:{i}",
                our_side=Side.NO if row["our_side"] == "no" else Side.YES,
                contracts_centi=int(row["contracts_centi"]),
                entry_price_cc=int(row["price_cc"]),
                legs=legs,
            )
        )
    return entities, quotes, marginals


def main() -> None:
    cfg = _load_live_pricing()
    entities, quotes, marginals = _pull_live_shape()
    if not quotes:
        print("no quotes reconstructed — nothing to profile")
        return

    # Candidate = the most recent quoted combo (a live-shaped fill candidate).
    candidate = quotes[0].hypotheticals[0]
    entities = [*entities, candidate]

    def run(qs: list[WorstCaseQuote]) -> tuple[float, dict[str, int], int]:
        best = float("inf")
        result: dict[str, int] = {}
        for _ in range(REPS):
            t0 = time.perf_counter()
            out = state_worst_case_by_game(entities, qs, marginals, None, cfg)
            best = min(best, time.perf_counter() - t0)
            result = {
                g: r.worst_case_cc for g, r in out.items() if r.certified
            }
        n_games = len(out)
        return best, result, n_games

    # Breached games = the games the resting book actually loads (top by count).
    from collections import Counter

    from combomaker.pricing.grouping import game_key

    per_game = Counter()
    for q in quotes:
        gs = set()
        for h in q.hypotheticals:
            for leg in h.legs:
                if leg.event_ticker:
                    gs.add(game_key(leg.event_ticker))
        per_game.update(gs)
    games = [g for g, _n in per_game.most_common(2)]
    print(
        f"book shape: {len(entities)} entities, {len(quotes)} resting quotes, "
        f"{len(marginals)} marginals; breached games (top2): "
        f"{[(g, per_game[g]) for g in games]}"
    )

    t_full, certified_full, n_games = run(quotes)
    print(
        f"FULL set    : {t_full * 1e3:8.1f} ms   games={n_games} "
        f"certified={certified_full}"
    )
    for k in K_SWEEP:
        kept, adders = trim_open_quotes_for_games(quotes, games, None, k)
        t, certified, _n = run(list(kept))
        bounded = {
            g: certified.get(g, 0) + adders.get(g, 0) for g in games
        }
        print(
            f"K={k:<3d} kept={len(kept):<4d}: {t * 1e3:8.1f} ms   "
            f"adders={ {g: adders.get(g, 0) for g in games} }   "
            f"certified+adder={bounded}"
        )


if __name__ == "__main__":
    main()
