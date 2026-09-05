"""EXCHANGE-EXPIRED ACCEPT RATE alarm — the derived replacement for the halt
class the 2026-09-05 confirm-halt classifier removed (review should-fix #5).

Why
---
``halt_confirm_timeouts`` used to fire on three ``HTTP 400 expired`` confirms
(the taker's accept window lapsed before our confirm landed). The classifier
exonerates that class per event — 25/25 failures on 2026-09-05 were exactly
this, every reservation was released by the reconcile, no position ever
resulted. But the RATE of expired accepts is still a fact about US: 2.6 %
overnight (3/116) against ~70 % from 09:05 ET, when the process was dropping
its own subscription (``Subscription buffer overflow``) and the accept
reached the handler late. With the halt gone, the only trace of that
systemic, own-side latency failure would be per-event WARNINGs. This module
judges the boot's expired SHARE against the pooled measured rate of the
retained boots — a binomial tail at the policy z ladder's daily rung — and
raises ``confirm_expired_rate_anomalous`` (WARNING + metric). Whether it
HALTS is the operator ruling the report lists as owed; nothing here halts.

Zero hand numbers
-----------------
* the baseline is the pooled (expired, confirmed) counts of every retained
  prior boot (``ExpiredRateTape``, retention = the oldest ``live_*.log`` on
  disk — the gap tape's rule), smoothed with the Jeffreys prior
  (``(K + 1/2) / (N + 1)``: the standard non-informative Beta(1/2, 1/2), so a
  baseline of 0/116 does not flag the first expired accept as impossible);
* ``z`` is the policy ladder's DAILY rung (daily 3 / weekly 4 / KILL 5): a
  boot is an intra-day unit and this is an alarm, not a kill;
* the tail is the EXACT binomial upper tail P(X >= k | n, p) (log-space via
  ``lgamma``; n is the boot's accepts, hundreds at most);
* no retained boot => UNJUDGED (nothing to compare against — never a verdict
  from an empty baseline).

Caveat, disclosed: the baseline pools every retained boot, so boots that ran
at the anomalous rate raise the baseline once they are folded in — the alarm
desensitises toward whatever becomes normal. The tape row per boot in
``confirm_expired_tape.json`` keeps the per-boot rates readable regardless.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from combomaker.ops.logging import get_logger
from combomaker.risk.heartbeat import _atomic_write
from combomaker.risk.stall_wall import normal_upper_tail_p

log = get_logger(__name__)

# The policy z ladder's DAILY rung (daily 3, weekly 4, KILL 5). A per-boot
# alarm is judged at the intra-day rung; the KILL rung is a halt's, and this
# never halts.
EXPIRED_RATE_ALARM_Z = 3.0
EXPIRED_TAPE_FILENAME = "confirm_expired_tape.json"
_SCHEMA_VERSION = 1


def expired_tape_path(data_dir: Path) -> Path:
    return data_dir / EXPIRED_TAPE_FILENAME


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    """log P(X = k) for X ~ Binomial(n, p), 0 < p < 1."""
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def binomial_upper_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact, summed in log space so n in
    the hundreds cannot overflow ``math.comb``. Degenerate p handled
    directly."""
    if n < 0 or k < 0:
        raise ValueError(f"k and n must be >= 0, got k={k} n={n}")
    if k == 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(_log_binom_pmf(i, n, p))
    return min(1.0, total)


def jeffreys_rate(expired: int, confirmed: int) -> float:
    """The Beta(1/2, 1/2) posterior mean of the expired share."""
    return (expired + 0.5) / (expired + confirmed + 1.0)


@dataclass(frozen=True, slots=True)
class ExpiredRateVerdict:
    """One judgement of this boot's expired share against the baseline."""

    boot_expired: int
    boot_confirmed: int
    baseline_expired: int
    baseline_confirmed: int
    baseline_boots: int
    baseline_rate: float  # Jeffreys-smoothed
    tail_p: float  # P(X >= boot_expired | n, baseline_rate)
    alarm_p: float  # 1 - Phi(z): the tail that flags
    z: float

    @property
    def boot_n(self) -> int:
        return self.boot_expired + self.boot_confirmed

    @property
    def boot_rate(self) -> float:
        return self.boot_expired / self.boot_n if self.boot_n else 0.0

    @property
    def anomalous(self) -> bool:
        return self.tail_p < self.alarm_p

    def as_log(self) -> dict[str, object]:
        return {
            "boot_expired": self.boot_expired,
            "boot_confirmed": self.boot_confirmed,
            "boot_rate": round(self.boot_rate, 4),
            "baseline_expired": self.baseline_expired,
            "baseline_confirmed": self.baseline_confirmed,
            "baseline_boots": self.baseline_boots,
            "baseline_rate": round(self.baseline_rate, 4),
            "tail_p": self.tail_p,
            "alarm_p": self.alarm_p,
            "z": self.z,
            "anomalous": self.anomalous,
        }


