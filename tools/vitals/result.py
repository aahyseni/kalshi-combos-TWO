"""The result shape. A wall of green dots is exactly what failed us, so every
check must surrender its MEASURED number, the DERIVED bound it was graded
against, and what it costs in MONEY when it goes red."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

FAST = "fast"
PRESHIP = "pre-ship"


@dataclass(frozen=True)
class Vital:
    key: str          # V1..V9
    incident: str     # the historical regression this exists because of
    name: str         # what it asserts
    degraded: str     # the DEGRADED state it constructs (the discriminating variable)
    money: str        # what it costs in dollars when it fails
    tier: str = FAST


@dataclass
class Result:
    vital: Vital
    passed: bool
    measured: str
    bound: str
    detail: str = ""
    seconds: float = 0.0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        return "PASS" if self.passed else "FAIL"


class Probe:
    """Handed to every check: accumulates the measured evidence, then grades."""

    def __init__(self, vital: Vital) -> None:
        self.vital = vital
        self.notes: list[str] = []

    def note(self, line: str) -> None:
        self.notes.append(line)

    def grade(self, ok: bool, *, measured: str, bound: str, detail: str = "") -> Result:
        return Result(
            vital=self.vital,
            passed=bool(ok),
            measured=measured,
            bound=bound,
            detail=detail,
            notes=list(self.notes),
        )


def run_check(vital: Vital, fn: Callable[[Probe, Any], Result], derived: Any) -> Result:
    probe = Probe(vital)
    started = time.perf_counter()
    try:
        result = fn(probe, derived)
    except BaseException as exc:  # noqa: BLE001 — a broken check is a RED check
        result = Result(
            vital=vital,
            passed=False,
            measured="check raised",
            bound="-",
            error=f"{type(exc).__name__}: {exc}",
            detail=traceback.format_exc(limit=6),
            notes=list(probe.notes),
        )
    result.seconds = time.perf_counter() - started
    return result
