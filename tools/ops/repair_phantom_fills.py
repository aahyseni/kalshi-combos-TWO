"""PHANTOM-FILL REPAIR (2026-09-04 build, item D) — READ-ONLY by default.

Joins the local fills ledger against exchange truth (``GET /portfolio/fills``
+ ``GET /portfolio/settlements`` — a saved all-time JSON pull, or a fresh
read-only pull with ``--pull``) on the EXACT key ``fills.order_id ==
Fill.order_id`` and classifies every local row:

  matched               order_id found on the tape (real fill)
  legacy_prefix_match   local order_id is a PREFIX of a tape order_id (the
                        three 2026-07-18 ``fill:*-reconciled`` rows whose
                        manual reconcile truncated the id to 13 chars) — real
  phantom               no tape fill for the order AND the ticker's settlement
                        count equals the ticker's MATCHED store rows exactly
                        (or the ticker never settled with us and has no tape
                        fill at all) — the exchange never held it
  unresolved            no tape fill, but settlement evidence does NOT
                        corroborate a phantom (never repaired here — listed)
  null_order_id         no exact key (never repaired here — listed)

and, separately, the tape fills with NO local row (writer-path misses — the
opposite direction, listed only, never written: one-writer rule).

``--dry-run`` (default) prints every class with evidence and writes a JSON
summary. ``--apply`` first copies the store to ``data/backups/<stamp>-
<name>`` (timestamped, never overwritten) and then marks each PHANTOM row
across the three ledgers exactly as the live ``Store.void_phantom_fill``
does: ``fills.status='phantom'`` (+ ``verified_at`` stamp,
``exchange_fill_id='phantom:repair_tool:<reason>'``), the OPEN
``position_ledger`` row → ``phantom``, ``ev_ledger`` expected/realized → 0.
Rows are never deleted (audit trail). Refuses to apply while a bot heartbeat
is fresh (never touch a live store) and refuses on a store that lacks the
verification columns unless ``--migrate`` is given (adds them exactly like
``Store._ensure_fills_verification_columns``).

Usage (worktree root, bot DOWN):

  PYTHONPATH=src python tools/ops/repair_phantom_fills.py \\
      --store D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3 \\
      --exchange-json <alltime_exchange.json> --dry-run --out phantom_dryrun.json

  ... --pull --env prod          # fresh read-only exchange pull instead of JSON
  ... --apply                    # after reading the dry-run; backup is automatic
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_STORE = "D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3"
KALSHI_PROD = "https://external-api.kalshi.com/trade-api/v2"
#: A local order_id at least this long that prefixes a tape order_id is the
#: same order (the 2026-07-18 reconcile truncated UUIDs to 13 chars = the
#: first two dash-groups; 13 hex+dash chars ≈ 2^44 keyspace — no collisions
#: on a 4k-row tape).
PREFIX_MIN_LEN = 13
HEARTBEAT_FRESH_S = 120.0

JsonDict = dict[str, Any]


# ----------------------------------------------------------------- exchange


async def pull_exchange(env: str) -> JsonDict:
    """READ-ONLY all-time pull of fills + settlements (paginated)."""
    sys.path.insert(0, str(REPO / "src"))
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.exchange.rest import KalshiRestClient
    from combomaker.ops.dotenv import load_dotenv

    load_dotenv()
    base = KALSHI_PROD if env == "prod" else "https://demo-api.kalshi.co/trade-api/v2"

    async def paginate(fn: Any, key: str, **params: Any) -> list[JsonDict]:
        out: list[JsonDict] = []
        cursor: str | None = None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            for attempt in range(6):
                try:
                    r = await fn(**p)
                    break
                except Exception as exc:  # noqa: BLE001 — bounded retry
                    print("retry", attempt, repr(exc)[:120])
                    await asyncio.sleep(2 + 2 * attempt)
            else:
                raise RuntimeError("pagination failed")
            out.extend(r.get(key) or [])
            cursor = r.get("cursor")
            if not cursor:
                break
            await asyncio.sleep(0.15)
        return out

    async with KalshiRestClient(
        base, RequestSigner(Credentials.for_env(env), SystemClock())
    ) as rest:
        fills = await paginate(rest.get_fills, "fills", limit=200)
        settlements = await paginate(rest.get_settlements, "settlements", limit=200)
    return {"pulled_at": time.time(), "fills": fills, "settlements": settlements}


# --------------------------------------------------------------- classify


def _cc(fp: Any) -> int:
    """FixedPoint 2dp string → centi-contracts (integer; no float money)."""
    s = str(fp)
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    val = int(whole or "0") * 100 + int(frac or "0")
    return -val if neg else val


def classify(store_rows: list[JsonDict], exchange: JsonDict) -> JsonDict:
    """Pure join + classification (unit-testable; no I/O)."""
    fills = exchange.get("fills") or []
    settlements = exchange.get("settlements") or []
    by_order: dict[str, list[JsonDict]] = defaultdict(list)
    for f in fills:
        oid = str(f.get("order_id") or "")
        if oid:
            by_order[oid].append(f)
    tape_orders = sorted(by_order)
    settle_by_ticker: dict[str, JsonDict] = {}
    for s in settlements:
        settle_by_ticker[str(s.get("ticker"))] = s

    def prefix_match(oid: str) -> str | None:
        if len(oid) < PREFIX_MIN_LEN:
            return None
        hits = [t for t in tape_orders if t.startswith(oid)]
        return hits[0] if len(hits) == 1 else None

    # Pass 1: exact / prefix.
    for row in store_rows:
        oid = row.get("order_id")
        row["_class"] = None
        row["_tape_order_id"] = None
        if not oid:
            row["_class"] = "null_order_id"
            continue
        if oid in by_order:
            row["_class"] = "matched"
            row["_tape_order_id"] = oid
            continue
        pm = prefix_match(str(oid))
        if pm is not None:
            row["_class"] = "legacy_prefix_match"
            row["_tape_order_id"] = pm
    # Per-ticker sums of MATCHED store rows (by side).
    matched_sum: dict[tuple[str, str], int] = defaultdict(int)
    tape_sum: dict[tuple[str, str], int] = defaultdict(int)
    for row in store_rows:
        if row["_class"] in ("matched", "legacy_prefix_match"):
            matched_sum[(row["combo_ticker"], row["our_side"])] += int(row["contracts_centi"])
    for f in fills:
        t = str(f.get("market_ticker") or f.get("ticker") or "")
        side = str(f.get("outcome_side") or f.get("side") or "").lower()
        tape_sum[(t, side)] += _cc(f.get("count_fp") or f.get("count") or "0")
    # Pass 2: phantom corroboration for the unmatched.
    for row in store_rows:
        if row["_class"] is not None:
            continue
        t, side = row["combo_ticker"], row["our_side"]
        settle = settle_by_ticker.get(t)
        settle_cc: int | None = None
        if settle is not None:
            key = "no_count_fp" if side == "no" else "yes_count_fp"
            try:
                settle_cc = _cc(settle.get(key) or "0")
            except ValueError:
                settle_cc = None
        row["_settlement_cc"] = settle_cc
        row["_matched_store_cc"] = matched_sum.get((t, side), 0)
        row["_tape_cc"] = tape_sum.get((t, side), 0)
        if settle_cc is not None and settle_cc == matched_sum.get((t, side), 0):
            row["_class"] = "phantom"
            row["_reason"] = "settlement_equals_matched_rows"
        elif settle is None and tape_sum.get((t, side), 0) == 0:
            row["_class"] = "phantom"
            row["_reason"] = "no_settlement_no_tape_fill"
        else:
            row["_class"] = "unresolved"
            row["_reason"] = "settlement_does_not_corroborate"
    store_orders = {
        str(r.get("order_id")) for r in store_rows if r.get("order_id")
    } | {str(r.get("_tape_order_id")) for r in store_rows if r.get("_tape_order_id")}
    tape_only: list[JsonDict] = []
    for oid in tape_orders:
        if oid in store_orders:
            continue
        prints = by_order[oid]
        tape_only.append(
            {
                "order_id": oid,
                "ticker": prints[0].get("market_ticker") or prints[0].get("ticker"),
                "side": prints[0].get("outcome_side") or prints[0].get("side"),
                "contracts_centi": sum(
                    _cc(p.get("count_fp") or p.get("count") or "0") for p in prints
                ),
                "created_time": min(str(p.get("created_time")) for p in prints),
                "is_taker": any(bool(p.get("is_taker")) for p in prints),
                "n_prints": len(prints),
            }
        )
    # Count mismatches among matched rows (partial executions on the tape).
    count_mismatch: list[JsonDict] = []
    for row in store_rows:
        if row["_class"] != "matched":
            continue
        tape_cc = sum(
            _cc(p.get("count_fp") or p.get("count") or "0")
            for p in by_order[str(row["order_id"])]
        )
        if tape_cc != int(row["contracts_centi"]):
            count_mismatch.append(
                {
                    "fill_ref": row["fill_ref"],
                    "order_id": row["order_id"],
                    "ticker": row["combo_ticker"],
                    "store_cc": int(row["contracts_centi"]),
                    "tape_cc": tape_cc,
                }
            )
    by_class: dict[str, list[JsonDict]] = defaultdict(list)
    for row in store_rows:
        by_class[str(row["_class"])].append(row)
    return {
        "n_store_rows": len(store_rows),
        "n_tape_fills": len(fills),
        "n_tape_orders": len(tape_orders),
        "n_settlements": len(settlements),
        "by_class": {k: len(v) for k, v in sorted(by_class.items())},
        "phantom": [_row_out(r) for r in by_class.get("phantom", [])],
        "unresolved": [_row_out(r) for r in by_class.get("unresolved", [])],
        "legacy_prefix_match": [_row_out(r) for r in by_class.get("legacy_prefix_match", [])],
        "null_order_id": [_row_out(r) for r in by_class.get("null_order_id", [])],
        "tape_only": tape_only,
        "matched_count_mismatch": count_mismatch,
    }


def _row_out(r: JsonDict) -> JsonDict:
    keys = (
        "id", "at", "fill_ref", "order_id", "combo_ticker", "our_side", "contracts_centi",
        "price_cc", "fee_cc", "status", "_class", "_reason", "_tape_order_id",
        "_settlement_cc", "_matched_store_cc", "_tape_cc", "provenance", "quote_id",
    )
    return {k: r.get(k) for k in keys if k in r}


# ------------------------------------------------------------------ store


def _read_store_rows(path: str, *, read_only: bool) -> tuple[list[JsonDict], bool]:
    uri = f"file:{path}?mode=ro" if read_only else f"file:{path}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(fills)")]
        has_status = "status" in cols
        status_sel = ", status" if has_status else ""
        rows: list[JsonDict] = []
        for rec in conn.execute(
            "SELECT id, at, fill_ref, order_id, combo_ticker, our_side, contracts_centi,"
            f" price_cc, fee_cc, raw_json{status_sel} FROM fills ORDER BY id"
        ):
            row: JsonDict = {
                "id": rec[0], "at": rec[1], "fill_ref": rec[2], "order_id": rec[3],
                "combo_ticker": rec[4], "our_side": rec[5], "contracts_centi": rec[6],
                "price_cc": rec[7], "fee_cc": rec[8],
                "status": rec[10] if has_status else "booked",
            }
            try:
                raw = json.loads(rec[9])
            except (TypeError, ValueError):
                raw = {}
            row["quote_id"] = raw.get("quote_id")
            row["provenance"] = (
                "poll" if raw.get("recovered_via_poll")
                else "fills_poll" if raw.get("recovered_via_fills_poll")
                else "ws" if "_ws_recv_mono_ns" in raw
                else "other"
            )
            rows.append(row)
        return rows, has_status
    finally:
        conn.close()


def _heartbeat_fresh(store_path: str) -> bool:
    """True if a bot heartbeat next to the store was written recently."""
    data_dir = Path(store_path).parent
    for name in ("heartbeat.json", "heartbeat", "liveness.json"):
        p = data_dir / name
        if p.exists() and time.time() - p.stat().st_mtime < HEARTBEAT_FRESH_S:
            return True
    return False


def apply_void(
    store_path: str,
    phantom_rows: list[JsonDict],
    *,
    migrate: bool,
    backup_dir: Path | None = None,
) -> JsonDict:
    """Mark the given rows phantom across the three ledgers (mirrors
    Store.void_phantom_fill — parity-tested against it), after a timestamped
    backup under ``backup_dir`` (default ``data/backups/``)."""
    if _heartbeat_fresh(store_path):
        raise SystemExit("REFUSED: a fresh bot heartbeat sits next to the store — bot must be DOWN")
    src = Path(store_path)
    backups = backup_dir if backup_dir is not None else REPO / "data" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = backups / f"{stamp}-{src.name}"
    if dest.exists():
        raise SystemExit(f"REFUSED: backup target exists: {dest}")
    shutil.copy2(src, dest)
    for sidecar in ("-wal", "-shm"):
        side = Path(str(src) + sidecar)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + sidecar))
    conn = sqlite3.connect(store_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(fills)")]
        if "status" not in cols:
            if not migrate:
                raise SystemExit(
                    "REFUSED: fills has no verification columns; rerun with --migrate "
                    "(adds status/verified_at/exchange_fill_id exactly like Store.open)"
                )
            conn.execute("ALTER TABLE fills ADD COLUMN status TEXT NOT NULL DEFAULT 'booked'")
            conn.execute("ALTER TABLE fills ADD COLUMN verified_at TEXT")
            conn.execute("ALTER TABLE fills ADD COLUMN exchange_fill_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_status ON fills (status)")
        now = datetime.now(UTC).isoformat()
        touched = {"fills": 0, "position_ledger": 0, "ev_ledger": 0}
        for row in phantom_rows:
            ref = row["fill_ref"]
            cur = conn.execute(
                "UPDATE fills SET status='phantom', verified_at=?, exchange_fill_id=?"
                " WHERE fill_ref=? AND status='booked'",
                (now, f"phantom:repair_tool:{row.get('_reason')}", ref),
            )
            if cur.rowcount == 0:
                continue
            touched["fills"] += cur.rowcount
            cur = conn.execute(
                "UPDATE position_ledger SET status='phantom', reconciled_at=?"
                " WHERE position_id=? AND status='open'",
                (now, ref),
            )
            touched["position_ledger"] += cur.rowcount
            cur = conn.execute(
                "UPDATE ev_ledger SET expected_edge_cc=0, realized_pnl_cc=0 WHERE fill_ref=?",
                (ref,),
            )
            touched["ev_ledger"] += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"backup": str(dest), "touched": touched}


# -------------------------------------------------------------------- main


def _print_table(title: str, rows: list[JsonDict], cols: list[str], limit: int = 200) -> None:
    print(f"\n== {title} ({len(rows)})")
    if not rows:
        return
    print(" | ".join(cols))
    for r in rows[:limit]:
        print(" | ".join(str(r.get(c, ""))[:44] for c in cols))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--exchange-json", help="saved all-time pull (fills + settlements)")
    ap.add_argument("--pull", action="store_true", help="fresh READ-ONLY exchange pull")
    ap.add_argument("--env", default="prod")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--migrate", action="store_true",
        help="--apply: add the verification columns if missing",
    )
    ap.add_argument("--out", help="JSON summary path")
    args = ap.parse_args(argv)

    if args.pull:
        exchange = asyncio.run(pull_exchange(args.env))
    elif args.exchange_json:
        exchange = json.loads(Path(args.exchange_json).read_text(encoding="utf-8"))
    else:
        ap.error("give --exchange-json or --pull")

    rows, has_status = _read_store_rows(args.store, read_only=True)
    result = classify(rows, exchange)
    result["store"] = args.store
    result["store_has_verification_columns"] = has_status
    result["mode"] = "apply" if args.apply else "dry-run"

    print(
        f"store rows {result['n_store_rows']}  tape fills {result['n_tape_fills']}"
        f" (orders {result['n_tape_orders']})  settlements {result['n_settlements']}"
    )
    print("by class:", result["by_class"])
    cols = ["at", "fill_ref", "order_id", "combo_ticker", "our_side", "contracts_centi", "price_cc",
            "status", "provenance", "_reason", "_settlement_cc", "_matched_store_cc"]
    _print_table("PHANTOM (no tape fill; settlement corroborates)", result["phantom"], cols)
    _print_table(
        "UNRESOLVED (no tape fill; NOT corroborated — never repaired)", result["unresolved"], cols
    )
    _print_table("LEGACY PREFIX MATCH (real; truncated ids)", result["legacy_prefix_match"], cols)
    _print_table("NULL order_id (no exact key — never repaired)", result["null_order_id"], cols)
    _print_table(
        "TAPE-ONLY exchange fills with NO store row (writer-path misses — listed, unrepaired)",
        result["tape_only"],
        ["created_time", "order_id", "ticker", "side", "contracts_centi", "is_taker", "n_prints"],
    )
    _print_table(
        "MATCHED rows whose tape count differs (partials)",
        result["matched_count_mismatch"],
        ["fill_ref", "order_id", "ticker", "store_cc", "tape_cc"],
    )

    if args.apply:
        phantom_rows = [r for r in rows if r.get("_class") == "phantom"]
        outcome = apply_void(args.store, phantom_rows, migrate=args.migrate)
        result["apply"] = outcome
        print("\nAPPLIED:", outcome)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print("\nwrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