def judge_expired_rate(
    *,
    boot_expired: int,
    boot_confirmed: int,
    baseline: tuple[int, int, int] | None,
    z: float = EXPIRED_RATE_ALARM_Z,
) -> ExpiredRateVerdict | None:
    """Judge this boot's expired share against the pooled baseline
    ``(expired, confirmed, boots)`` of the RETAINED PRIOR boots. None when
    there is no baseline (no prior boot with any accept) or this boot has no
    accepts yet — unjudged, never a verdict."""
    if baseline is None:
        return None
    base_expired, base_confirmed, base_boots = baseline
    if base_expired < 0 or base_confirmed < 0 or base_expired + base_confirmed == 0:
        return None
    n = boot_expired + boot_confirmed
    if n <= 0 or boot_expired < 0 or boot_confirmed < 0:
        return None
    rate = jeffreys_rate(base_expired, base_confirmed)
    tail = binomial_upper_tail(boot_expired, n, rate)
    alarm_p = 1.0 - normal_upper_tail_p(z)
    return ExpiredRateVerdict(
        boot_expired=boot_expired,
        boot_confirmed=boot_confirmed,
        baseline_expired=base_expired,
        baseline_confirmed=base_confirmed,
        baseline_boots=base_boots,
        baseline_rate=rate,
        tail_p=tail,
        alarm_p=alarm_p,
        z=z,
    )


class ExpiredRateTape:
    """Per-boot (expired, confirmed) counts on disk — the gap tape's shape:
    load -> fold this boot -> prune to the log retention -> pool the OTHER
    boots -> save. Every failure degrades to "no baseline" (unjudged), never
    a raise toward the confirm path."""

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
            log.warning("expired_tape_unreadable", path=str(self._path))
            return
        boots = payload.get("boots") if isinstance(payload, dict) else None
        if not isinstance(boots, dict):
            return
        for key, rec in boots.items():
            if not isinstance(rec, dict):
                continue
            try:
                expired = int(rec["expired"])
                confirmed = int(rec["confirmed"])
            except (KeyError, TypeError, ValueError):
                continue
            if expired < 0 or confirmed < 0:
                continue
            started = rec.get("started_at_ts")
            self.boots[str(key)] = {
                "started_at_ts": float(started) if isinstance(started, int | float) else None,
                "expired": expired,
                "confirmed": confirmed,
            }

    def fold(
        self, boot_key: str, *, expired: int, confirmed: int, started_at_ts: float
    ) -> None:
        """Replace this boot's row with its CURRENT counters (cumulative for
        the boot — replace, never add)."""
        self.boots[boot_key] = {
            "started_at_ts": started_at_ts,
            "expired": int(expired),
            "confirmed": int(confirmed),
        }

    def prune(self, *, retain_since_ts: float | None) -> int:
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

    def baseline_excluding(self, boot_key: str) -> tuple[int, int, int] | None:
        """Pooled ``(expired, confirmed, boots)`` over every retained boot
        EXCEPT ``boot_key`` (the boot being judged never sits in its own
        baseline). None when no other boot has an accept."""
        expired = confirmed = boots = 0
        for key, rec in self.boots.items():
            if key == boot_key:
                continue
            e = int(rec.get("expired", 0))  # type: ignore[call-overload]
            c = int(rec.get("confirmed", 0))  # type: ignore[call-overload]
            if e + c == 0:
                continue
            expired += e
            confirmed += c
            boots += 1
        if expired + confirmed == 0:
            return None
        return (expired, confirmed, boots)

    def save(self) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "boots": {
                key: {
                    "started_at_ts": rec.get("started_at_ts"),
                    "expired": rec.get("expired", 0),
                    "confirmed": rec.get("confirmed", 0),
                }
                for key, rec in self.boots.items()
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path, json.dumps(payload))
        except OSError as exc:  # pragma: no cover - disk failure path
            log.warning("expired_tape_write_failed", path=str(self._path), error=repr(exc))


def refresh_expired_baseline(
    *,
    tape_path: Path,
    boot_key: str,
    boot_started_at_ts: float,
    boot_expired: int,
    boot_confirmed: int,
    retain_since_ts: float | None,
) -> tuple[int, int, int] | None:
    """One cycle: load, fold this boot's counters, prune, save, and return the
    pooled baseline of the OTHER retained boots. Synchronous file I/O — run
    it off the event loop (``asyncio.to_thread``), like the gap tape."""
    tape = ExpiredRateTape(tape_path)
    tape.load()
    tape.fold(
        boot_key,
        expired=boot_expired,
        confirmed=boot_confirmed,
        started_at_ts=boot_started_at_ts,
    )
    tape.prune(retain_since_ts=retain_since_ts)
    baseline = tape.baseline_excluding(boot_key)
    tape.save()
    return baseline


__all__ = [
    "EXPIRED_RATE_ALARM_Z",
    "EXPIRED_TAPE_FILENAME",
    "ExpiredRateTape",
    "ExpiredRateVerdict",
    "binomial_upper_tail",
    "expired_tape_path",
    "jeffreys_rate",
    "judge_expired_rate",
    "refresh_expired_baseline",
]
