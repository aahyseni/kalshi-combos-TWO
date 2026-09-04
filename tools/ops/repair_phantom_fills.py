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
summary. ``--apply``:

  1. REFUSES while the bot may be alive (review fix 2026-09-04: the first
     version looked for files the bot never writes). Liveness evidence is the
     bot's own signals in the store's data_dir — ``heartbeat.txt`` (the
     dedicated liveness task), ``supervisor_heartbeat.txt`` (the watchdog) and
     ``loop_progress.json`` (per-loop progress) — read with the SAME readers
     the supervisor uses; the freshness window is the supervisor's own
     ``supervisor.heartbeat_timeout_s`` from the live YAML (values only, the
     file is never echoed), else the ``SupervisorConfig`` default. A present
     but unreadable/corrupt signal is refused too (fail-closed). The check
     runs BEFORE any backup or store connection.
  2. Writes a ROW-LEVEL backup under ``data/backups/`` (review fix: the live
     store is 213 GB — a whole-file copy for a 28-row repair is wrong):
     ``<stamp>-phantom_rows_backup.json`` with the complete pre-state of every
     affected fills / position_ledger / ev_ledger row, and
     ``<stamp>-restore.sql`` with the exact UPDATEs that put them back.
  3. Marks each PHANTOM row across the three ledgers exactly as the live
     ``Store.void_phantom_fill`` does: ``fills.status='phantom'`` (+
     ``verified_at`` stamp, ``exchange_fill_id='phantom:repair_tool:<reason>'``),
     the OPEN ``position_ledger`` row → ``phantom``, ``ev_ledger`` expected/
     realized → 0. Rows are never deleted (audit trail).

A store that lacks the verification columns (the live store today) is
refused unless ``--migrate`` is given, which adds them exactly like
``Store._ensure_fills_verification_columns`` — including the once-only
``store_meta`` watermark (MAX(fills.id) + stamp) the restart re-arm and the
ledger-quantity alarm scope on.

Usage (worktree root, bot DOWN):

  PYTHONPATH=src python tools/ops/repair_phantom_fills.py \\
      --store D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3 \\
      --exchange-json <alltime_exchange.json> --dry-run --out phantom_dryrun.json

  ... --pull --env prod          # fresh read-only exchange pull instead of JSON
  ... --apply --migrate          # after reading the dry-run; backup is automatic
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
#: The bot's liveness signals, by filename in the store's data_dir (the
#: writers: ops/quote_app.py ``Heartbeat(... data_dir / "heartbeat.txt")``,
#: ops/supervisor.py ``SUPERVISOR_HEARTBEAT_FILENAME``, risk/progress.py
#: ``PROGRESS_FILENAME``).
BOT_HEARTBEAT_FILENAME = "heartbeat.txt"
SUPERVISOR_HEARTBEAT_FILENAME = "supervisor_heartbeat.txt"
PROGRESS_FILENAME = "loop_progress.json"

JsonDict = dict[str, Any]


def _src_on_path() -> None:
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


# ----------------------------------------------------------------- exchange


async def pull_exchange(env: str) -> JsonDict:
    """READ-ONLY all-time pull of fills + settlements (paginated)."""
    _src_on_path()
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
        row_oid = row.get("order_id")
        row["_class"] = None
        row["_tape_order_id"] = None
        if not row_oid:
            row["_class"] = "null_order_id"
            continue
        if row_oid in by_order:
            row["_class"] = "matched"
            row["_tape_order_id"] = row_oid
            continue
        pm = prefix_match(str(row_oid))
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


# --------------------------------------------------------------- liveness


def liveness_window_s(config_path: Path | None) -> float:
    """The freshness window for the bot's liveness signals = the supervisor's
    own wedge anchor ``supervisor.heartbeat_timeout_s`` (the live YAML when
    given/found — values only, the file is never echoed: it sits next to
    secrets), else the ``SupervisorConfig`` code default. No number of this
    tool's own."""
    _src_on_path()
    from combomaker.ops.config import SupervisorConfig

    default = float(SupervisorConfig().heartbeat_timeout_s)
    if config_path is None or not config_path.exists():
        return default
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — an unreadable config falls back to the anchor default
        return default
    sup = data.get("supervisor") if isinstance(data, dict) else None
    if isinstance(sup, dict) and "heartbeat_timeout_s" in sup:
        try:
            return float(sup["heartbeat_timeout_s"])
        except (TypeError, ValueError):
            return default
    return default


