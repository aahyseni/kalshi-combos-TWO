"""CONFIRM-HALT CLASSIFIER (2026-09-05 build, item A).

What the tape says (EIGHT ``halt_confirm_timeouts`` on 2026-09-05, every one
of the 25 confirm failures ``KalshiApiError('HTTP 400 expired: expired')``):

  boot 2103  04:50:43Z  3rd expired accept → HALT (28 accepts on the boot)
  boot 0052  05:16:40Z / 12:11:20Z / 13:04:20Z → HALT — with 104 clean
             confirms between the 1st and 2nd "consecutive" failure and 1
             between the 2nd and 3rd (116 accepts, 113 confirm audits)
  boot 0905  13:14:10Z / 13:24:13Z / 13:41:14Z → HALT (3 accepts, 3 expired)
  boot 0942  15:10:41Z (fail) 15:15:49Z (ok) 15:22:50Z (fail) 15:40:50Z (fail)
             → HALT: fail, SUCCESS, fail, fail still tripped "3 consecutive"
  boot 1141  15:54:36Z (fail) 15:59:56Z (fail) 16:02:28Z (ok) 16:11:31Z (fail)
             → HALT: fail, fail, SUCCESS, fail still tripped "3 consecutive"
  boot 1213  16:39:47Z (fail) — 1 of 3 when the boot ended
  boot 1407  18:32:03Z / 18:40:00Z / 18:45:25Z → HALT
  boot 1446  18:56:13Z / 18:58:38Z / 18:59:18Z → HALT
  boot 1500  19:32:28Z / 19:34:29Z / 20:00:56Z → HALT (``halt_receipt.json``)

Every failure was the exchange saying the TAKER's accept window lapsed before
our confirm landed (in-handler time 0.5-0.8 s from ``quote_accepted``), and in
every case the reservation reconcile then RELEASED the headroom — no position
on the exchange. Two mechanism defects, both fixed here and pinned below:

  1. ``_confirm_failures`` was cumulative per run, never reset on a successful
     confirm (the 7/31 addendum already measured this) — "consecutive" was a
     label, not a rule.
  2. An exchange-expired accept — a lost auction, not a failure of OURS to
     confirm and never an unknown-committed position — counted like a timeout.

The halt itself is KEPT for genuinely consecutive failures of ours (timeouts,
connection errors, HTTP 5xx, any other refusal), and the unknown-committed
posture (reservation held until the exchange reconcile proves it) is unchanged
for every class — the tests assert that too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import KalshiApiError, RateLimitedError
from combomaker.ops.persistence import Store
from combomaker.rfq.lifecycle import (
    CONFIRM_EXPIRED_BY_EXCHANGE,
    CONFIRM_FAILURE_CONNECTION,
    CONFIRM_FAILURE_OTHER,
    CONFIRM_FAILURE_REFUSED,
    CONFIRM_FAILURE_SERVER,
    CONFIRM_FAILURE_TIMEOUT,
    CONFIRM_HALT_CONSECUTIVE,
    classify_confirm_failure,
    confirm_failure_counts_toward_halt,
)
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import FakeSender, JsonDict, Rig, accepted_msg
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event
from tests.test_reservation_lifecycle import BIG_BANKROLL_CC, _build


def expired() -> KalshiApiError:
    """The exact live shape: ``KalshiApiError('HTTP 400 expired: expired')``."""
    return KalshiApiError(400, "expired", "expired")


class ScriptedSender(FakeSender):
    """One outcome per confirm, in order: ``None`` = success, an exception =
    raise it. Exhausting the script is a test bug (raises IndexError)."""

    def __init__(self) -> None:
        super().__init__()
        self.script: list[BaseException | None] = []

    async def confirm_quote(self, quote_id: str) -> JsonDict:
        outcome = self.script.pop(0)
        if outcome is not None:
            raise outcome
        self.confirmed.append(quote_id)
        return {}


@pytest.fixture()
async def rig(tmp_path: Path) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    r = Rig(h, store)
    sender = ScriptedSender()
    r.sender = sender
    r.lifecycle._sender = sender  # noqa: SLF001 (test seam: scripted outcomes)
    return r


async def _accept_n(rig: Rig, n: int, *, start: int = 0) -> None:
    """Quote + accept ``n`` distinct RFQs; each accept drives one confirm."""
    for i in range(start, start + n):
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"rfq_{i}"))
        quote_id = rig.sender.created[-1]["id"]
        await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))


# ---------------------------------------------------------------- the classes


@pytest.mark.parametrize(
    ("exc", "kind", "counts"),
    [
        (KalshiApiError(400, "expired", "expired"), CONFIRM_EXPIRED_BY_EXCHANGE, False),
        # a different 400 is a refusal of OUR request — counts
        (KalshiApiError(400, "insufficient_balance", "x"), CONFIRM_FAILURE_REFUSED, True),
        # 'expired' with a non-400 status is NOT the exonerated shape
        (KalshiApiError(409, "expired", "expired"), CONFIRM_FAILURE_REFUSED, True),
        (KalshiApiError(401, "unauthorized", "x"), CONFIRM_FAILURE_REFUSED, True),
        (KalshiApiError(404, "not_found", "x"), CONFIRM_FAILURE_REFUSED, True),
        (RateLimitedError(429, "rate_limited", "x"), CONFIRM_FAILURE_REFUSED, True),
        (KalshiApiError(500, "internal", "x"), CONFIRM_FAILURE_SERVER, True),
        (KalshiApiError(503, "unavailable", "x"), CONFIRM_FAILURE_SERVER, True),
        (TimeoutError("confirm timed out"), CONFIRM_FAILURE_TIMEOUT, True),
        (ConnectionResetError("reset"), CONFIRM_FAILURE_CONNECTION, True),
        (OSError("socket"), CONFIRM_FAILURE_CONNECTION, True),
        (RuntimeError("confirm boom"), CONFIRM_FAILURE_OTHER, True),
    ],
)
def test_classifier_names_exactly_one_exonerated_class(
    exc: BaseException, kind: str, counts: bool
) -> None:
    assert classify_confirm_failure(exc) == kind
    assert confirm_failure_counts_toward_halt(kind) is counts


def test_halt_rule_is_three_consecutive() -> None:
    assert CONFIRM_HALT_CONSECUTIVE == 3


# ------------------------------------------- exchange-expired never counts


async def test_exchange_expired_accepts_never_trip_the_halt(rig: Rig) -> None:
    """Today's 0905 boot: three expired accepts in a row halted the bot. Now:
    classified, counted in their own metric, and the halt never fires — even
    for many more than three."""
    # Twice the halt count, inside the fill-velocity governor's 8-per-window
    # (a 9th accept is declined by that governor, not by this classifier).
    n = CONFIRM_HALT_CONSECUTIVE * 2
    rig.sender.script = [expired() for _ in range(n)]
    await _accept_n(rig, n)
    assert not rig.killswitch.halted
    assert rig.metrics.counter("confirm.expired_by_exchange") == n
    assert rig.metrics.counter("confirm.failed") == 0
    # A lost auction books nothing.
    assert rig.exposure.positions == {}


async def test_expired_keeps_the_reservation_held_until_reconcile(
    tmp_path: Path,
) -> None:
    """The unknown-committed posture is NOT weakened by the classification:
    an expired accept still leaves its reservation HELD and flagged
    unconfirmed — the exchange reconcile is the prover, not the error code
    (today it released 15/15; the code never assumes that)."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "r.sqlite3", h.clock)
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle, sender, exposure, reservation = _build(
        h, store, limits=limits, bankroll_cc=BIG_BANKROLL_CC
    )
    scripted = ScriptedSender()
    scripted.script = [expired()]
    lifecycle._sender = scripted  # noqa: SLF001 (test seam)
    await lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = scripted.created[-1]["id"]
    await lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    assert reservation.outstanding_count == 1
    assert reservation.is_unconfirmed(f"fill:{quote_id}") is True
    assert f"fill:{quote_id}" not in exposure.positions
    assert not h.killswitch.halted


