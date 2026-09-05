"""DERIVED maintenance stall wall — the supervisor's kill bound for the
maintenance loop, measured from the loop's own recorded progress instead of
hand-set (2026-09-05 build ``confirm-halt-and-derived-wall``, item 6).

What was hand-set
-----------------
``ProgressLedger.register`` derives every loop's stall bound as
``wedge_timeout_s + interval_s`` — for the maintenance loop that is
``supervisor.heartbeat_timeout_s`` (30 → 60 on 2026-08-18, a hand edit made
because "passes ran 29-31 s at 178 GB") plus its 0.5 s cadence: 60.5 s. Nothing
ever MEASURED the pass distribution that number was supposed to clear — the
ledger publishes a single overwritten snapshot, so no tape of completed passes
existed to derive from. This module adds the tape and the derivation.

The rule (the hang watchdog's rule, in the bot)
-----------------------------------------------
``tools/ops/hang_watchdog.py`` derives its whole-process stall threshold as
``max(_MARGIN * max_healthy_gap, floor)`` from the recorded log tape. This
module applies the SAME rule to the maintenance loop's inter-mark gaps:

    wall_s           = max(floor_s, STALL_WALL_MARGIN * Q_p(completed gaps))
    sub_step_bound_s = wall_s / STALL_WALL_MARGIN

* A COMPLETED gap is the time between two consecutive progress marks of the
  loop. By construction every recorded gap is HEALTHY: a hang never completes
  a gap (no second mark arrives), so the recorded distribution can only ever
  contain gaps the loop recovered from. There is no "healthy" filter to get
  wrong.
* ``Q_p`` is the upper quantile at the policy z ladder's KILL anchor
  (``WALL_QUANTILE_Z = 5`` ⇒ p = Φ(5)); for any sample smaller than ~3.5M gaps
  that is the sample maximum, i.e. exactly the watchdog's ``max_healthy_gap``.
  Expressing it as a quantile means the rule keeps working when the pooled
  tape outgrows that — the wall then tracks the 5σ edge, not one outlier.
* ``STALL_WALL_MARGIN`` is the watchdog's ``_MARGIN`` (2.0): a REAL hang still
  dies within one margin of the longest pass the loop has ever completed.
* ``floor_s`` is TODAY's derived bound (``wedge_timeout_s + interval_s``,
  60.5 s live). The wall can only LOOSEN from it by measurement — the
  floor is a policy anchor already in the config, not a new number.
* ``sub_step_bound_s`` bounds every direct store await on the maintenance
  path (``QuoteLifecycle._bounded_store``): a single sub-step that outlives
  the longest gap the loop has EVER completed is outside the healthy
  distribution by definition, so it yields with a logged timeout and the loop
  advances — with exactly one margin to spare before the supervisor's wall.

The feedback path, and why loosening is GATED (review fix 2026-09-05)
---------------------------------------------------------------------
The bound is ``wall / MARGIN`` and the wall is ``MARGIN × Q(gaps)``, so a
gap that is long BECAUSE a bounded store await ran to its bound feeds the
bound back into the wall: any completed gap g in (wall/MARGIN, wall] makes
the next derivation ``MARGIN × g`` — with MARGIN = 2 the wall DOUBLES at the
next refresh, the bound doubles with it, and the next such gap can be twice
as long. Under exactly the degraded state this module targets (a saturated
store) the wall would ratchet, and the tape's retention (the oldest
``live_*.log`` on disk — 45 days as of 2026-09-05) keeps a loosened wall for
weeks. Two defences, both in code:

1. TAINT. A gap that contained a ``store.await_timeout`` is not a
   measurement of the healthy loop — it "completed" only because the await
   gave up. ``QuoteLifecycle._bounded_store`` taints the ledger's current gap
   on every timeout (``ProgressLedger.taint``) and the next mark skips it.
   This closes the timeout branch of the loop exactly; it does NOT cover a
   sub-step whose several sequential bounded ops each finish just UNDER the
   bound (k ops ⇒ a completed gap up to k × bound with no timeout at all).
2. MODE. ``supervisor.stall_wall_derived`` — ``"shadow"`` (DEFAULT): the
   derivation runs and is logged (``stall_wall_derivation`` carries the
   would-be ``wall_s``) but the bound APPLIED to the ledger and the store
   awaits stays at the floor; ``"on"``: the derived wall is applied. Today's
   measurement (max completed gap 3.544 s vs a 60.5 s floor) means the
   loosening branch can only ever act under degradation, so whether the wall
   may loosen at all is an OPERATOR RULING, owed before ``"on"`` — the
   ``open_quote_capacity_derived`` shadow/on precedent.

The tape
--------
``GapTape`` persists one bucketed histogram per boot to
``<data_dir>/maintenance_gap_tape.json`` (atomic write, same helper as the
heartbeat). Retention is the operator's EXISTING log retention: boots older
than the oldest ``live_*.log`` still on disk are pruned — no new number.
(``oldest_live_log_mtime`` reads that log's mtime — its END, not its start —
so the boot that owns the oldest log is pruned one boot early: strictly
tighter, harmless.) ``derive_stall_wall`` pools the retained boots with the
current one. ``refresh_stall_wall`` is pure file I/O + arithmetic on a COPY
of the histogram; the caller (quote_app) runs it off the event loop.

Blast radius: the supervisor's maintenance-loop bound and the maintenance
loop's own store awaits. Pricing and quoting read nothing from here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from combomaker.ops.logging import get_logger
from combomaker.risk.heartbeat import _atomic_write

log = get_logger(__name__)

# The hang watchdog's ``_MARGIN`` — the SAME rule, pinned by a test that reads
# the tool's constant. A real hang dies within one margin of the longest gap
# the loop has ever completed.
STALL_WALL_MARGIN = 2.0
# The policy z ladder's KILL anchor (KILL 12% = 5σ; daily 3, weekly 4, KILL 5).
# The wall IS a kill, so its quantile sits at the ladder's kill rung.
WALL_QUANTILE_Z = 5.0

GAP_TAPE_FILENAME = "maintenance_gap_tape.json"
_SCHEMA_VERSION = 1


def gap_tape_path(data_dir: Path) -> Path:
    return data_dir / GAP_TAPE_FILENAME


def normal_upper_tail_p(z: float) -> float:
    """Φ(z) — the cumulative probability the quantile sits at."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(slots=True)