def default_config_path() -> Path | None:
    """The bot's launch config: the gitignored local override first (what
    tools/ops/start_all.ps1 launches with), then the base per-env files."""
    candidates = sorted(REPO.glob("config/*.local.yaml")) + sorted(REPO.glob("config/*.yaml"))
    return candidates[0] if candidates else None


def bot_liveness_evidence(store_path: str, *, window_s: float) -> list[str]:
    """Every reason to believe a bot is ALIVE on this store's data_dir, as
    strings (empty ⇒ no evidence). Read with the bot's own readers
    (``HeartbeatReader`` / ``ProgressReader`` parsing) against the real clock:

      * ``heartbeat.txt`` / ``supervisor_heartbeat.txt``: age ≤ window ⇒ alive;
        present but unparseable / implausibly future ⇒ REFUSED too (the
        reader's fail-closed None);
      * ``loop_progress.json``: ``written_at`` age ≤ the larger of the window
        and every loop's own derived ``stall_after_s`` ⇒ alive; present but
        unreadable ⇒ refused.

    An ABSENT file is no evidence (the relaunch purge deletes them; the live
    data_dir today holds none of the three)."""
    _src_on_path()
    from combomaker.core.clock import SystemClock
    from combomaker.risk.heartbeat import HeartbeatReader
    from combomaker.risk.progress import ProgressReader

    clock = SystemClock()
    data_dir = Path(store_path).resolve().parent
    evidence: list[str] = []
    for name in (BOT_HEARTBEAT_FILENAME, SUPERVISOR_HEARTBEAT_FILENAME):
        path = data_dir / name
        if not path.exists():
            continue
        age = HeartbeatReader(clock, path).read_age_s(retries=1)
        if age is None:
            evidence.append(f"{name}: present but unreadable/implausible (fail-closed)")
        elif age <= window_s:
            evidence.append(f"{name}: beaten {age:.1f}s ago (window {window_s:.1f}s)")
    progress = data_dir / PROGRESS_FILENAME
    if progress.exists():
        reader = ProgressReader(clock, progress)
        payload = reader._read(retries=1)  # noqa: SLF001 — the bot's own parser
        if payload is None:
            evidence.append(f"{PROGRESS_FILENAME}: present but unreadable (fail-closed)")
        else:
            age = reader.file_age_s(payload)
            bound = window_s
            loops = payload.get("loops")
            if isinstance(loops, dict):
                for state in loops.values():
                    if isinstance(state, dict):
                        try:
                            bound = max(bound, float(state.get("stall_after_s", 0.0)))
                        except (TypeError, ValueError):
                            pass
            if age is None:
                evidence.append(f"{PROGRESS_FILENAME}: present but unreadable (fail-closed)")
            elif age <= bound:
                evidence.append(
                    f"{PROGRESS_FILENAME}: written {age:.1f}s ago (bound {bound:.1f}s)"
                )
    return evidence


# ------------------------------------------------------------------ apply


