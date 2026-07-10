"""Mixed WC/MLB rerun GATHER (2026-07-10 pre-registered look-ahead A/B) — ONE
chunked rfqs scan classifying every distinct combo into THREE strict buckets:

  wc     every leg KXWC*
  mlb    every leg KXMLB*
  mixed  every leg in KXWC* UNION KXMLB*, with at least one of each

and writing SIX run dirs (<outroot>/{wc,mlb,mixed}_{fixed,lookahead}) that the
existing price/analyze stages (tools/backtests/wc_backtest.py /
mlb_backtest.py) consume unchanged:

  inputs.pkl            {ticker: {legs, sides, snaps}}            <- NO prices
      *_fixed     : snaps PREGAME-FILTERED (< per-combo cutoff) — the repaired
                    snapshot policy (the 2026-07-10 LOOK-AHEAD FIX).
      *_lookahead : snaps UNFILTERED — the OLD behavior, reproduced
                    DELIBERATELY for the A/B. Pre-registration:
                    docs/reports/2026-07-10-lookahead-rerun-preregistration.md
  outcomes.pkl          {ticker: {clearings, clearings_all_n, cutoff, resolved,
                        settle_yes, fetched}} — IDENTICAL between the two
                        variants of a bucket (written from the same object).
  printed_tickers.json  priceable combos with >=1 STRICTLY-PREGAME print.
  gather_meta.json      full counters — ALL buckets reported (honesty rules).

CUTOFF per combo = min over legs of (expected_expiration_time − offset), the
offset chosen PER LEG family: KXWC* -> 2.5h (soccer regulation + buffer),
KXMLB* -> 4.0h (9-inning game + buffer). A mixed combo applies each leg's own
family offset BEFORE taking the min, so no leg is in-play at the cutoff.

TAPE policy (authed get_trades, cursor-paginated, retry/backoff, sem 8):
  wc    -> ALL PRICEABLE WC combos + all DB-traded WC combos (complete coverage
           of every combo that can join an error row; the original wc_backtest
           "all combos" method was sized for a ~5k universe, this rerun's is
           100k+ — unpriceable+untraded combos cannot produce error rows).
           If even that exceeds WC_FULL_FETCH_HARD_CAP, falls back to
           candidates + fixed-seed sample capped WC_FALLBACK_SAMPLE_CAP
           (policy actually applied is recorded in gather_meta.json).
  mlb   -> DB-candidates (priceable ∩ combo_trades DISTINCT) + a fixed-seed
           random sample of 'untraded' priceable combos capped at 20,000 (the
           gate run's method, seed 20260709 — the 2026-07-09 audit measured a
           ~45% poller miss rate, so this sample IS the unbiased source).
  mixed -> same method, sample capped at 15,000.

DB DISCIPLINE — the prod recorder is LIVE: read-only URI, busy_timeout <= 10s,
rowid-bounded LIMIT chunks (a slow chunk halves the next), progress persisted
so an interrupted scan resumes; would_quotes via the rfq_id INDEX (batched
IN-lists); combo_trades is one bounded DISTINCT over a small table. Post-scan
stages cache to <outroot>/*.pkl so a crash never forces a DB re-scan.
ADDITIVE tool: no live module is touched (CLAUDE.md rule 8).

Usage (from repo root):
  python -m tools.backtests.mixed_wc_mlb_backtest gather \
      --outroot <dir> --since 2026-07-06
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "src")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1252 console
except Exception:
    pass

DB = "file:data/combomaker-prod.sqlite3?mode=ro"          # READ-ONLY, never write
KALSHI = "https://external-api.kalshi.com/trade-api/v2"    # public market reads

BUCKETS = ("wc", "mlb", "mixed")
# Per-LEG-family pregame offset: cutoff contribution = expected_expiration − offset.
PREGAME_OFFSET_HOURS = {"KXWC": 2.5, "KXMLB": 4.0}
AUDIT_CAP = {"mlb": 20_000, "mixed": 15_000}   # candidates + sample, per bucket
AUDIT_SEED = 20260709                          # the gate run's fixed seed
# WC preferred policy = COMPLETE coverage of all priceable + DB-traded combos.
# If that set exceeds the hard cap (this rerun's WC universe is 100k+ distinct
# combos vs the ~5k the original wc_backtest was sized for), fall back to the
# candidates+fixed-seed-sample method with a larger cap — reported in meta.
WC_FULL_FETCH_HARD_CAP = 60_000
WC_FALLBACK_SAMPLE_CAP = 40_000


def _leg_family(ticker: str) -> str | None:
    # ORDER MATTERS conceptually only for docs; prefixes don't overlap.
    if ticker.startswith("KXWC"):
        return "KXWC"
    if ticker.startswith("KXMLB"):
        return "KXMLB"
    return None


def _bucket_of(legs: tuple[tuple[str, str], ...]) -> str | None:
    """wc / mlb / mixed per the STRICT rules above; None = out of scope
    (any leg outside KXWC* ∪ KXMLB*, or an empty combo). legs are SLIM
    (ticker, side) tuples — the in-scope universe is millions of combos, so
    list-of-dict legs would multiply memory for nothing."""
    fams: set[str] = set()
    for t, _s in legs:
        f = _leg_family(t)
        if f is None:
            return None
        fams.add(f)
    if not fams:
        return None
    if fams == {"KXWC"}:
        return "wc"
    if fams == {"KXMLB"}:
        return "mlb"
    return "mixed"


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _adump(obj: object, path: Path) -> None:
    """ATOMIC pickle dump — a kill mid-write never leaves a corrupt cache."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    pickle.dump(obj, open(tmp, "wb"))
    tmp.replace(path)