class GapHistogram:
    """Bucketed histogram of completed inter-mark gaps (seconds).

    Bucket width is the loop's OWN cadence (``bucket_s``), so a gap of one
    tick lands in bucket 1 and the quantile is resolved to the tick — the same
    granularity the supervisor polls at. Sparse: only buckets that were hit
    are stored, so a 60 s tail costs a handful of keys, not an array.
    """

    bucket_s: float
    counts: dict[int, int]
    n: int = 0
    max_s: float = 0.0
    sum_s: float = 0.0

    @classmethod
    def empty(cls, bucket_s: float) -> GapHistogram:
        if not (bucket_s > 0.0) or math.isinf(bucket_s):
            raise ValueError(f"bucket_s must be > 0 and finite, got {bucket_s}")
        return cls(bucket_s=bucket_s, counts={})

    def observe(self, gap_s: float) -> None:
        """O(1): one floor-division and one dict increment — safe inside
        ``ProgressLedger.mark`` on the maintenance loop."""
        if not (gap_s >= 0.0) or math.isinf(gap_s):
            return  # a clock step backwards / NaN is not a gap
        idx = int(gap_s // self.bucket_s)
        self.counts[idx] = self.counts.get(idx, 0) + 1
        self.n += 1
        self.sum_s += gap_s
        if gap_s > self.max_s:
            self.max_s = gap_s

    def merge(self, other: GapHistogram) -> None:
        if other.bucket_s != self.bucket_s:
            raise ValueError(
                f"bucket mismatch: {self.bucket_s} vs {other.bucket_s}"
            )
        for idx, c in other.counts.items():
            self.counts[idx] = self.counts.get(idx, 0) + c
        self.n += other.n
        self.sum_s += other.sum_s
        if other.max_s > self.max_s:
            self.max_s = other.max_s

    def quantile(self, p: float) -> float:
        """Upper edge of the bucket holding the p-quantile, capped at the
        observed maximum (a quantile is never above anything seen). 0.0 on an
        empty histogram."""
        if not 0.0 < p <= 1.0:
            raise ValueError(f"quantile out of range: {p}")
        if self.n == 0:
            return 0.0
        target = p * self.n
        seen = 0
        for idx in sorted(self.counts):
            seen += self.counts[idx]
            if seen >= target:
                return min((idx + 1) * self.bucket_s, self.max_s)
        return self.max_s

    def copy(self) -> GapHistogram:
        return GapHistogram(
            bucket_s=self.bucket_s,
            counts=dict(self.counts),
            n=self.n,
            max_s=self.max_s,
            sum_s=self.sum_s,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "bucket_s": self.bucket_s,
            "n": self.n,
            "max_s": self.max_s,
            "sum_s": self.sum_s,
            "counts": {str(k): v for k, v in sorted(self.counts.items())},
        }

    @classmethod
    def from_json(cls, payload: object) -> GapHistogram | None:
        """None on any malformed payload — a corrupt row is dropped from the
        pool, never allowed to raise into the derivation."""
        if not isinstance(payload, dict):
            return None
        try:
            bucket_s = float(payload["bucket_s"])
            hist = cls.empty(bucket_s)
            raw_counts = payload.get("counts", {})
            if not isinstance(raw_counts, dict):
                return None
            for k, v in raw_counts.items():
                idx = int(k)
                c = int(v)
                if idx < 0 or c < 0:
                    return None
                if c:
                    hist.counts[idx] = c
            hist.n = int(payload.get("n", sum(hist.counts.values())))
            hist.max_s = float(payload.get("max_s", 0.0))
            hist.sum_s = float(payload.get("sum_s", 0.0))
        except (KeyError, TypeError, ValueError):
            return None
        if hist.n != sum(hist.counts.values()) or hist.max_s < 0.0:
            return None
        return hist


@dataclass(frozen=True, slots=True)
class StallWallDerivation:
    """One derivation — everything the log line and the report need."""

    wall_s: float
    floor_s: float
    margin: float
    z: float
    quantile_p: float
    quantile_s: float
    max_gap_s: float
    n_gaps: int
    boots_pooled: int
    source: str  # "floor" | "measured"

    @property
    def sub_step_bound_s(self) -> float:
        return self.wall_s / self.margin

    def as_log(self) -> dict[str, object]:
        return {
            "wall_s": round(self.wall_s, 3),
            "floor_s": round(self.floor_s, 3),
            "margin": self.margin,
            "z": self.z,
            "quantile_p": self.quantile_p,
            "quantile_s": round(self.quantile_s, 3),
            "max_gap_s": round(self.max_gap_s, 3),
            "n_gaps": self.n_gaps,
            "boots_pooled": self.boots_pooled,
            "sub_step_bound_s": round(self.sub_step_bound_s, 3),
            "source": self.source,
        }


def derive_stall_wall(
    pooled: GapHistogram | None,
    *,
    floor_s: float,
    boots_pooled: int = 0,
    margin: float = STALL_WALL_MARGIN,
    z: float = WALL_QUANTILE_Z,
) -> StallWallDerivation:
    """``max(floor, margin * Q_Φ(z)(completed gaps))`` — see the module doc.

    ``floor_s`` must be > 0 (it is today's derived bound, never absent);
    ``margin`` must be >= 1 (a margin below one would let the wall sit INSIDE
    the healthy distribution — the exact defect this module removes)."""
    if not (floor_s > 0.0) or math.isinf(floor_s):
        raise ValueError(f"floor_s must be > 0 and finite, got {floor_s}")
    if not (margin >= 1.0) or math.isinf(margin):
        raise ValueError(f"margin must be >= 1 and finite, got {margin}")
    p = normal_upper_tail_p(z)
    if pooled is None or pooled.n == 0:
        return StallWallDerivation(
            wall_s=floor_s,
            floor_s=floor_s,
            margin=margin,
            z=z,
            quantile_p=p,
            quantile_s=0.0,
            max_gap_s=0.0,
            n_gaps=0,
            boots_pooled=boots_pooled,
            source="floor",
        )
    q = pooled.quantile(p)
    measured = margin * q
    wall = max(floor_s, measured)
    return StallWallDerivation(
        wall_s=wall,
        floor_s=floor_s,
        margin=margin,
        z=z,
        quantile_p=p,
        quantile_s=q,
        max_gap_s=pooled.max_s,
        n_gaps=pooled.n,
        boots_pooled=boots_pooled,
        source="measured" if measured > floor_s else "floor",
    )


def oldest_live_log_mtime(data_dir: Path) -> float | None:
    """The operator's existing log retention, as a wall timestamp: boots whose
    log has already been rotated away are pruned from the tape. None when no
    ``live_*.log`` exists (tests, fresh installs) ⇒ keep everything. The
    mtime is the log's LAST write (its end), so the boot that owns the oldest
    log is pruned one boot early — tighter, never looser (should-fix #6).
    Synchronous file I/O: call it off the event loop."""
    oldest: float | None = None
    try:
        for p in data_dir.glob("live_*.log"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if oldest is None or m < oldest:
                oldest = m
    except OSError:
        return None
    return oldest


class GapTape:
    """Per-boot gap histograms on disk. Load → fold this boot → prune → pool →
    save. Every failure degrades to "no tape" (the floor), never to a raise
    into the maintenance loop."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.boots: dict[str, dict[str, object]] = {}

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        self.boots = {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            log.warning("gap_tape_unreadable", path=str(self._path))
            return
        boots = payload.get("boots") if isinstance(payload, dict) else None
        if not isinstance(boots, dict):
            return
        for key, rec in boots.items():
            if not isinstance(rec, dict):
                continue
            hist = GapHistogram.from_json(rec.get("hist"))
            if hist is None:
                continue
            started = rec.get("started_at_ts")
            self.boots[str(key)] = {
                "started_at_ts": float(started) if isinstance(started, int | float) else None,
                "hist": hist,
            }

    def fold(self, boot_key: str, hist: GapHistogram, *, started_at_ts: float) -> None:
        """Replace this boot's record with its CURRENT histogram (the in-memory
        histogram is cumulative for the boot, so replace — never add)."""
        self.boots[boot_key] = {"started_at_ts": started_at_ts, "hist": hist.copy()}

    def prune(self, *, retain_since_ts: float | None) -> int:
        """Drop boots that started before the retention horizon. A record with
        no start stamp is kept (never prune on missing evidence)."""
        if retain_since_ts is None:
            return 0
        doomed = [
            k
            for k, rec in self.boots.items()
            if isinstance(rec.get("started_at_ts"), float)
            and float(rec["started_at_ts"]) < retain_since_ts  # type: ignore[arg-type]
        ]
        for k in doomed:
            del self.boots[k]
        return len(doomed)

    def pooled(self, bucket_s: float) -> GapHistogram:
        """All retained boots merged. Boots recorded at a different bucket
        width (a cadence change) are skipped, not mis-binned."""
        pooled = GapHistogram.empty(bucket_s)
        for rec in self.boots.values():
            hist = rec.get("hist")
            if isinstance(hist, GapHistogram) and hist.bucket_s == bucket_s:
                pooled.merge(hist)
        return pooled

    def save(self) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "boots": {
                key: {
                    "started_at_ts": rec.get("started_at_ts"),
                    "hist": rec["hist"].to_json(),  # type: ignore[attr-defined]
                }
                for key, rec in self.boots.items()
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path, json.dumps(payload))
        except OSError as exc:  # pragma: no cover - disk failure path
            log.warning("gap_tape_write_failed", path=str(self._path), error=repr(exc))


def refresh_stall_wall(
    *,
    tape_path: Path,
    data_dir: Path,
    boot_key: str,
    boot_started_at_ts: float,
    this_boot: GapHistogram | None,
    bucket_s: float,
    floor_s: float,
) -> StallWallDerivation:
    """One derivation cycle: load the tape, fold this boot's histogram, prune
    to the log retention, pool, derive, save. Pure function of its inputs plus
    the tape file; the caller applies the result to the ledger and logs it.
    Synchronous file I/O (glob + stat of every ``live_*.log``, a JSON read and
    an atomic write on the store's disk): pass a COPY of the live histogram
    and run this in a worker thread (``asyncio.to_thread``), never on the
    event loop (review fix 2026-09-05, should-fix #1)."""
    tape = GapTape(tape_path)
    tape.load()
    if this_boot is not None and this_boot.n > 0:
        tape.fold(boot_key, this_boot, started_at_ts=boot_started_at_ts)
    tape.prune(retain_since_ts=oldest_live_log_mtime(data_dir))
    pooled = tape.pooled(bucket_s)
    derivation = derive_stall_wall(
        pooled, floor_s=floor_s, boots_pooled=len(tape.boots)
    )
    tape.save()
    return derivation


__all__ = [
    "GAP_TAPE_FILENAME",
    "STALL_WALL_MARGIN",
    "WALL_QUANTILE_Z",
    "GapHistogram",
    "GapTape",
    "StallWallDerivation",
    "derive_stall_wall",
    "gap_tape_path",
    "normal_upper_tail_p",
    "oldest_live_log_mtime",
    "refresh_stall_wall",
]