_BACKUP_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    # table: (key column, columns the repair changes — the restore.sql scope)
    "fills": ("fill_ref", ("status", "verified_at", "exchange_fill_id")),
    "position_ledger": ("position_id", ("status", "reconciled_at")),
    "ev_ledger": ("fill_ref", ("expected_edge_cc", "realized_pnl_cc")),
}


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _row_level_backup(
    conn: sqlite3.Connection, fill_refs: list[str], *, backups: Path, stamp: str, store: str
) -> JsonDict:
    """Dump the complete PRE-state of every affected row (all columns) to
    ``<stamp>-phantom_rows_backup.json`` and the exact reversing UPDATEs to
    ``<stamp>-restore.sql``. Both are written and flushed BEFORE any write
    to the store; a failure here aborts the apply."""
    dump: dict[str, list[JsonDict]] = {}
    restore: list[str] = ["BEGIN;"]
    for table, (key_col, changed_cols) in _BACKUP_TABLES.items():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not cols:
            dump[table] = []
            continue
        rows: list[JsonDict] = []
        for ref in fill_refs:
            for rec in conn.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE {key_col} = ?", (ref,)
            ):
                row = dict(zip(cols, rec, strict=True))
                rows.append(row)
                sets = [
                    f"{c} = {_sql_literal(row[c])}" for c in changed_cols if c in row
                ]
                if sets:
                    where = f"{key_col} = {_sql_literal(ref)}"
                    if "id" in row:
                        where += f" AND id = {_sql_literal(row['id'])}"
                    restore.append(f"UPDATE {table} SET {', '.join(sets)} WHERE {where};")
        dump[table] = rows
    restore.append("COMMIT;")
    backups.mkdir(parents=True, exist_ok=True)
    json_path = backups / f"{stamp}-phantom_rows_backup.json"
    sql_path = backups / f"{stamp}-restore.sql"
    if json_path.exists() or sql_path.exists():
        raise SystemExit(f"REFUSED: backup target exists: {json_path}")
    json_path.write_text(
        json.dumps(
            {"store": store, "stamp": stamp, "fill_refs": fill_refs, "rows": dump},
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    sql_path.write_text("\n".join(restore) + "\n", encoding="utf-8")
    return {
        "backup": str(json_path),
        "restore_sql": str(sql_path),
        "rows_backed_up": {t: len(r) for t, r in dump.items()},
    }


def apply_void(
    store_path: str,
    phantom_rows: list[JsonDict],
    *,
    migrate: bool,
    backup_dir: Path | None = None,
    liveness_window_s_: float | None = None,
    config_path: Path | None = None,
) -> JsonDict:
    """Mark the given rows phantom across the three ledgers (mirrors
    Store.void_phantom_fill — parity-tested against it), after (1) the
    liveness refusal and (2) a row-level backup under ``backup_dir``
    (default ``data/backups/``). ``liveness_window_s_`` overrides the config-
    derived window (tests); otherwise it derives from ``config_path`` (or the
    default config search) per ``liveness_window_s``."""
    # (1) NEVER touch a live store — before any backup, before any connection.
    window = (
        liveness_window_s_
        if liveness_window_s_ is not None
        else liveness_window_s(config_path or default_config_path())
    )
    evidence = bot_liveness_evidence(store_path, window_s=window)
    if evidence:
        raise SystemExit(
            "REFUSED: the bot may be ALIVE on this store's data_dir — "
            + "; ".join(evidence)
            + " — stop it (and its supervisor) before --apply"
        )
    src = Path(store_path)
    backups = backup_dir if backup_dir is not None else REPO / "data" / "backups"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    fill_refs = [str(row["fill_ref"]) for row in phantom_rows]
    conn = sqlite3.connect(store_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(fills)")]
        if "status" not in cols:
            if not migrate:
                raise SystemExit(
                    "REFUSED: fills has no verification columns; rerun with --migrate "
                    "(adds status/verified_at/exchange_fill_id exactly like Store.open)"
                )
            now_iso = datetime.now(UTC).isoformat()
            conn.execute("ALTER TABLE fills ADD COLUMN status TEXT NOT NULL DEFAULT 'booked'")
            conn.execute("ALTER TABLE fills ADD COLUMN verified_at TEXT")
            conn.execute("ALTER TABLE fills ADD COLUMN exchange_fill_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_status ON fills (status)")
            # The once-only watermark, exactly as Store._ensure_fills_
            # verification_columns stamps it (parity-tested).
            conn.execute(
                "CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO store_meta (key, value)"
                " SELECT 'fills_verification_watermark_id',"
                " CAST(COALESCE(MAX(id), 0) AS TEXT) FROM fills"
            )
            conn.execute(
                "INSERT OR IGNORE INTO store_meta (key, value)"
                " VALUES ('fills_verification_migrated_at', ?)",
                (now_iso,),
            )
            conn.commit()
        # (2) Row-level backup of the PRE-state, flushed before any write.
        backup = _row_level_backup(
            conn, fill_refs, backups=backups, stamp=stamp, store=str(src)
        )
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
    return {**backup, "touched": touched, "liveness_window_s": window}


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
    ap.add_argument(
        "--config",
        help="bot launch YAML the liveness window derives from "
        "(default: config/*.local.yaml, then config/*.yaml)",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--migrate", action="store_true",
        help="--apply: add the verification columns (+ store_meta watermark) if missing",
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
    if not has_status:
        result["apply_requires_migrate"] = True

    print(
        f"store rows {result['n_store_rows']}  tape fills {result['n_tape_fills']}"
        f" (orders {result['n_tape_orders']})  settlements {result['n_settlements']}"
    )
    print("by class:", result["by_class"])
    if not has_status:
        print("store has NO verification columns: --apply requires --migrate")
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
        outcome = apply_void(
            args.store,
            phantom_rows,
            migrate=args.migrate,
            config_path=Path(args.config) if args.config else None,
        )
        result["apply"] = outcome
        print("\nAPPLIED:", outcome)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print("\nwrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
