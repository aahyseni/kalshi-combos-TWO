"""DERIVED INPUTS — every threshold the gate grades against, and where it came from.

THE RULE (CLAUDE.md, operator 2026-07-22): a number a human has to move is a bug.
So this module contains NO tuned constants. Every quantity below is one of:

  * a MEASURED DISTRIBUTION off the recorded live tape (``data/live_*.log``) —
    the observed API bucket, the largest same-tick quarantine wave that has ever
    actually happened, the launch->first-beat times of every recorded boot, the
    live book's position count;
  * an OBSERVED EXCHANGE FACT the bot itself recorded (``api_tier_observed`` is
    the reply to ``GET /account/limits``);
  * a PROTOCOL FACT imported from the live module that owns it
    (``EXCHANGE_CONFIRM_WINDOW_S``, ``DELETE_QUOTE_TOKEN_COST``) — the same class
    as a tick size;
  * the OPERATOR'S OWN RATIFIED KNOBS, read out of the live config file
    (``supervisor.heartbeat_timeout_s``, ``risk.entity_loss_frac``,
    ``risk.max_open_quotes``) — read, never invented;
  * live BANKROLL, read READ-ONLY out of the production store.

The tape is ~10 GB, so the scan is cached in ``tape_facts.json`` next to this
file, keyed by a manifest of every log's (size, mtime). The cache is a
first-class artifact: it carries its own provenance and is what makes the gate
runnable on a machine that does not have the tape.

READ-ONLY, ALWAYS. Nothing here opens a socket, writes to the store, or touches
a live module.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
# ``VITALS_DATA_DIR`` lets a SCRATCH COPY of the repo (the regression-proof
# harness, which reintroduces historical defects into src/) point at the real
# recorded tape and store instead of duplicating 10 GB. Read-only either way.
DATA = Path(os.environ.get("VITALS_DATA_DIR") or (REPO / "data"))
CACHE = Path(__file__).resolve().parent / "tape_facts.json"

# The markers we mine out of the tape. Chunk-level substring rejection on these
# is what makes a 10 GB scan IO-bound instead of line-parse-bound.
_MARKERS = (
    b"api_tier_observed",
    b"market_quarantined",
    b"quote_app_starting",
    b"book_risk_snapshot",
    b"candidate_gate_",
)


def _log_files() -> list[Path]:
    return sorted(DATA.glob("live_*.log"))


def _manifest(files: list[Path]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for f in files:
        st = f.stat()
        out[f.name] = [st.st_size, round(st.st_mtime, 3)]
    return out


def _ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def scan_tape(files: list[Path]) -> dict[str, Any]:
    """One streaming pass per log. Returns the measured distributions."""
    tier: dict[str, Any] | None = None
    tier_at = ""
    quarantine_by_second: dict[str, int] = {}
    boots: list[dict[str, Any]] = []
    n_positions: list[int] = []
    gate_deadline = 0
    gate_confirm = 0
    gate_attempts_max = 0

    for path in files:
        launched: datetime | None = None
        first_beat: datetime | None = None
        tail = b""
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                buf = tail + chunk
                cut = buf.rfind(b"\n")
                if cut < 0:
                    tail = buf
                    continue
                block, tail = buf[: cut + 1], buf[cut + 1 :]
                if not any(m in block for m in _MARKERS):
                    continue
                for raw in block.split(b"\n"):
                    if not any(m in raw for m in _MARKERS):
                        continue
                    try:
                        rec = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    event = rec.get("event")
                    stamp = str(rec.get("ts", ""))
                    if event == "api_tier_observed" and stamp >= tier_at:
                        tier, tier_at = rec, stamp
                    elif event == "market_quarantined":
                        # Same-tick fan-out. The maintenance tick is sub-second,
                        # so the wall SECOND is the tick bucket on the tape.
                        key = f"{path.name}|{stamp[:19]}"
                        quarantine_by_second[key] = quarantine_by_second.get(key, 0) + 1
                    elif event == "quote_app_starting" and launched is None:
                        launched = _ts(stamp)
                    elif event in ("startup_book_risk_snapshot", "book_risk_snapshot"):
                        # The bot's FIRST heartbeat.beat() runs immediately after
                        # _startup_book_risk_snapshot, so this is the launch->beat
                        # proxy the relighter's latch is graded against.
                        if launched is not None and first_beat is None:
                            first_beat = _ts(stamp)
                        if event == "book_risk_snapshot":
                            n = rec.get("n_positions")
                            if isinstance(n, int):
                                n_positions.append(n)
                    elif event == "candidate_gate_deadline":
                        gate_deadline += 1
                    elif event == "candidate_gate_confirm":
                        gate_confirm += 1
                    if event and event.startswith("candidate_gate_"):
                        attempt = rec.get("attempt")
                        if isinstance(attempt, int):
                            gate_attempts_max = max(gate_attempts_max, attempt + 1)
        if launched is not None and first_beat is not None:
            boots.append(
                {
                    "log": path.name,
                    "launch_to_first_beat_s": round(
                        (first_beat - launched).total_seconds(), 2
                    ),
                }
            )

    waves = sorted(quarantine_by_second.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "provenance": {
            "scanned_logs": len(files),
            "scanned_bytes": sum(f.stat().st_size for f in files),
            "manifest": _manifest(files),
        },
        "observed_tier": tier or {},
        "observed_tier_at": tier_at,
        "max_same_tick_quarantine": waves[0][1] if waves else 0,
        "max_same_tick_quarantine_where": waves[0][0] if waves else "",
        "boots": boots,
        "live_n_positions_last": n_positions[-1] if n_positions else 0,
        "live_n_positions_max": max(n_positions) if n_positions else 0,
        "candidate_gate_deadline_events": gate_deadline,
        "candidate_gate_confirm_events": gate_confirm,
        "candidate_gate_attempts_max": gate_attempts_max,
    }


def tape_facts(*, refresh: bool = False) -> dict[str, Any]:
    """The cached tape scan. Recomputed only when the tape actually changed."""
    files = _log_files()
    cached: dict[str, Any] | None = None
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
        except ValueError:
            cached = None
    if cached is not None and not refresh:
        if not files:
            cached["provenance"]["source"] = "cache (no tape on this machine)"
            return cached
        if cached.get("provenance", {}).get("manifest") == _manifest(files):
            cached["provenance"]["source"] = "cache (tape unchanged)"
            return cached
    if not files:
        raise SystemExit(
            "no recorded tape (data/live_*.log) and no tape_facts.json cache — "
            "the gate cannot derive its thresholds"
        )
    facts = scan_tape(files)
    facts["provenance"]["source"] = "fresh scan"
    CACHE.write_text(json.dumps(facts, indent=2, sort_keys=True), encoding="utf-8")
    return facts


# --------------------------------------------------------------------------- #
# The operator's own knobs, read out of the live config (never invented here).
# --------------------------------------------------------------------------- #


def _live_config_path() -> Path:
    for name in ("prod-live-wc.local.yaml", "prod.yaml", "demo.yaml"):
        p = REPO / "config" / name
        if p.exists():
            return p
    raise SystemExit("no config file under config/ — cannot read the operator knobs")


def live_config() -> tuple[dict[str, Any], Path]:
    import yaml

    path = _live_config_path()
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}, path


def risk_bankroll_cc() -> tuple[int, str]:
    """The live risk-capital denominator, READ-ONLY out of the production store.

    ``daily_ruin_anchors.equity_cc`` is the same equity basis the ruin floor and
    every %-of-bankroll cap are denominated in. If several stores exist the most
    recently anchored one wins.
    """
    best: tuple[str, int, str] | None = None
    for db in sorted(DATA.glob("combomaker*.sqlite3")):
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute(
                "select utc_date, equity_cc from daily_ruin_anchors "
                "order by utc_date desc limit 1"
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row and row[1]:
            if best is None or row[0] > best[0]:
                best = (row[0], int(row[1]), db.name)
    if best is None:
        raise SystemExit(
            "no equity anchor in any store under data/ — the gate refuses to "
            "invent a bankroll"
        )
    return best[1], f"{best[2]}:daily_ruin_anchors[{best[0]}]"


def live_open_positions() -> tuple[list[dict[str, Any]], str]:
    """The REAL open book, READ-ONLY out of ``position_ledger``. This is the
    fixture the confirm-window check is measured against — the live leg shape
    (and therefore the live O(T^2) ticker-pair count), not a synthetic one."""
    best: tuple[int, list[dict[str, Any]], str] | None = None
    for db in sorted(DATA.glob("combomaker*.sqlite3")):
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        rows: list[dict[str, Any]] = []
        try:
            for pid, side, contracts, price, legs_json in conn.execute(
                "select position_id, our_side, contracts_centi, entry_price_cc, "
                "legs_json from position_ledger where status = 'open'"
            ):
                try:
                    legs = json.loads(legs_json)
                except ValueError:
                    continue
                rows.append(
                    {
                        "position_id": pid,
                        "our_side": side,
                        "contracts_centi": int(contracts),
                        "entry_price_cc": int(price),
                        "legs": legs,
                    }
                )
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        if rows and (best is None or len(rows) > best[0]):
            best = (len(rows), rows, f"{db.name}:position_ledger[status=open]")
    if best is None:
        raise SystemExit("no open positions in any store — cannot measure the live book")
    return best[1], best[2]


@dataclass(frozen=True)
class Derived:
    """Everything the checks grade against, with its provenance string."""

    tape: dict[str, Any]
    config: dict[str, Any]
    config_path: Path
    bankroll_cc: int
    bankroll_source: str

    # -- exchange-observed ------------------------------------------------- #
    @property
    def write_refill_per_s(self) -> int:
        return int(self.tape["observed_tier"]["write_refill_per_s"])

    @property
    def write_capacity(self) -> int:
        return int(self.tape["observed_tier"]["write_capacity"])

    @property
    def usage_tier(self) -> str:
        return str(self.tape["observed_tier"].get("usage_tier", "?"))

    # -- measured live shape ----------------------------------------------- #
    @property
    def fanout_n(self) -> int:
        """The largest same-tick quarantine wave that has EVER happened."""
        return int(self.tape["max_same_tick_quarantine"])

    @property
    def boot_delays_s(self) -> list[float]:
        return [float(b["launch_to_first_beat_s"]) for b in self.tape["boots"]]

    @property
    def live_n_positions(self) -> int:
        return int(self.tape["live_n_positions_max"])

    @property
    def gate_attempts(self) -> int:
        return max(1, int(self.tape["candidate_gate_attempts_max"]))

    # -- operator knobs, read ---------------------------------------------- #
    def knob(self, section: str, name: str) -> Any:
        """The knob production would see: the YAML section parsed by the SHIPPED
        pydantic model, so an unset key resolves to the live code default rather
        than to anything this gate invents."""
        from combomaker.ops import config as cfg_mod

        model = {"supervisor": cfg_mod.SupervisorConfig, "risk": cfg_mod.RiskConfig}[
            section
        ]
        raw = self.config.get(section) or {}
        known = {k: v for k, v in raw.items() if k in model.model_fields}
        return getattr(model(**known), name)

    @property
    def heartbeat_timeout_s(self) -> float:
        return float(self.knob("supervisor", "heartbeat_timeout_s"))

    @property
    def max_open_quotes(self) -> int:
        return int(self.knob("risk", "max_open_quotes"))

    @property
    def entity_loss_frac(self) -> str:
        return str(self.knob("risk", "entity_loss_frac"))

    @property
    def write_budget_capacity(self) -> int:
        return int(self.knob("supervisor", "write_budget_capacity"))

    @property
    def write_budget_refill_s(self) -> float:
        return float(self.knob("supervisor", "write_budget_refill_s"))

    def lines(self) -> list[str]:
        prov = self.tape.get("provenance", {})
        gb = prov.get("scanned_bytes", 0) / 1e9
        return [
            f"tape           : {prov.get('scanned_logs', 0)} logs, {gb:.2f} GB "
            f"({prov.get('source', '?')})",
            f"observed tier  : {self.usage_tier} — write {self.write_capacity} cap / "
            f"{self.write_refill_per_s} refill per s   [GET /account/limits, recorded "
            f"{self.tape.get('observed_tier_at', '?')}]",
            f"fan-out N      : {self.fanout_n} markets quarantined in one tick   "
            f"[max ever, {self.tape.get('max_same_tick_quarantine_where', '?')}]",
            f"boots measured : {len(self.boot_delays_s)} boots, launch->first beat "
            f"{min(self.boot_delays_s):.1f}s - {max(self.boot_delays_s):.1f}s",
            f"live book      : n_positions max {self.live_n_positions} "
            f"(last {self.tape.get('live_n_positions_last')})",
            f"bankroll       : {self.bankroll_cc / 10_000:,.2f} USD   [{self.bankroll_source}]",
            f"operator knobs : {self.config_path.name} — heartbeat_timeout_s="
            f"{self.heartbeat_timeout_s}, max_open_quotes={self.max_open_quotes}, "
            f"entity_loss_frac={self.entity_loss_frac}, write_budget="
            f"{self.write_budget_capacity}tok/{self.write_budget_refill_s}s",
        ]


def derive(*, refresh_tape: bool = False) -> Derived:
    tape = tape_facts(refresh=refresh_tape)
    if not tape.get("observed_tier"):
        raise SystemExit("no api_tier_observed on the tape — cannot derive the rate ceiling")
    cfg, path = live_config()
    bankroll, source = risk_bankroll_cc()
    return Derived(
        tape=tape,
        config=cfg,
        config_path=path,
        bankroll_cc=bankroll,
        bankroll_source=source,
    )
