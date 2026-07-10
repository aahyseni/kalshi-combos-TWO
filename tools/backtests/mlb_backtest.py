"""Fast, ZERO-BIAS MLB combo backtest harness — the rule-8 validation gate for
the PROMOTED 32-entry pair_rho_by_sport["mlb"] table (canonical method; modeled
on tools/backtests/wc_backtest.py).

WHY THIS SHAPE — the pricer must NEVER see the maker's price. It's enforced
STRUCTURALLY, not by discipline:

  gather  ── combos/sides/marginals from the recorder DB (MLB-STRICT: every leg
              KXMLB*) + clearings straight from Kalshi's trade tape (get_trades,
              COMPLETE → poller-gap-immune) → TWO separate caches:
              inputs.pkl   = {ticker: {legs, sides, snapshots(at, marginals)}}   ← NO prices
              outcomes.pkl = {ticker: {clearings:[(price,count,side,t)], cutoff,
                                       settle}}                                   ← outcomes only
              Clearings are STRICTLY PRE-GAME: a print is kept only if it landed
              before the earliest leg's estimated first pitch (no in-play prints).
  price   ── reads inputs.pkl ONLY → fairs.pkl, via a thin driver that calls the
              SAME production pricing modules + shipped PricingConfig the live
              engine uses. DUAL-CONFIG: every combo is priced TWICE — (a) the
              SHIPPED promoted config, (b) a LEGACY override where the mlb pair
              table is reduced to the pre-promotion 4 entries (so every prop pair
              falls to the flat same-event 0.6 prior — "legacy-flat-0.6"). The
              override is a deep copy built HERE; config.py is untouched. Per
              same-game PAIR the rho + source-note (typed-sport / typed-global /
              untyped fallback) is recorded straight from build_sgp_correlation.
              No clearing argument; outcomes.pkl never opened.
  analyze ── joins fairs.pkl + outcomes.pkl → |err| / bias / within-2c for BOTH
              configs, split by family-composition bucket (game-lines-only /
              props-only / mixed) and by pair-source; the GATE VERDICT (promoted
              beats legacy on prop-carrying, no regression on game-lines-only)
              and the DO-9 exposure counters. First and only place the maker
              price and the settlement appear.

DB DISCIPLINE — the prod recorder is LIVE: read-only URI, busy_timeout ≤ 10s,
rowid-bounded LIMIT chunks (adaptive: a slow chunk halves the next chunk size),
progress persisted so an interrupted gather resumes instead of re-scanning.

Usage (from repo root):
  python -m tools.backtests.mlb_backtest gather --since 2026-07-05 [--pregame-hours 4.0]
  python -m tools.backtests.mlb_backtest price
  python -m tools.backtests.mlb_backtest analyze
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import random
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "src")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1252 console
except Exception:
    pass

DB = "file:data/combomaker-prod.sqlite3?mode=ro"          # READ-ONLY, never write
KALSHI = "https://external-api.kalshi.com/trade-api/v2"    # public market reads


def _mlb_strict(legs: list[dict]) -> bool:
    """A combo counts only if EVERY leg is an MLB market (KXMLB*). Excludes
    WC/UCL soccer, tennis, and any mixed combo — 'strictly MLB'."""
    return bool(legs) and all(lg["market_ticker"].startswith("KXMLB") for lg in legs)


# ───────────────────────── stage 1: GATHER ─────────────────────────
# STRICTLY-PREGAME cutoff. Kalshi does NOT expose first pitch; the one time
# anchor it gives is expected_expiration_time ≈ game-end/settlement. So we
# ESTIMATE first pitch = expected_expiration_time − PREGAME_HOURS. An MLB game
# runs ~3–3.5h plus a settlement buffer, so 4.0h lands at-or-before first pitch
# for 9-inning-settled markets; extras run LATER, making this conservative — the
# estimate is ≤ the true first pitch, so the filter only ever DROPS borderline
# pre-game prints, never ADMITS an in-play one. THIS IS AN ESTIMATE, tunable via
# --pregame-hours. (MLB game codes embed a local start token, e.g.
# 26JUL071835MILSTLG1 — a future refinement could parse it, but its timezone
# convention is unverified, so the expiry anchor stays canonical here.)
DEFAULT_PREGAME_HOURS = 4.0


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _first_rowid_at(con: sqlite3.Connection, since: str) -> int:
    """First rfqs rowid with seen_at >= since, via BINARY SEARCH over indexed
    rowid probes (seen_at has no index; a MIN() scan over the 44 GB table is
    exactly the >30s query this tool must never run)."""
    hi = con.execute("SELECT MAX(rowid) FROM rfqs").fetchone()[0] or 0
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        row = con.execute(
            "SELECT seen_at FROM rfqs WHERE rowid >= ? ORDER BY rowid LIMIT 1", (mid,)
        ).fetchone()
        if row is None or row[0] >= since:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _scan_db(
    outdir: Path, since: str, chunk_rows: int, progress_path: Path,
) -> tuple[set[str], dict[str, list[dict]], dict, int]:
    """(a) rfqs → {market_ticker: legs}, MLB-strict only, via chunked
    rowid-bounded LIMIT reads ('KXMLB' LIKE pre-filter runs in SQLite — a cheap
    SUPERSET; Python enforces the STRICT every-leg filter; progress persists so
    an interrupted scan resumes). (b) would_quotes → {ticker: [(at, marginals)]}
    via the rfq_id INDEX (batched IN-lists — no full scan of the 7M-row table)."""
    con = sqlite3.connect(DB, uri=True, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")  # ≤10s: never camp on the recorder's lock

    ticker_legs: dict[str, list[dict]] = {}
    rfq_ticker: dict[str, str] = {}
    n_scanned = 0
    if progress_path.exists():
        st = pickle.load(open(progress_path, "rb"))
        lo, ticker_legs, rfq_ticker, n_scanned = (
            st["next_rowid"], st["ticker_legs"], st["rfq_ticker"], st["n_scanned"])
        print(f"RESUME gather at rowid {lo} ({len(ticker_legs)} combos so far)", flush=True)
    else:
        lo = _first_rowid_at(con, since)
        print(f"scan rfqs from rowid {lo} (seen_at >= {since}), MLB-strict…", flush=True)
    max_rowid = con.execute("SELECT MAX(rowid) FROM rfqs").fetchone()[0] or 0
    while lo <= max_rowid:
        hi_bound = lo + chunk_rows - 1
        t0 = time.time()
        rows = con.execute(
            "SELECT rowid, market_ticker, rfq_id, legs_json FROM rfqs"
            " WHERE rowid >= ? AND rowid <= ? AND seen_at >= ?"
            "   AND legs_json LIKE '%KXMLB%' LIMIT ?",
            (lo, hi_bound, since, chunk_rows),
        ).fetchall()
        dt = time.time() - t0
        n_scanned += min(chunk_rows, max_rowid - lo + 1)
        for _rid, mt, rfq_id, legs_json in rows:
            if not legs_json:
                continue
            legs = [{"market_ticker": lg["market_ticker"], "side": lg.get("side", "yes")}
                    for lg in json.loads(legs_json)]
            if not _mlb_strict(legs):
                continue
            ticker_legs.setdefault(mt, legs)
            rfq_ticker[rfq_id] = mt
        lo = hi_bound + 1
        if dt > 20:  # hot DB: halve the next chunk instead of risking a >30s query
            chunk_rows = max(25_000, chunk_rows // 2)
            print(f"  slow chunk ({dt:.0f}s) → chunk_rows={chunk_rows}", flush=True)
        pickle.dump({"next_rowid": lo, "ticker_legs": ticker_legs,
                     "rfq_ticker": rfq_ticker, "n_scanned": n_scanned},
                    open(progress_path, "wb"))
        print(f"  rowid {lo - 1}/{max_rowid} · {len(ticker_legs)} MLB combos "
              f"({dt:.1f}s/chunk)", flush=True)
    mlb = set(ticker_legs)
    print(f"scanned ~{n_scanned} rfq rows → {len(mlb)} distinct MLB-strict combos", flush=True)

    print("fetch would_quotes marginal snapshots (indexed rfq_id, batched)…", flush=True)
    ticker_snaps: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
    rfq_ids = sorted(rfq_ticker)
    for b in range(0, len(rfq_ids), 500):
        batch = rfq_ids[b:b + 500]
        qmarks = ",".join("?" * len(batch))
        for rfq_id, at, probs_json in con.execute(
            f"SELECT rfq_id, at, leg_probs_json FROM would_quotes"
            f" WHERE rfq_id IN ({qmarks})", batch,
        ):
            if probs_json:
                ticker_snaps[rfq_ticker[rfq_id]].append((at, json.loads(probs_json)))
        if (b // 500) % 20 == 0:
            print(f"  would_quotes batch {b}/{len(rfq_ids)}", flush=True)
    for mt in ticker_snaps:
        ticker_snaps[mt].sort(key=lambda x: x[0])
    con.close()  # DB done for this stage (combo_trades is read separately, bounded)
    return mlb, ticker_legs, ticker_snaps, n_scanned


def gather(outdir: Path, since: str, pregame_hours: float, chunk_rows: int,
           audit_sample: int, refetch_outcomes: bool) -> None:
    """One pass → inputs.pkl (pricing inputs) + outcomes.pkl (clearings +
    settlement + cutoff) + gather_meta.json (counters), inputs and outcomes in
    SEPARATE files so the price stage can't reach outcomes. Combos, sides and
    marginal snapshots come from the recorder DB (rfqs + would_quotes); the
    CLEARINGS come straight from Kalshi's trade tape (get_trades) — COMPLETE and
    GAP-FREE by construction (poller-gap-immune) and never touch the
    write-locked combo_trades table. Every clearing is then STRICTLY-PREGAME
    filtered: a combo's print is kept only if it landed before the estimated
    first pitch of its EARLIEST-starting leg (so no leg was in-play), and a
    combo with zero pre-game prints is dropped from the error rows (counted as
    in-play-only for DO-9)."""
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.ops.dotenv import load_dotenv

    load_dotenv()  # KALSHI_PROD_* for the authed trade tape (get_trades needs auth)

    outdir.mkdir(parents=True, exist_ok=True)
    progress_path = outdir / "gather_progress.pkl"

    if refetch_outcomes and (outdir / "inputs.pkl").exists():
        # OUTCOME-ONLY refetch (e.g. a larger audit sample): reuse the already
        # scanned inputs.pkl — combos, sides and snapshots are identical; only
        # the tape/settlement side is redone. No rfqs/would_quotes re-scan.
        prev = pickle.load(open(outdir / "inputs.pkl", "rb"))
        ticker_legs = {mt: [{"market_ticker": t, "side": s}
                            for t, s in zip(d["legs"], d["sides"], strict=False)]
                       for mt, d in prev.items()}
        ticker_snaps = defaultdict(list, {mt: d["snaps"] for mt, d in prev.items()})
        n_scanned = -1
        print(f"REFETCH-OUTCOMES: reusing inputs.pkl ({len(ticker_legs)} combos)", flush=True)
        mlb = set(ticker_legs)
    else:
        mlb, ticker_legs, ticker_snaps, n_scanned = _scan_db(
            outdir, since, chunk_rows, progress_path)

    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())

    # Tape + settlement fetches are RESTRICTED to combos that have at least one
    # would_quote snapshot: only those are priceable, so only those can ever
    # produce an error row (MLB has ~600k+ distinct RFQ'd combos — hammering
    # get_trades for the unpriceable ones is pure rate-limit exposure).
    priceable = sorted(mt for mt in mlb if ticker_snaps.get(mt))
    print(f"{len(priceable)}/{len(mlb)} combos have marginal snapshots (priceable)",
          flush=True)

    # CANDIDATE INDEX — >97% of priceable combos never traded (the whole
    # exchange has ~10k distinct traded combos), so 182k get_trades calls would
    # be hours of rate-limit exposure for empty responses. The recorder's SMALL
    # combo_trades table (one bounded DISTINCT read) picks WHICH combos to ask
    # the tape about; every clearing PRICE still comes from the Kalshi tape,
    # per combo, cursor-paginated (COMPLETE per combo). Poller-gap risk is
    # AUDITED, not assumed away: a fixed-seed random sample of 'untraded'
    # priceable combos is also tape-fetched — any print found there is a poller
    # miss and is both reported and included in the results.
    con2 = sqlite3.connect(DB, uri=True, timeout=10)
    con2.execute("PRAGMA busy_timeout=10000")
    traded = {r[0] for r in con2.execute("SELECT DISTINCT ticker FROM combo_trades")}
    con2.close()
    candidates = [mt for mt in priceable if mt in traded]
    untraded = [mt for mt in priceable if mt not in traded]
    audit = random.Random(20260709).sample(untraded, min(audit_sample, len(untraded)))
    fetch_set = candidates + audit
    print(f"candidate index: {len(candidates)} priceable combos have combo_trades "
          f"rows; tape-fetching those + {len(audit)} audit samples of the "
          f"{len(untraded)} 'untraded'", flush=True)

    # (c) CLEARINGS from Kalshi's trade tape — an OUTCOME. Sourced from the tape
    #     (NOT the DB) so each fetched combo's print history is complete.
    print(f"fetch clearings from Kalshi tape for {len(fetch_set)} combos…", flush=True)
    clearings = asyncio.run(_fetch_clearings(fetch_set, signer))
    audit_hits = [t for t in audit if clearings.get(t)]
    print(f"AUDIT: {len(audit_hits)}/{len(audit)} 'untraded' combos had tape prints "
          f"(poller misses — 0 expected)", flush=True)

    # (d) per-leg settlement (status/result) + expected_expiration_time — OUTCOME.
    #     Only combos with >=1 tape print need a cutoff / settlement.
    printed = [mt for mt in fetch_set if clearings.get(mt)]
    distinct_legs = sorted({lg["market_ticker"]
                            for mt in printed for lg in ticker_legs[mt]})
    print(f"fetch settlement + expiry for {len(distinct_legs)} legs…", flush=True)
    leg_meta = asyncio.run(_fetch_leg_meta(distinct_legs))

    def leg_won(mt: str, side: str) -> bool | None:
        s = leg_meta.get(mt)
        if not s or s["status"] not in ("finalized", "settled") or s["result"] not in ("yes", "no"):
            return None
        return s["result"] == side

    off = timedelta(hours=pregame_hours)
    outcomes: dict[str, dict] = {}
    n_pre = n_inplay_only = n_no_prints = 0
    for mt in priceable:
        legs = ticker_legs[mt]
        # cutoff = EARLIEST estimated first pitch across the legs (no leg live).
        starts = [_parse_ts(leg_meta[lg["market_ticker"]]["exp"]) - off
                  for lg in legs
                  if leg_meta.get(lg["market_ticker"], {}).get("exp")]
        cutoff = min(starts) if starts else None
        allc = clearings.get(mt, [])
        pre = [c for c in allc if cutoff and _parse_ts(c[3]) < cutoff]
        if pre:
            n_pre += 1
        elif allc:
            n_inplay_only += 1  # had prints, but every one was in-play/post-start
        else:
            n_no_prints += 1
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
    pickle.dump(inputs, open(outdir / "inputs.pkl", "wb"))
    pickle.dump(outcomes, open(outdir / "outcomes.pkl", "wb"))
    nres = sum(1 for o in outcomes.values() if o["resolved"])
    meta = {"since": since, "pregame_hours": pregame_hours, "n_rfq_rows_scanned": n_scanned,
            "n_combos": len(inputs), "n_priceable": len(priceable), "n_pregame": n_pre,
            "n_inplay_only": n_inplay_only, "n_no_prints": n_no_prints, "n_resolved": nres,
            "n_candidates": len(candidates), "n_audit": len(audit),
            "n_audit_hits": len(audit_hits), "audit_hit_tickers": audit_hits[:50]}
    json.dump(meta, open(outdir / "gather_meta.json", "w"), indent=1)
    # Ticker LIST of combos with >=1 STRICTLY-PREGAME print — NO prices. Lets
    # the price stage skip the expensive (copula ~0.3s) fair computation for
    # combos that can never join an error row (fairs are a pure per-combo
    # function of inputs, so this restriction cannot bias any fair; pair /
    # counter records still cover ALL priceable combos).
    json.dump(sorted(mt for mt in priceable if outcomes[mt]["clearings"]),
              open(outdir / "printed_tickers.json", "w"))
    progress_path.unlink(missing_ok=True)
    print(f"WROTE inputs.pkl ({len(inputs)} combos) + outcomes.pkl "
          f"({nres} resolved · {n_pre} with pre-game prints · "
          f"{n_inplay_only} in-play/post-start only · {n_no_prints} never traded · "
          f"cutoff = expiry − {pregame_hours}h).  inputs.pkl has NO prices.", flush=True)


async def _fetch_clearings(
    combos: list[str], signer: object,
) -> dict[str, list[tuple[float, float, str, str]]]:
    """Every combo's cleared trades from Kalshi's tape — COMPLETE, which is what
    makes this poller-gap-immune. Each trade →
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
                    resp = None
                    for attempt in range(5):  # retry/backoff: a 429 must not
                        try:                   # silently DROP a combo's tape
                            resp = await rest.get_trades(**params)
                            break
                        except Exception:
                            await asyncio.sleep(0.5 * 2 ** attempt)
                    if resp is None:
                        print(f"  WARN clearings incomplete for {tk}", flush=True)
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
    game-end/settlement anchor from which first pitch is estimated). Public."""
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
# The LEGACY override: pair_rho_by_sport["mlb"] reduced to the pre-promotion 4
# entries (+ the matching 4 bands). Under it every promoted prop pair falls to
# the flat same-event 0.6 prior — the exact pre-promotion behavior (props then
# typed UNKNOWN → the same flat 0.6). Built by deep-copying the shipped config
# IN THIS DRIVER; config.py and the live modules stay pristine (rule 8).
LEGACY_MLB_RHO = {
    "moneyline|total": -0.05,
    "extras|total": 0.10,
    "extras|moneyline": -0.04,
    "moneyline|moneyline": -0.95,
}
LEGACY_MLB_BANDS = {
    "mlb:moneyline|total": 0.06,
    "mlb:extras|total": 0.10,
    "mlb:extras|moneyline": 0.08,
    "mlb:moneyline|moneyline": 0.04,
}

_GAME_LINE = {"moneyline", "total", "spread", "extras"}
_PROP = {"player_hr", "player_hit", "player_ks", "player_tb", "player_hrr", "rfi"}
_PLAYER_PROP = _PROP - {"rfi"}
_NUM_SUFFIX = re.compile(r"^\d+(?:\.5)?$")


def _bucket(fam_names: set[str]) -> str:
    if not fam_names <= (_GAME_LINE | _PROP):
        return "unknown_carrying"
    has_prop = bool(fam_names & _PROP)
    has_game = bool(fam_names & _GAME_LINE)
    if has_prop and has_game:
        return "mixed"
    if has_prop:
        return "props_only"
    return "game_lines_only"


def _game_code(ticker: str) -> str:
    parts = ticker.split("-")
    return parts[1] if len(parts) > 1 else ticker


def _entity_token(ticker: str) -> str | None:
    """The player/team token of a prop or spread leg: everything after the game
    code, with a trailing numeric line token stripped (KXMLBHR-…-JUDGE-2 →
    JUDGE; KXMLBSPREAD-…-BOS4 → BOS). None when there is no suffix (RFI)."""
    parts = ticker.upper().split("-")
    toks = parts[2:]
    if toks and _NUM_SUFFIX.match(toks[-1]):
        toks = toks[:-1]  # a lone numeric token (a total line) leaves NO entity
    if toks and len(toks) == 1:
        m = re.fullmatch(r"([A-Z]+?)(\d+)", toks[-1])  # fused TEAM+line (BOS4)
        if m:
            toks = [m.group(1)]
    return "-".join(toks) or None


def _build_pricer():
    """Prices a combo the way the live engine does, by importing the SAME
    production pricing modules — classify_legs, StructuralPricer (Dixon-Coles /
    margin-total / mlb-runs NegBin grid), build_sgp_correlation +
    price_joint_matrices, price_containment — with the SHIPPED PricingConfig().
    It is OUR real correlation code and config: not agents, and NOT a modified
    engine (engine.py is untouched). The short dispatch below MIRRORS
    PricingEngine.price()'s fair-value block — keep the two in sync (and keep it
    structured IDENTICALLY to wc_backtest._build_pricer's dispatch, so a change
    there — e.g. the nested-band branch — lands in both the same way).
    price_combo has NO clearing argument."""
    from combomaker.ops.config import PricingConfig
    from combomaker.pricing.joint import price_containment, price_joint_matrices
    from combomaker.pricing.legs import LegBelief
    from combomaker.pricing.legtypes import classify_leg, pair_key
    from combomaker.pricing.relationships import RelationshipKind, classify_legs
    from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
    from combomaker.pricing.structural import StructuralPricer, structural_applicable
    from combomaker.rfq.models import RfqLeg

    def sgp_from(cfg) -> SgpParams:  # noqa: ANN001
        co = cfg.correlation
        return SgpParams(pair_rho=dict(co.pair_rho), default_rho=co.same_event_rho,
            cross_event_rho=co.cross_event_rho, typed_uncertainty=co.typed_rho_uncertainty,
            untyped_uncertainty=co.untyped_rho_uncertainty,
            pair_uncertainty=dict(co.pair_rho_uncertainty),
            pair_rho_by_sport={s: dict(t) for s, t in co.pair_rho_by_sport.items()},
            oriented_curve={k: list(v) for k, v in co.oriented_curve.items()},
            oriented_curve_uncertainty=dict(co.oriented_curve_uncertainty))

    cfg = PricingConfig()                      # (a) SHIPPED promoted config
    cfg_legacy = cfg.model_copy(deep=True)     # (b) LEGACY flat-0.6 override (driver-local)
    mlb_table = cfg_legacy.correlation.pair_rho_by_sport["mlb"]
    mlb_table.clear()
    mlb_table.update(LEGACY_MLB_RHO)
    bands = cfg_legacy.correlation.pair_rho_uncertainty
    for k in [k for k in bands if k.startswith("mlb:")]:
        del bands[k]
    bands.update(LEGACY_MLB_BANDS)
    sgp_by_config = {"promoted": sgp_from(cfg), "legacy": sgp_from(cfg_legacy)}
    pricer = StructuralPricer(cfg.structural, cfg.margin_total, cfg.mlb_runs)

    class _StubMeta:  # classify_legs only asks about mutual exclusion; traded MLB combos have none
        def event_mutually_exclusive(self, e):  # noqa: ANN001, ANN201
            return False

    pair_memo: dict[tuple, dict] = {}

    def _legs_of(leg_tickers, sides):  # noqa: ANN001, ANN202
        return [RfqLeg(market_ticker=t, event_ticker="-".join(t.split("-")[:2]),
                       side=s, yes_settlement_value_cc=None)
                for t, s in zip(leg_tickers, sides, strict=False)]

    def price_combo(leg_tickers, sides, marginals, which):  # noqa: ANN001, ANN202
        """marginals = P(SELECTED side) per leg, recorded at RFQ time; blind to
        any price. Returns (fair, path) — path records containment / structural
        / copula(+structural-decline reason) for the pair-source split."""
        sgp = sgp_by_config[which]
        legs = _legs_of(leg_tickers, sides)
        yes = [p if s == "yes" else 1.0 - p for p, s in zip(marginals, sides, strict=False)]
        rel = classify_legs(legs, _StubMeta())  # type: ignore[arg-type]
        if rel.kind is RelationshipKind.IMPOSSIBLE:
            return (0.0, "impossible-farmable") if rel.farmable else (None, "impossible")
        if rel.kind is RelationshipKind.UNKNOWN:
            return None, "unknown"
        beliefs = [LegBelief(p=y, uncertainty=0.005, source="bt") for y in yes]
        if rel.kind is RelationshipKind.CONTAINMENT and rel.containment is not None:
            return price_containment(beliefs, sides, rel.containment).p, "containment"
        path = "copula"
        if structural_applicable(list(legs), rel.same_event_groups):
            j, reason = pricer.try_price(list(legs), beliefs, sides)
            if j is not None:
                return j.p, "structural"
            path = f"copula(structural-declined: {reason})"
        corr = build_sgp_correlation(list(legs), rel.same_event_groups, sgp, marginals=yes)
        return (price_joint_matrices(beliefs, sides, corr.corr,
                                     corr.corr_low, corr.corr_high).p, path)

    def pair_records(leg_tickers, sides, marginals):  # noqa: ANN001, ANN202
        """Per same-game PAIR: the rho + source-note under BOTH configs, read
        straight off the LIVE build_sgp_correlation (called on the pair alone —
        prior resolution is per-pair-independent, so this equals the full-matrix
        entry). Categories: typed-sport (mlb:* table hit), typed-global (silent
        global-table hit), flat-no-prior / untyped-unknown-leg (the 0.6
        fallbacks). Also counts cross-game pairs. Inputs only — NO prices."""
        legs = _legs_of(leg_tickers, sides)
        yes = [p if s == "yes" else 1.0 - p for p, s in zip(marginals, sides, strict=False)]
        rel = classify_legs(legs, _StubMeta())  # type: ignore[arg-type]
        if rel.kind is not RelationshipKind.OK:
            return [], 0
        in_group: dict[int, int] = {}
        for gi, g in enumerate(rel.same_event_groups):
            for i in g:
                in_group[i] = gi
        types = [classify_leg(t) for t in leg_tickers]
        pairs: list[dict] = []
        n_cross = 0
        for i in range(len(legs)):
            for j in range(i + 1, len(legs)):
                same = i in in_group and j in in_group and in_group[i] == in_group[j]
                if not same:
                    n_cross += 1
                    continue
                # Memoized: 600k combos are built from ~1k markets, so distinct
                # pairs are few. Marginals join the key (rounded) because the
                # oriented moneyline branches consult them.
                mkey = (leg_tickers[i], leg_tickers[j], sides[i], sides[j],
                        round(yes[i], 3), round(yes[j], 3))
                if mkey in pair_memo:
                    pairs.append({**pair_memo[mkey], "i": i, "j": j})
                    continue
                rec: dict = {"i": i, "j": j, "key": pair_key(types[i], types[j])}
                for which, sgp in sgp_by_config.items():
                    sub = build_sgp_correlation(
                        [legs[i], legs[j]], [(0, 1)], sgp, marginals=[yes[i], yes[j]])
                    note = sub.notes[0] if sub.notes else ""
                    if note.startswith("untyped pair"):
                        cat = "untyped-unknown-leg"
                    elif note.startswith("no prior for pair"):
                        cat = "flat-no-prior"
                    elif note.startswith("pair "):
                        cat = "typed-sport"
                    else:
                        cat = "typed-global"
                    rec[f"rho_{which}"] = float(sub.corr[0, 1])
                    rec[f"src_{which}"] = note or f"silent global hit {rec['key']}"
                    rec[f"cat_{which}"] = cat
                pair_memo[mkey] = rec
                pairs.append(rec)
        return pairs, n_cross

    return price_combo, pair_records, classify_leg


_WORKER_PRICER = None


def _price_worker(chunk: list[tuple]) -> list[tuple]:
    """Fair computation for one chunk of combos, BOTH configs, in a worker
    process (the copula band pricing is ~0.3s/combo — CPU-bound, so the fair
    pass is parallelized; every worker builds the SAME live pricer)."""
    global _WORKER_PRICER
    if _WORKER_PRICER is None:
        _WORKER_PRICER = _build_pricer()[0]
    out = []
    for mt, legs, sides, marginals in chunk:
        row: list = [mt]
        for which in ("promoted", "legacy"):
            try:
                f, p = _WORKER_PRICER(legs, sides, marginals, which)
            except Exception as exc:
                f, p = None, f"error: {exc}"
            row += [f, p]
        out.append(tuple(row))
    return out


def price(outdir: Path, workers: int) -> None:
    from concurrent.futures import ProcessPoolExecutor

    inputs = pickle.load(open(outdir / "inputs.pkl", "rb"))   # ← ONLY inputs. Never outcomes.
    printed: set[str] | None = None
    if (outdir / "printed_tickers.json").exists():
        # Prices-free ticker list from gather: fairs are only COMPUTED for
        # combos with a strictly-pregame print (a pure runtime cut — a fair is
        # a per-combo function of inputs, so skipping others biases nothing);
        # pair/counter records below still cover EVERY priceable combo.
        printed = set(json.load(open(outdir / "printed_tickers.json")))
        print(f"fair computation limited to {len(printed)} pregame-printed combos",
              flush=True)
    _, pair_records, classify_leg = _build_pricer()
    fairs: dict[str, dict] = {}
    worklist: list[tuple] = []
    done = 0
    for mt, d in inputs.items():
        done += 1
        if done % 100_000 == 0:
            print(f"  records {done}/{len(inputs)}", flush=True)
        snaps = d["snaps"]
        if not snaps:
            continue
        marginals = snaps[-1][1]  # latest recorded snapshot
        if len(marginals) != len(d["legs"]):
            continue
        rec: dict = {"n_legs": len(d["legs"]),
                     "fair_promoted": None, "path_promoted": "skipped-unprinted",
                     "fair_legacy": None, "path_legacy": "skipped-unprinted"}
        if printed is None or mt in printed:
            worklist.append((mt, d["legs"], d["sides"], marginals))
        fam_names = {classify_leg(t).name.lower() for t in d["legs"]}
        rec["fams"] = "+".join(sorted(fam_names))
        rec["bucket"] = _bucket(fam_names)
        pairs, n_cross = pair_records(d["legs"], d["sides"], marginals)
        rec["pairs"], rec["n_cross_event_pairs"] = pairs, n_cross
        # DO-9 ticker-shape counters (inputs-derived; no prices involved).
        types = [classify_leg(t).name.lower() for t in d["legs"]]
        same_player_xfam = nested_band = 0
        for a in range(len(d["legs"])):
            for b in range(a + 1, len(d["legs"])):
                ta, tb = types[a], types[b]
                ka, kb = d["legs"][a], d["legs"][b]
                if _game_code(ka) != _game_code(kb):
                    continue
                ea, eb = _entity_token(ka), _entity_token(kb)
                if (ta in _PLAYER_PROP and tb in _PLAYER_PROP and ta != tb
                        and ea is not None and ea == eb):
                    same_player_xfam += 1
                nested = (
                    (ta == tb == "total" and ka != kb)
                    or (ta == tb == "spread" and ka != kb and ea is not None and ea == eb)
                    or (ta == tb and ta in _PLAYER_PROP and ka != kb
                        and ea is not None and ea == eb)
                )
                if nested:
                    nested_band += 1
        rec["same_player_cross_family_pairs"] = same_player_xfam
        rec["nested_band_pairs"] = nested_band
        fairs[mt] = rec

    print(f"record pass done ({len(fairs)} priceable) — fair pass on "
          f"{len(worklist)} combos across {workers} workers…", flush=True)
    chunks = [worklist[i:i + 100] for i in range(0, len(worklist), 100)]
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for batch in ex.map(_price_worker, chunks):
            for mt, fp, pp, fl, pl in batch:
                fairs[mt].update(fair_promoted=fp, path_promoted=pp,
                                 fair_legacy=fl, path_legacy=pl)
            done += len(batch)
            if done % 1000 < 100:
                print(f"  fairs {done}/{len(worklist)}", flush=True)
    pickle.dump(fairs, open(outdir / "fairs.pkl", "wb"))
    ok = sum(1 for f in fairs.values() if f["fair_promoted"] is not None)
    print(f"WROTE fairs.pkl — priced {ok}/{len(worklist)} under BOTH configs via the "
          "live pricing code (engine.py untouched; blind to any maker price)", flush=True)


# ───────────────────────── stage 3: ANALYZE ─────────────────────────
def _stats(errs: list[float]) -> str:
    ae = [abs(x) for x in errs]
    w2 = sum(x <= 2 for x in ae) / len(ae) * 100
    return (f"median|err| {statistics.median(ae):5.2f}c  mean|err| {statistics.mean(ae):5.2f}c  "
            f"bias {statistics.mean(errs):+5.2f}c  within2c {w2:3.0f}%")


def analyze(outdir: Path) -> None:
    fairs = pickle.load(open(outdir / "fairs.pkl", "rb"))
    outcomes = pickle.load(open(outdir / "outcomes.pkl", "rb"))  # ← outcomes enter HERE, not before
    meta = {}
    if (outdir / "gather_meta.json").exists():
        meta = json.load(open(outdir / "gather_meta.json"))
    rows = []
    for mt, f in fairs.items():
        if f["fair_promoted"] is None or f["fair_legacy"] is None:
            continue
        o = outcomes.get(mt)
        if not o or not o["clearings"]:  # only combos with a STRICTLY PRE-GAME print
            continue
        clr = statistics.median(c[0] for c in o["clearings"])  # tape price is dollars
        rows.append({"ticker": mt, "fair_promoted": f["fair_promoted"],
                     "fair_legacy": f["fair_legacy"], "clearing": clr,
                     "err_promoted": f["fair_promoted"] - clr,
                     "err_legacy": f["fair_legacy"] - clr,
                     "bucket": f["bucket"], "fams": f["fams"], "n_legs": f["n_legs"],
                     "path_promoted": f["path_promoted"], "pairs": f["pairs"],
                     "resolved": o["resolved"], "settle_yes": o["settle_yes"],
                     "n_tr": len(o["clearings"])})
    if not rows:
        print("no rows — run gather + price first (and ensure combos have clearings).")
        return

    def by(pred):  # noqa: ANN001, ANN202
        return [r for r in rows if pred(r)]

    def report(label: str, sub: list[dict]) -> None:
        if not sub:
            print(f"  {label:24s} n=0")
            return
        ep = [r["err_promoted"] * 100 for r in sub]
        el = [r["err_legacy"] * 100 for r in sub]
        print(f"  {label:24s} n={len(sub):4d}")
        print(f"    promoted : {_stats(ep)}")
        print(f"    legacy   : {_stats(el)}")

    print(f"\n=== MLB BACKTEST — fair vs maker CLEARING, promoted vs legacy-flat-0.6 "
          f"(n={len(rows)} combos w/ pre-game prints) ===")
    report("ALL", rows)
    print("\n=== by family-composition bucket ===")
    for b in ("game_lines_only", "props_only", "mixed", "unknown_carrying"):
        report(b, by(lambda r, b=b: r["bucket"] == b))
    prop_carrying = by(lambda r: r["bucket"] in ("props_only", "mixed"))
    report("prop_carrying (po+mx)", prop_carrying)

    print("\n=== by pair-source (promoted config; combo counted once per source it carries) ===")
    src_groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        cats = {p["cat_promoted"] for p in r["pairs"]}
        if r["path_promoted"] == "structural":
            src_groups["structural-priced"].append(r)
        elif r["path_promoted"] == "containment":
            src_groups["containment-priced"].append(r)
        elif r["path_promoted"].startswith("copula(structural-declined"):
            src_groups["structural-DECLINED->copula"].append(r)
        for c in cats:
            src_groups[f"pair:{c}"].append(r)
        if not r["pairs"]:
            src_groups["no-same-game-pairs"].append(r)
    for label in sorted(src_groups):
        report(label, src_groups[label])

    # ---- THE GATE VERDICT -------------------------------------------------
    print("\n=== GATE VERDICT (rule-8 promoted-table validation) ===")
    verdicts = []
    for label, sub in (("prop-carrying", prop_carrying),
                       ("game-lines-only", by(lambda r: r["bucket"] == "game_lines_only"))):
        if not sub:
            print(f"  {label}: n=0 — NO EVIDENCE")
            verdicts.append(None)
            continue
        mp = statistics.median(abs(r["err_promoted"]) * 100 for r in sub)
        ml = statistics.median(abs(r["err_legacy"]) * 100 for r in sub)
        verdicts.append((mp, ml, len(sub)))
        print(f"  {label}: promoted median|err| {mp:.2f}c vs legacy {ml:.2f}c (n={len(sub)})")
    if verdicts[0] and verdicts[1]:
        beats = verdicts[0][0] < verdicts[0][1]
        no_reg = verdicts[1][0] <= verdicts[1][1] + 0.05
        print(f"  → promoted beats legacy on prop-carrying: {beats} "
              f"({verdicts[0][0]:.2f}c vs {verdicts[0][1]:.2f}c, n={verdicts[0][2]})")
        print(f"  → no regression on game-lines-only:       {no_reg} "
              f"({verdicts[1][0]:.2f}c vs {verdicts[1][1]:.2f}c, n={verdicts[1][2]})")
        print(f"  GATE: {'PASS' if beats and no_reg else 'FAIL'}")
    else:
        print("  GATE: INCONCLUSIVE (a bucket has no rows)")

    # ---- DO-9 EXPOSURE COUNTERS (over ALL priceable combos w/ pair records) --
    print("\n=== DO-9 COUNTERS (universe = all priceable combos; cleared subset in parens) ===")
    cleared = {r["ticker"] for r in rows}
    allp = [(mt, f) for mt, f in fairs.items() if "pairs" in f]
    ga = gb = ge = 0
    ga_combos, gb_combos, ge_combos = set(), set(), set()
    ga_c: Counter = Counter()
    for mt, f in allp:
        if f["bucket"] in ("props_only", "mixed"):
            for p in f["pairs"]:
                if p["key"] in ("moneyline|total", "spread|total"):
                    ga += 1
                    ga_c[p["key"]] += 1
                    ga_combos.add(mt)
        if f["same_player_cross_family_pairs"]:
            gb += f["same_player_cross_family_pairs"]
            gb_combos.add(mt)
        if f["nested_band_pairs"]:
            ge += f["nested_band_pairs"]
            ge_combos.add(mt)
    print(f"  (a) game-line pairs inside prop-carrying combos: {ga} pairs "
          f"({dict(ga_c)}) across {len(ga_combos)} combos "
          f"({len(ga_combos & cleared)} cleared)")
    print(f"  (b) same-player cross-family prop pairs: {gb} pairs across "
          f"{len(gb_combos)} combos ({len(gb_combos & cleared)} cleared)")
    fallback = Counter()
    fb_combos = set()
    for mt, f in allp:
        for p in f["pairs"]:
            if p["cat_promoted"] in ("flat-no-prior", "untyped-unknown-leg"):
                fallback[f"{p['cat_promoted']}:{p['key']}"] += 1
                fb_combos.add(mt)
    print(f"  (c) untyped/flat-fallback pair hits remaining (promoted config): "
          f"{sum(fallback.values())} across {len(fb_combos)} combos "
          f"({len(fb_combos & cleared)} cleared)")
    for k, v in fallback.most_common():
        print(f"        {v:5d}  {k}")
    print(f"  (d) in-play-only combo count (prints existed, all post-start): "
          f"{meta.get('n_inplay_only', '?')}  "
          f"(pre-game {meta.get('n_pregame', '?')}, never-traded {meta.get('n_no_prints', '?')})")
    print(f"  (e) nested-band-shaped MLB pairs (expect 0): {ge} pairs across "
          f"{len(ge_combos)} combos ({len(ge_combos & cleared)} cleared)")

    # ---- settlement bonus (partial — many MLB combos unsettled) -------------
    res = [r for r in rows if r["resolved"]]
    if res:
        yes = sum(r["settle_yes"] for r in res) / len(res)
        fp = statistics.mean(r["fair_promoted"] for r in res)
        fl = statistics.mean(r["fair_legacy"] for r in res)
        fc = statistics.mean(r["clearing"] for r in res)
        print(f"\n=== settlement bonus (resolved n={len(res)}) ===")
        print(f"  settled-YES {yes * 100:.1f}%  vs mean fair promoted {fp * 100:.1f}c / "
              f"legacy {fl * 100:.1f}c / clearing {fc * 100:.1f}c")

    slim = [{k: v for k, v in r.items() if k != "pairs"} for r in rows]
    json.dump({"rows": slim, "meta": meta}, open(outdir / "mlb_backtest.json", "w"))
    print("\nwrote mlb_backtest.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gather", "price", "analyze"])
    ap.add_argument("--outdir", type=Path, default=Path("data") / "backtests" / "mlb")
    ap.add_argument("--since", default="2026-07-05",
                    help="gather: only scan rfqs seen_at >= this date")
    ap.add_argument("--pregame-hours", type=float, default=DEFAULT_PREGAME_HOURS,
                    help="gather: first pitch ≈ leg expected_expiration − this many hours "
                         "(ESTIMATE: MLB ~3-3.5h game + settlement buffer); clearings after "
                         "it are dropped (strictly pre-game). Larger = stricter. "
                         "Default %(default)s.")
    ap.add_argument("--chunk-rows", type=int, default=250_000,
                    help="gather: rfqs rowid chunk size (auto-halves if the DB is hot)")
    ap.add_argument("--audit-sample", type=int, default=20_000,
                    help="gather: how many 'untraded-per-DB' priceable combos to ALSO "
                         "tape-fetch (fixed-seed random — both a poller-gap audit and an "
                         "unbiased clearing sample; the 2026-07-09 audit measured a 45%% "
                         "poller miss rate, so this sample IS the unbiased data source)")
    ap.add_argument("--refetch-outcomes", action="store_true",
                    help="gather: reuse inputs.pkl (skip the DB scan) and redo only the "
                         "tape/settlement side")
    ap.add_argument("--workers", type=int, default=7,
                    help="price: parallel fair-pass processes")
    a = ap.parse_args()
    if a.stage == "gather":
        gather(a.outdir, a.since, a.pregame_hours, a.chunk_rows,
               a.audit_sample, a.refetch_outcomes)
    elif a.stage == "price":
        price(a.outdir, a.workers)
    else:
        analyze(a.outdir)


if __name__ == "__main__":
    main()