def _first_rowid_at(con: sqlite3.Connection, since: str) -> int:
    """First rfqs rowid with seen_at >= since via BINARY SEARCH over indexed
    rowid probes (seen_at is unindexed; a MIN() scan is the forbidden query)."""
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
    outroot: Path, since: str, chunk_rows: int, progress_path: Path,
) -> tuple[dict[str, list[dict]], dict[str, str], dict[str, str], int]:
    """(a) rfqs -> {market_ticker: legs} + {ticker: bucket}, via chunked
    rowid-bounded LIMIT reads (LIKE pre-filter for KXWC/KXMLB is a cheap
    SUPERSET in SQLite; Python enforces the STRICT bucket rules; progress
    persists so an interrupted scan resumes). Returns rfq_ticker for the
    would_quotes join."""
    con = sqlite3.connect(DB, uri=True, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")  # <=10s: never camp on the recorder's lock

    ticker_legs: dict[str, list[dict]] = {}
    ticker_bucket: dict[str, str] = {}
    rfq_ticker: dict[str, str] = {}
    n_scanned = 0
    lo = None
    if progress_path.exists():
        try:
            st = pickle.load(open(progress_path, "rb"))
            lo, ticker_legs, ticker_bucket, rfq_ticker, n_scanned = (
                st["next_rowid"], st["ticker_legs"], st["ticker_bucket"],
                st["rfq_ticker"], st["n_scanned"])
            print(f"RESUME gather at rowid {lo} ({len(ticker_legs)} combos so far)",
                  flush=True)
        except Exception as exc:  # truncated dump (killed mid-write) → fresh start
            print(f"progress file unreadable ({exc}) — restarting scan", flush=True)
            lo = None
    if lo is None:
        lo = _first_rowid_at(con, since)
        print(f"scan rfqs from rowid {lo} (seen_at >= {since}), "
              "wc/mlb/mixed strict buckets…", flush=True)
    max_rowid = con.execute("SELECT MAX(rowid) FROM rfqs").fetchone()[0] or 0
    intern = sys.intern
    n_chunk = 0
    while lo <= max_rowid:
        hi_bound = lo + chunk_rows - 1
        t0 = time.time()
        rows = con.execute(
            "SELECT rowid, market_ticker, rfq_id, legs_json FROM rfqs"
            " WHERE rowid >= ? AND rowid <= ? AND seen_at >= ?"
            "   AND (legs_json LIKE '%KXWC%' OR legs_json LIKE '%KXMLB%') LIMIT ?",
            (lo, hi_bound, since, chunk_rows),
        ).fetchall()
        dt = time.time() - t0
        n_scanned += min(chunk_rows, max_rowid - lo + 1)
        for _rid, mt, rfq_id, legs_json in rows:
            if not legs_json:
                continue
            legs = tuple((intern(lg["market_ticker"]), intern(lg.get("side", "yes")))
                         for lg in json.loads(legs_json))
            b = _bucket_of(legs)
            if b is None:
                continue
            ticker_legs.setdefault(mt, legs)
            ticker_bucket.setdefault(mt, b)
            rfq_ticker[rfq_id] = mt
        lo = hi_bound + 1
        n_chunk += 1
        if dt > 20:  # hot DB: halve the next chunk instead of risking a >30s query
            chunk_rows = max(25_000, chunk_rows // 2)
            print(f"  slow chunk ({dt:.0f}s) -> chunk_rows={chunk_rows}", flush=True)
        if n_chunk % 5 == 0 or lo > max_rowid:  # dump cadence: the state reaches
            _adump({"next_rowid": lo, "ticker_legs": ticker_legs,  # GBs; per-chunk
                    "ticker_bucket": ticker_bucket,                 # dumps would
                    "rfq_ticker": rfq_ticker,                       # dominate runtime
                    "n_scanned": n_scanned}, progress_path)
        nb = {b: 0 for b in BUCKETS}
        for bb in ticker_bucket.values():
            nb[bb] += 1
        print(f"  rowid {lo - 1}/{max_rowid} · wc {nb['wc']} · mlb {nb['mlb']} · "
              f"mixed {nb['mixed']} ({dt:.1f}s/chunk)", flush=True)
    con.close()
    print(f"scanned ~{n_scanned} rfq rows -> {len(ticker_legs)} distinct in-scope "
          "combos", flush=True)
    return ticker_legs, ticker_bucket, rfq_ticker, n_scanned


def _fetch_snaps(rfq_ticker: dict[str, str]) -> dict[str, list[tuple[str, tuple]]]:
    """would_quotes -> {ticker: [(at, marginals)]} via a rowid-CHUNKED
    sequential sweep with a Python-side rfq_id join. This rerun's in-scope
    rfq_id set is 11.4M — batched IN-list index probes measured ~0.9s/batch
    (~5.7h total), while one bounded sequential sweep of the ~7.5M-row table
    is minutes (the wc_backtest full-scan pattern, made chunk-bounded so no
    single query can exceed the 30s budget)."""
    con = sqlite3.connect(DB, uri=True, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
    print("sweep would_quotes (rowid-chunked; Python-side rfq_id join)…", flush=True)
    ticker_snaps: dict[str, list[tuple[str, tuple]]] = defaultdict(list)
    max_rowid = con.execute("SELECT MAX(rowid) FROM would_quotes").fetchone()[0] or 0
    lo, chunk = 1, 250_000
    n_hit = 0
    while lo <= max_rowid:
        hi_bound = lo + chunk - 1
        t0 = time.time()
        rows = con.execute(
            "SELECT rfq_id, at, leg_probs_json FROM would_quotes"
            " WHERE rowid >= ? AND rowid <= ? LIMIT ?", (lo, hi_bound, chunk),
        ).fetchall()
        dt = time.time() - t0
        for rfq_id, at, probs_json in rows:
            mt = rfq_ticker.get(rfq_id)
            if mt is not None and probs_json:
                ticker_snaps[mt].append((at, tuple(json.loads(probs_json))))
                n_hit += 1
        lo = hi_bound + 1
        if dt > 20:  # hot DB: halve the next chunk instead of risking a >30s query
            chunk = max(25_000, chunk // 2)
            print(f"  slow chunk ({dt:.0f}s) -> chunk={chunk}", flush=True)
        print(f"  would_quotes rowid {lo - 1}/{max_rowid} · {n_hit} snaps "
              f"({dt:.1f}s/chunk)", flush=True)
    for mt in ticker_snaps:
        ticker_snaps[mt].sort(key=lambda x: x[0])
    con.close()
    return ticker_snaps


async def _fetch_clearings(
    combos: list[str], signer: object,
) -> tuple[dict[str, list[tuple[float, float, str, str]]], list[str]]:
    """Every combo's cleared trades from Kalshi's tape — COMPLETE per combo
    (poller-gap-immune). Each trade -> (yes_price_dollars, count, taker_side,
    created_time). Cursor-paginated; authed; retry/backoff so a 429 never
    silently drops a combo's tape. Returns (clearings, incomplete_tickers)."""
    from combomaker.exchange.rest import KalshiRestClient
    out: dict[str, list[tuple[float, float, str, str]]] = {}
    incomplete: list[str] = []
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
                    for attempt in range(5):
                        try:
                            resp = await rest.get_trades(**params)
                            break
                        except Exception:
                            await asyncio.sleep(0.5 * 2 ** attempt)
                    if resp is None:
                        print(f"  WARN clearings incomplete for {tk}", flush=True)
                        incomplete.append(tk)
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
                if done[0] % 500 == 0:
                    print(f"  clearings {done[0]}/{len(combos)}", flush=True)
        await asyncio.gather(*(one(t) for t in combos))
    return out, incomplete


async def _fetch_leg_meta(legs: list[str]) -> dict[str, dict]:
    """Per-leg settlement (status/result) + expected_expiration_time (the
    game-end anchor from which the start estimate derives). Public endpoint."""
    from combomaker.exchange.rest import KalshiRestClient
    out: dict[str, dict] = {}
    sem = asyncio.Semaphore(8)
    done = [0]
    async with KalshiRestClient(KALSHI, None) as rest:
        async def one(tk: str) -> None:
            async with sem:
                for attempt in range(4):
                    try:
                        m = (await rest.get_market(tk))["market"]
                        out[tk] = {"status": m.get("status"), "result": m.get("result"),
                                   "exp": m.get("expected_expiration_time")}
                        break
                    except Exception:
                        await asyncio.sleep(0.4 * (attempt + 1))
                else:
                    out[tk] = {"status": "ERR", "result": "", "exp": None}
                done[0] += 1
                if done[0] % 500 == 0:
                    print(f"  leg meta {done[0]}/{len(legs)}", flush=True)
        await asyncio.gather(*(one(t) for t in legs))
    return out


def gather(outroot: Path, since: str, chunk_rows: int) -> None:
    """One pass -> SIX run dirs. inputs and outcomes stay in SEPARATE files so
    a price stage can never reach outcomes; the fixed/lookahead pair of a
    bucket shares ONE outcomes object (identical by construction) and differs
    ONLY in the snapshot filtering of inputs.pkl."""
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.ops.dotenv import load_dotenv

    load_dotenv()  # KALSHI_PROD_* for the authed trade tape
    outroot.mkdir(parents=True, exist_ok=True)
    progress_path = outroot / "gather_progress.pkl"
    gathered_at = datetime.now(UTC).isoformat()

    # ── stage A: DB scan + snapshots (cached: a crash never re-scans the DB) ──
    scan_cache = outroot / "scan_cache.pkl"
    if scan_cache.exists():
        st = pickle.load(open(scan_cache, "rb"))
        ticker_legs, ticker_bucket, ticker_snaps, n_scanned = (
            st["ticker_legs"], st["ticker_bucket"], st["ticker_snaps"], st["n_scanned"])
        print(f"REUSE scan_cache.pkl ({len(ticker_legs)} combos)", flush=True)
    else:
        ticker_legs, ticker_bucket, rfq_ticker, n_scanned = _scan_db(
            outroot, since, chunk_rows, progress_path)
        ticker_snaps = _fetch_snaps(rfq_ticker)
        _adump({"ticker_legs": ticker_legs, "ticker_bucket": ticker_bucket,
                "ticker_snaps": dict(ticker_snaps), "n_scanned": n_scanned},
               scan_cache)
        progress_path.unlink(missing_ok=True)

    by_bucket = {b: sorted(mt for mt, bb in ticker_bucket.items() if bb == b)
                 for b in BUCKETS}
    priceable = {b: [mt for mt in by_bucket[b] if ticker_snaps.get(mt)]
                 for b in BUCKETS}
    for b in BUCKETS:
        print(f"bucket {b}: {len(by_bucket[b])} combos, {len(priceable[b])} priceable",
              flush=True)

    # ── stage B: candidate index (one bounded DISTINCT over small combo_trades) ──
    con2 = sqlite3.connect(DB, uri=True, timeout=10)
    con2.execute("PRAGMA busy_timeout=10000")
    traded = {r[0] for r in con2.execute("SELECT DISTINCT ticker FROM combo_trades")}
    con2.close()

    cand_sets: dict[str, list[str]] = {}
    audit_sets: dict[str, list[str]] = {}
    fetch_sets: dict[str, list[str]] = {}
    fetch_policy: dict[str, str] = {}
    for b in BUCKETS:
        cand_sets[b] = [mt for mt in priceable[b] if mt in traded]
        untraded = [mt for mt in priceable[b] if mt not in traded]
        if b == "wc":
            # WC preferred = COMPLETE tape coverage of every combo that can
            # join an error row: ALL PRICEABLE WC combos + every DB-traded WC
            # combo. (The original wc_backtest fetched ALL distinct WC combos,
            # but was sized for a ~5k universe; an unpriceable+untraded combo
            # can never produce an error row, so fetching it is pure rate-limit
            # exposure — the MLB candidate-index argument.) If even that set
            # blows past the hard cap, fall back to candidates+sample.
            full = sorted(set(priceable[b]) | {mt for mt in by_bucket[b] if mt in traded})
            if len(full) <= WC_FULL_FETCH_HARD_CAP:
                fetch_sets[b], audit_sets[b] = full, []
                fetch_policy[b] = ("all-priceable+db-traded (DEVIATION from 'all WC"
                                   " combos': unpriceable+untraded combos cannot join"
                                   " error rows)")
            else:
                audit_sets[b] = random.Random(AUDIT_SEED).sample(
                    untraded, min(WC_FALLBACK_SAMPLE_CAP, len(untraded)))
                fetch_sets[b] = cand_sets[b] + audit_sets[b]
                fetch_policy[b] = (f"candidates+audit WC-FALLBACK (full set {len(full)}"
                                   f" > hard cap {WC_FULL_FETCH_HARD_CAP}; seed"
                                   f" {AUDIT_SEED}, cap {WC_FALLBACK_SAMPLE_CAP})")
        else:
            audit_sets[b] = random.Random(AUDIT_SEED).sample(
                untraded, min(AUDIT_CAP[b], len(untraded)))
            fetch_sets[b] = cand_sets[b] + audit_sets[b]
            fetch_policy[b] = "candidates+audit"
        print(f"bucket {b}: fetch {len(fetch_sets[b])} combos "
              f"({len(cand_sets[b])} DB-candidates + {len(audit_sets[b])} audit; "
              f"policy {fetch_policy[b]})", flush=True)

    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())

    # ── stage C: tape clearings (cached) ──
    clr_cache = outroot / "clearings_cache.pkl"
    if clr_cache.exists():
        st = pickle.load(open(clr_cache, "rb"))
        clearings, incomplete = st["clearings"], st["incomplete"]
        print(f"REUSE clearings_cache.pkl ({len(clearings)} combos)", flush=True)
    else:
        union = sorted({mt for b in BUCKETS for mt in fetch_sets[b]})
        print(f"fetch clearings from Kalshi tape for {len(union)} combos…", flush=True)
        clearings, incomplete = asyncio.run(_fetch_clearings(union, signer))
        _adump({"clearings": clearings, "incomplete": incomplete}, clr_cache)

    # ── stage D: leg meta for ALL distinct legs across buckets (cached) ──
    meta_cache = outroot / "leg_meta_cache.pkl"
    if meta_cache.exists():
        leg_meta = pickle.load(open(meta_cache, "rb"))
        print(f"REUSE leg_meta_cache.pkl ({len(leg_meta)} legs)", flush=True)
    else:
        distinct_legs = sorted({t for legs in ticker_legs.values() for t, _s in legs})
        print(f"fetch settlement + expiry for {len(distinct_legs)} legs…", flush=True)
        leg_meta = asyncio.run(_fetch_leg_meta(distinct_legs))
        _adump(leg_meta, meta_cache)

    def leg_won(tk: str, side: str) -> bool | None:
        s = leg_meta.get(tk)
        if not s or s["status"] not in ("finalized", "settled") or s["result"] not in ("yes", "no"):
            return None
        return s["result"] == side

    # ── stage E: per bucket — outcomes + fixed/lookahead inputs + meta ──
    incomplete_set = set(incomplete)
    for b in BUCKETS:
        fetched = set(fetch_sets[b])
        outset = sorted(set(priceable[b]) | fetched)
        outcomes: dict[str, dict] = {}
        n_pre_f = n_inplay_only = n_printless_fetched = n_no_cutoff = 0
        for mt in outset:
            legs = ticker_legs[mt]
            # cutoff = min over legs of (expiry − per-LEG-family offset):
            # earliest estimated start, no leg in-play before it.
            starts = []
            for t, _s in legs:
                exp = leg_meta.get(t, {}).get("exp")
                if exp:
                    off = timedelta(hours=PREGAME_OFFSET_HOURS[_leg_family(t)])
                    starts.append(_parse_ts(exp) - off)
            cutoff = min(starts) if starts else None
            if cutoff is None:
                n_no_cutoff += 1
            allc = clearings.get(mt, []) if mt in fetched else []
            pre = [c for c in allc if cutoff and _parse_ts(c[3]) < cutoff]
            if mt in fetched:
                if pre:
                    n_pre_f += 1
                elif allc:
                    n_inplay_only += 1  # prints existed, all in-play/post-start
                else:
                    n_printless_fetched += 1
            wins = [leg_won(t, s) for t, s in legs]
            resolved = all(w is not None for w in wins)
            outcomes[mt] = {
                "clearings": pre,                 # STRICTLY PRE-GAME prints only
                "clearings_all_n": len(allc),     # audit: prints before filtering
                "cutoff": cutoff.isoformat() if cutoff else None,
                "resolved": resolved,
                "settle_yes": (1 if all(wins) else 0) if resolved else None,
                "fetched": mt in fetched,
            }

        def _pregame_snaps(mt: str) -> list[tuple[str, list[float]]]:
            cut = outcomes[mt]["cutoff"] if mt in outcomes else None
            sn = ticker_snaps.get(mt, [])
            if cut is None:
                return list(sn)
            cdt = _parse_ts(cut)
            return [s for s in sn if _parse_ts(s[0]) < cdt]

        printed = sorted(mt for mt in priceable[b] if outcomes[mt]["clearings"])
        printed_set = set(printed)
        pre_snaps = {mt: _pregame_snaps(mt) for mt in by_bucket[b]}
        n_empt_priceable = sum(1 for mt in priceable[b] if not pre_snaps[mt])
        n_empt_printed = sum(1 for mt in printed if not pre_snaps[mt])
        audit_hits = [t for t in audit_sets[b] if clearings.get(t)]
        n_res_priceable = sum(1 for mt in priceable[b] if outcomes[mt]["resolved"])

        base_meta = {
            "bucket": b, "since": since, "gathered_at": gathered_at,
            "pregame_offset_hours": PREGAME_OFFSET_HOURS,
            "fetch_policy": fetch_policy[b],
            "audit_seed": AUDIT_SEED,
            "audit_cap": AUDIT_CAP.get(b, 0),
            "n_rfq_rows_scanned": n_scanned,
            "n_combos": len(by_bucket[b]),
            "n_priceable": len(priceable[b]),
            "n_fetched": len(fetch_sets[b]),
            "n_candidates": len(cand_sets[b]),
            "n_audit": len(audit_sets[b]),
            "n_audit_hits": len(audit_hits),
            "audit_hit_tickers": audit_hits[:50],
            "n_pregame_printed_fetched": n_pre_f,
            "n_pregame_printed_priceable": len(printed),
            "n_inplay_only": n_inplay_only,
            "n_printless_fetched": n_printless_fetched,
            "n_unfetched_priceable": len(set(priceable[b]) - fetched),
            "n_no_cutoff": n_no_cutoff,
            "n_resolved_priceable": n_res_priceable,
            "n_snap_emptied_priceable_fixed": n_empt_priceable,
            "n_snap_emptied_printed_fixed": n_empt_printed,
            "n_clearings_incomplete": len(incomplete_set & fetched),
        }
        for variant in ("fixed", "lookahead"):
            d = outroot / f"{b}_{variant}"
            d.mkdir(parents=True, exist_ok=True)
            inputs = {mt: {"legs": [t for t, _s in ticker_legs[mt]],
                           "sides": [s for _t, s in ticker_legs[mt]],
                           "snaps": (pre_snaps[mt] if variant == "fixed"
                                     else list(ticker_snaps.get(mt, [])))}
                      for mt in by_bucket[b]}
            _adump(inputs, d / "inputs.pkl")
            del inputs
            _adump(outcomes, d / "outcomes.pkl")  # SAME object both dirs
            json.dump(printed, open(d / "printed_tickers.json", "w"))
            json.dump({**base_meta, "variant": variant,
                       "snapshot_policy": ("last pre-cutoff snapshot (pregame-filtered)"
                                           if variant == "fixed"
                                           else "latest snapshot UNFILTERED (old behavior,"
                                                " deliberate A/B)")},
                      open(d / "gather_meta.json", "w"), indent=1)
        print(f"\n=== bucket {b} ===")
        print(f"  combos {len(by_bucket[b])} · priceable {len(priceable[b])} · "
              f"fetched {len(fetch_sets[b])} ({len(cand_sets[b])} cand + "
              f"{len(audit_sets[b])} audit, {len(audit_hits)} audit hits)")
        print(f"  pregame-printed {n_pre_f} fetched / {len(printed)} priceable · "
              f"in-play-only {n_inplay_only} · printless-fetched {n_printless_fetched} · "
              f"unfetched-priceable {base_meta['n_unfetched_priceable']}")
        print(f"  fixed-filter: snap-emptied {n_empt_priceable} priceable "
              f"({n_empt_printed} of the printed) · no-cutoff {n_no_cutoff} · "
              f"resolved {n_res_priceable} priceable · "
              f"tape-incomplete {base_meta['n_clearings_incomplete']}", flush=True)
    print("\nWROTE six run dirs under", outroot, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gather"])
    ap.add_argument("--outroot", type=Path, required=True,
                    help="parent dir for the six {wc,mlb,mixed}_{fixed,lookahead} run dirs")
    ap.add_argument("--since", default="2026-07-06",
                    help="gather: only scan rfqs seen_at >= this date")
    ap.add_argument("--chunk-rows", type=int, default=250_000,
                    help="gather: rfqs rowid chunk size (auto-halves if the DB is hot)")
    a = ap.parse_args()
    gather(a.outroot, a.since, a.chunk_rows)


if __name__ == "__main__":
    main()