# ----------------------------------------------- consecutive means consecutive


async def test_own_failures_separated_by_successes_never_trip(rig: Rig) -> None:
    """The 00:52 boot: failure, 104 successes, failure, 1 success, failure →
    halted. Three failures with ANY success between them must never trip."""
    rig.sender.script = [
        TimeoutError("t1"), None,
        TimeoutError("t2"), None,
        TimeoutError("t3"), None,
        TimeoutError("t4"),
    ]
    await _accept_n(rig, 7)
    assert not rig.killswitch.halted
    assert rig.metrics.counter("confirm.failed") == 4
    assert rig.metrics.counter("confirm.sent") == 3


async def test_a_success_resets_the_counter_exactly(rig: Rig) -> None:
    """Two failures, a success, two failures: not halted (the run is 2).
    One more failure: halted (the run is now 3)."""
    rig.sender.script = [
        TimeoutError("a"), TimeoutError("b"), None, TimeoutError("c"), TimeoutError("d"),
    ]
    await _accept_n(rig, 5)
    assert not rig.killswitch.halted
    rig.sender.script = [TimeoutError("e")]
    await _accept_n(rig, 1, start=5)
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason == ReasonCode.HALT_CONFIRM_TIMEOUTS


async def test_expired_neither_counts_nor_resets(rig: Rig) -> None:
    """An exchange-expired accept is orthogonal to whether WE can confirm: it
    does not add to the run and does not clear it."""
    rig.sender.script = [TimeoutError("a"), expired(), TimeoutError("b"), expired()]
    await _accept_n(rig, 4)
    assert not rig.killswitch.halted  # the run of OUR failures is 2
    rig.sender.script = [TimeoutError("c")]
    await _accept_n(rig, 1, start=4)
    assert rig.killswitch.halted  # ...and now 3, uninterrupted by the expireds
    assert rig.metrics.counter("confirm.expired_by_exchange") == 2
    assert rig.metrics.counter("confirm.failed") == 3


# --------------------------------------------- the halt is KEPT for our failures


@pytest.mark.parametrize(
    "make",
    [
        lambda: TimeoutError("confirm timed out"),
        lambda: ConnectionResetError("connection reset"),
        lambda: KalshiApiError(503, "unavailable", "upstream"),
        lambda: KalshiApiError(401, "unauthorized", "bad key"),
        lambda: RuntimeError("confirm boom"),
    ],
    ids=["timeout", "connection", "http_5xx", "http_4xx_refusal", "other"],
)
async def test_three_genuinely_consecutive_own_failures_still_halt(
    rig: Rig, make: object
) -> None:
    rig.sender.script = [make() for _ in range(CONFIRM_HALT_CONSECUTIVE)]  # type: ignore[operator]
    await _accept_n(rig, CONFIRM_HALT_CONSECUTIVE)
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason == ReasonCode.HALT_CONFIRM_TIMEOUTS
    assert "of ours" in rig.killswitch.halt_event.detail
    assert rig.metrics.counter("confirm.failed") == CONFIRM_HALT_CONSECUTIVE


async def test_two_own_failures_do_not_halt(rig: Rig) -> None:
    rig.sender.script = [TimeoutError("a"), TimeoutError("b")]
    await _accept_n(rig, 2)
    assert not rig.killswitch.halted


# ------------------------------------ the WARNING makes the loop stall visible


async def test_expired_warning_carries_the_accept_to_confirm_latency(rig: Rig) -> None:
    """Item A(2): an exchange-expired accept is logged with the whole in-process
    accept → confirm latency split into its two parts — the DISPATCH delay
    (WS receive stamp → handler entry: the event-loop stall) and the confirm
    round trip — against the exchange's 3.0 s window, so a late-delivered
    accept reads as such on the tape instead of as a failure of ours. Under
    the fake clock every number is exact: 1.2 s in the dispatch queue, 0.3 s
    on the wire."""
    clock = rig.h.clock
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = rig.sender.created[-1]["id"]
    clock.advance(5.0)  # a positive receive stamp needs a clock past 1.2 s

    async def slow_expire(_quote_id: str) -> JsonDict:
        clock.advance(0.3)
        raise expired()

    rig.sender.confirm_quote = slow_expire  # type: ignore[method-assign]
    msg = accepted_msg(quote_id, "yes")
    msg["_ws_recv_mono_ns"] = clock.monotonic_ns() - int(1.2e9)
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_accepted(msg)
    lines = [c for c in cap if c.get("event") == "confirm_expired_by_exchange"]
    assert len(lines) == 1
    line = lines[0]
    assert line["log_level"] == "warning"
    assert line["quote_id"] == quote_id
    assert line["dispatch_delay_ms"] == pytest.approx(1200.0)
    assert line["confirm_rtt_ms"] == pytest.approx(300.0)
    assert line["accept_to_confirm_ms"] == pytest.approx(1500.0)
    assert line["exchange_window_ms"] == pytest.approx(3000.0)
    assert rig.metrics.histogram_max_ms(
        "confirm.expired_by_exchange.accept_to_confirm_ms"
    ) == pytest.approx(1500.0)
    assert rig.metrics.counter("confirm.failed") == 0
    assert not rig.killswitch.halted


async def test_own_failure_error_line_carries_the_same_timings(rig: Rig) -> None:
    """A failure of OURS logs the same split (so a timeout's cause — slow
    dispatch vs slow wire — is readable) plus its class and the run length."""
    clock = rig.h.clock
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = rig.sender.created[-1]["id"]
    clock.advance(5.0)

    async def slow_timeout(_quote_id: str) -> JsonDict:
        clock.advance(2.0)
        raise TimeoutError("confirm timed out")

    rig.sender.confirm_quote = slow_timeout  # type: ignore[method-assign]
    msg = accepted_msg(quote_id, "yes")
    msg["_ws_recv_mono_ns"] = clock.monotonic_ns() - int(0.5e9)
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_accepted(msg)
    lines = [c for c in cap if c.get("event") == "confirm_failed"]
    assert len(lines) == 1
    line = lines[0]
    assert line["log_level"] == "error"
    assert line["kind"] == CONFIRM_FAILURE_TIMEOUT
    assert line["consecutive"] == 1
    assert line["dispatch_delay_ms"] == pytest.approx(500.0)
    assert line["confirm_rtt_ms"] == pytest.approx(2000.0)
    assert line["accept_to_confirm_ms"] == pytest.approx(2500.0)
    assert rig.metrics.counter("confirm.failed") == 1
