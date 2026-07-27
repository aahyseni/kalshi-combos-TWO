"""ONE WRITE PATH, ONE PROVER, AND THE PROVER IS A READ (2026-07-26).

The adversarial gate proved three defects in the first B3 cut with PoCs. These
tests are the same PoCs, pointed at the shipped fix:

  1. ``cancel_all`` dropped UNRESOLVED ids from the mirror on the premise
     "terminal path, there is no next tick". That premise is FALSE for 5 of its
     7 call sites, so a 429 storm on a feed resync / channel loss / exchange
     status halt / fill-velocity decline made our book FORGET quotes that were
     still resting and could still fill.
  2. The reprice sweep re-DROVE every pending withdrawal every 0.5 s tick —
     O(pending) WRITES per tick on the bucket that was already 429ing, and
     ``_delete_quote`` owned a SECOND, UNMETERED ``delete_quote`` call so those
     writes were not even counted. At the live ``max_open_quotes`` = 200 that is
     400 write tokens/tick against a 300 tok/s ceiling: the storm sustained its
     own 429s and never established truth.
  3. Quoting therefore BRICKED: the pending set never drained, the book sat at
     ``max_open_quotes``, and every new RFQ was refused for capacity.

The fix is structural, not tuned: exactly one function may call the sender's
delete (after spending its tokens), exactly two functions may drop a withdrawn
quote (and both hold PROOF), and the UNKNOWN is resolved by ONE open-quote READ
per tick — 10 read tokens, O(1) in the pending count, on the bucket the write
storm is not touching.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import (
    DEFAULT_ENDPOINT_TOKEN_COST,
    DELETE_QUOTE_TOKEN_COST,
    KalshiApiError,
    RateLimitedError,
)
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import MAINTENANCE_TICK_INTERVAL_S
from combomaker.ops.write_budget import TokenBudget, WriteBudget
from combomaker.rfq import lifecycle as lifecycle_mod
from combomaker.risk.limits import RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import Rig
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

SRC = Path(lifecycle_mod.__file__)
QUOTE_APP_SRC = SRC.parent.parent / "ops" / "quote_app.py"

# The OBSERVED live prod bucket, read read-only 2026-07-26 via
# GET /account/limits: advanced, read 600 cap / 300 refill, write 600 cap / 300
# refill — on TWO SEPARATE buckets. Every rate assertion below is graded against
# these, never against a number this test picked.
OBSERVED_WRITE_REFILL_PER_S = 300
OBSERVED_WRITE_CAPACITY = 600
OBSERVED_READ_REFILL_PER_S = 300

# The shipped operator write knob (ops.config.SupervisorConfig defaults), which
# is what ``_tier_clamped_write_budget`` hands the lifecycle live: 200 tokens per
# 10 s = 20 tok/s = 10 DELETEs/s sustained, burst 200 tokens = 100 DELETEs.
LIVE_WRITE_CAPACITY = 200
LIVE_WRITE_REFILL_S = 10.0

# config/prod-live-wc.local.yaml -> risk.max_open_quotes
LIVE_MAX_OPEN_QUOTES = 200


# ────────────────────────────────────────────────────────────── rig plumbing ──


class _FakeLister:
    """``GET /communications/quotes?user_filter=self&status=open``. Answers with
    whatever ids the test says the EXCHANGE still holds — the only thing that
    can prove a withdrawal."""

    def __init__(self, open_ids: Callable[[], set[str]]) -> None:
        self._open_ids = open_ids
        self.calls = 0
        self.fail: BaseException | None = None
        self.params: list[dict[str, Any]] = []

    async def get_quotes(self, **params: Any) -> dict[str, Any]:
        self.calls += 1
        self.params.append(dict(params))
        if self.fail is not None:
            raise self.fail
        return {"quotes": [{"id": q} for q in sorted(self._open_ids())], "cursor": ""}


class _Gone(KalshiApiError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "not found")


def _429() -> RateLimitedError:
    return RateLimitedError(429, "too_many_requests", "too many requests")


async def _rig(
    tmp_path: Path, db: str, *, max_open_quotes: int = 20, quotes: int = 1
) -> Rig:
    """A rig whose ONLY binding capacity limit is ``max_open_quotes`` — every
    other cap is opened up so the test grades the capacity/brick axis and not an
    unrelated dollar cap."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    rig = Rig(
        h,
        store,
        risk_limits=RiskLimits(
            max_open_quotes=max_open_quotes,
            max_market_delta_contracts=1e9,
            max_event_delta_contracts=1e9,
            max_gross_notional_dollars=1e9,
            max_event_worst_case_loss_dollars=1e9,
            caps_shadow_mode=True,
        ),
    )
    for i in range(1, quotes + 1):
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"rfq_{i}"))
    assert rig.lifecycle.open_quote_count == quotes
    return rig


def _wire_lister(rig: Rig, lister: _FakeLister, *, read_budget: bool = True) -> None:
    rig.lifecycle._quote_lister = lister  # noqa: SLF001 (ctor seam)
    if read_budget:
        rig.lifecycle._read_budget = TokenBudget.create(  # noqa: SLF001
            rig.h.clock,
            capacity=OBSERVED_WRITE_CAPACITY,
            refill_s=OBSERVED_WRITE_CAPACITY / OBSERVED_READ_REFILL_PER_S,
        )


def _live_write_budget(rig: Rig) -> WriteBudget:
    """The bucket ``quote_app._tier_clamped_write_budget()`` hands the live
    lifecycle: the operator knob clamped by the OBSERVED tier."""
    capacity = min(LIVE_WRITE_CAPACITY, OBSERVED_WRITE_CAPACITY)
    rate = min(LIVE_WRITE_CAPACITY / LIVE_WRITE_REFILL_S, OBSERVED_WRITE_REFILL_PER_S)
    budget = WriteBudget.create(rig.h.clock, capacity=capacity, refill_s=capacity / rate)
    rig.lifecycle._withdraw_budget = budget  # noqa: SLF001
    return budget


@pytest.fixture()
def simulated_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the token gate's ``asyncio.sleep`` ADVANCE the rig's clock instead of
    burning wall time, so the pacing arithmetic is exact and the test is fast.
    The bucket refills off that same clock, so what is measured is the real
    admission schedule, just in simulated seconds."""
    real_sleep = asyncio.sleep
    holder: dict[str, Any] = {}

    async def fake_sleep(delay: float, *a: Any, **k: Any) -> Any:
        clock = holder.get("clock")
        if clock is not None and delay > 0:
            clock.advance(delay)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    lifecycle_mod._SIMULATED_CLOCK_HOLDER = holder  # type: ignore[attr-defined]
    yield
    del lifecycle_mod._SIMULATED_CLOCK_HOLDER  # type: ignore[attr-defined]


def _bind_clock(rig: Rig) -> None:
    holder = getattr(lifecycle_mod, "_SIMULATED_CLOCK_HOLDER", None)
    assert holder is not None, "use the simulated_time fixture"
    holder["clock"] = rig.h.clock


def _mark_all_pending(rig: Rig) -> None:
    """Put every open quote into the UNKNOWN-withdrawal state the way the live
    code does — a real DELETE that 429s — not by poking the field."""
    for state in rig.lifecycle._open.values():  # noqa: SLF001
        assert state.withdraw_pending_reason is not None


async def _tick(rig: Rig) -> None:
    """One maintenance tick at the real cadence. The clock ADVANCE matters: the
    resolver's happens-before guard only resolves quotes whose withdrawal was
    asked STRICTLY BEFORE the list request was issued, so a test that froze time
    would (correctly) resolve nothing."""
    rig.h.clock.advance(MAINTENANCE_TICK_INTERVAL_S)
    await rig.lifecycle.maintenance_tick()


# ══════════════════════════════════════════════════════════════════════════════
# INVARIANTS — the fix is structural, so it is checkable in the SOURCE
# ══════════════════════════════════════════════════════════════════════════════


def _fn_nodes(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.setdefault(node.name, node)
            out[f"{node.name}#{node.lineno}"] = node
    return out


def _calls_to(node: ast.AST, attr: str) -> int:
    n = 0
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == attr
        ):
            n += 1
    return n


def test_sender_delete_quote_has_exactly_one_call_site() -> None:
    """DEFECT 2, ROOT CAUSE. An unmetered write must be UNWRITABLE, not merely
    discouraged: ``self._sender.delete_quote`` may appear exactly once in
    lifecycle.py — inside ``_withdraw_batch._one``, immediately after the token
    gate spends ``DELETE_QUOTE_TOKEN_COST``. ``_delete_quote``'s private copy is
    what let 1,137 withdrawals in the incident spend no tokens at all."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete_quote"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_sender"
    ]
    assert len(sites) == 1, f"delete_quote call sites at lines {[s.lineno for s in sites]}"
    fns = _fn_nodes(SRC)
    batch = fns["_withdraw_batch"]
    assert (
        batch.lineno < sites[0].lineno < max(  # type: ignore[attr-defined]
            n.lineno for n in ast.walk(batch) if hasattr(n, "lineno")
        )
    ), "the one delete_quote call must live inside _withdraw_batch"
    # ...and the token spend must PRECEDE it in that same function.
    spends = [
        node.lineno
        for node in ast.walk(batch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "try_spend"
    ]
    assert spends and min(spends) < sites[0].lineno


def test_no_withdrawal_entry_point_can_drop_an_unproven_quote() -> None:
    """DEFECT 1, ROOT CAUSE. ``_drop_quote`` must be unreachable from any
    withdrawal entry point except through PROOF. The four entry points
    (``_delete_quote``, ``cancel_all``, ``cancel_quotes_touching``,
    ``_withdraw_batch``) contain NO ``_drop_quote`` call at all; the only two
    droppers of a withdrawn quote are ``_withdraw_and_reconcile`` (the ``gone``
    list = ack or 404) and ``_resolve_withdraw_pending`` (absent from the
    exchange's own open-quote list). So the correctness of the fix cannot depend
    on any caller's docstring being true."""
    fns = _fn_nodes(SRC)
    for name in ("_delete_quote", "cancel_all", "cancel_quotes_touching", "_withdraw_batch"):
        assert _calls_to(fns[name], "_drop_quote") == 0, name
    assert _calls_to(fns["_withdraw_and_reconcile"], "_drop_quote") == 1
    assert _calls_to(fns["_resolve_withdraw_pending"], "_drop_quote") == 1


def test_the_reprice_sweep_is_no_longer_a_write_driver() -> None:
    """DEFECT 2, AMPLIFIER. The sweep must not re-drive pending withdrawals: it
    is O(open) per 0.5 s tick, which is what made the storm self-sustaining. Its
    pending branch must contain NO await at all — no write, no read, no wait."""
    src = textwrap.dedent(
        inspect.getsource(lifecycle_mod.QuoteLifecycle.maintenance_tick)
    )
    tree = ast.parse(src)
    branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "withdraw_pending_reason" in ast.dump(node.test)
    ]
    assert len(branches) == 1, "one pending branch in the sweep"
    awaits = [n for n in ast.walk(branches[0]) if isinstance(n, ast.Await)]
    assert awaits == [], "the reprice sweep is not a withdrawal driver any more"


def test_cancel_all_call_sites_are_all_the_one_implementation() -> None:
    """The 7 call sites the design enumerates, and no eighth. Nothing else in
    the tree may cancel a book."""
    sites: list[tuple[str, int]] = []
    for path in (SRC, QUOTE_APP_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cancel_all"
            ):
                sites.append((path.name, node.lineno))
    assert len(sites) == 7, sites


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — cancel_all under a PERSISTENT 429, from EVERY caller
# ══════════════════════════════════════════════════════════════════════════════

# The exact argument each of the 7 call sites passes.
CANCEL_ALL_CALLERS = [
    ("quote_app:on_invalidate (feed resync)", "book_invalidated", False),
    ("quote_app:on_halt (HARD halt -> _stop.set)", ReasonCode.HALT_MANUAL, True),
    ("quote_app:on_channel_lost (force reconnect)", "comms_channel_lost", False),
    ("quote_app:shutdown", ReasonCode.HALT_MANUAL, True),
    ("quote_app:HALT_EXCHANGE_STATUS", ReasonCode.HALT_EXCHANGE_STATUS, False),
    ("lifecycle:confirm DECLINE_FILL_VELOCITY", ReasonCode.DECLINE_FILL_VELOCITY, False),
    ("lifecycle:tick DECLINE_FILL_VELOCITY", ReasonCode.DECLINE_FILL_VELOCITY, False),
]


@pytest.mark.parametrize(
    ("site", "reason", "terminal"),
    CANCEL_ALL_CALLERS,
    ids=[c[0] for c in CANCEL_ALL_CALLERS],
)
async def test_cancel_all_under_persistent_429_never_forgets_a_quote(
    tmp_path: Path, site: str, reason: ReasonCode | str, terminal: bool
) -> None:
    """DEFECT 1. Under a persistent 429 nothing is PROVEN, so nothing leaves the
    mirror or the exposure book — from EVERY caller, including the two genuinely
    terminal ones (where keeping the mirror costs nothing: the process is
    stopping and the startup reconcile rebuilds truth from the exchange)."""
    rig = await _rig(tmp_path, f"ca-{abs(hash(site))}.sqlite3", quotes=3)
    ids = list(rig.lifecycle._open)  # noqa: SLF001

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(reason)

    for quote_id in ids:
        assert quote_id in rig.lifecycle._open, (site, quote_id)  # noqa: SLF001
        assert quote_id in rig.lifecycle._exposure.open_quotes, (site, quote_id)
        state = rig.lifecycle._open[quote_id]  # noqa: SLF001
        assert state.withdraw_pending_reason == reason
        assert state.withdraw_asked_mono_ns > 0
    assert rig.metrics.counter("quote.delete_unresolved") == 3
    assert rig.metrics.counter(f"quote.deleted.{reason}") == 0
    assert terminal in (True, False)  # both classes get the SAME rule


@pytest.mark.parametrize("outcome", ["ack", "404"])
async def test_cancel_all_drops_exactly_the_proven_gone(
    tmp_path: Path, outcome: str
) -> None:
    """The other half of the rule: an ACK and a 404 are both PROOF the quote is
    off the wire, and proven quotes DO leave the mirror. A mixed book drops
    exactly the proven half."""
    rig = await _rig(tmp_path, f"ca-ok-{outcome}.sqlite3", quotes=4)
    ids = list(rig.lifecycle._open)  # noqa: SLF001
    proven, unknown = set(ids[:2]), set(ids[2:])

    async def mixed(quote_id: str) -> dict[str, Any]:
        if quote_id in unknown:
            raise _429()
        if outcome == "404":
            raise _Gone()
        return {}

    rig.sender.delete_quote = mixed  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(ReasonCode.HALT_MANUAL)

    assert set(rig.lifecycle._open) == unknown  # noqa: SLF001
    assert set(rig.lifecycle._exposure.open_quotes) == unknown
    for quote_id in proven:
        assert quote_id not in rig.lifecycle._open  # noqa: SLF001
    assert rig.metrics.counter("quote.deleted.halt_manual") == 2
    if outcome == "404":
        assert rig.metrics.counter("quote.delete_already_gone") == 2


async def test_tick_fill_velocity_cancel_all_is_driven_end_to_end(
    tmp_path: Path,
) -> None:
    """One caller driven for real rather than by its argument: the maintenance
    tick's DECLINE_FILL_VELOCITY cancel-all (lifecycle.py, a caller whose
    'terminal path' premise was always false — the bot keeps running)."""
    rig = await _rig(tmp_path, "ca-fv.sqlite3", quotes=3)
    ids = list(rig.lifecycle._open)  # noqa: SLF001

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    rig.lifecycle._fill_velocity_verdict = lambda: (  # noqa: SLF001
        "decline",
        "test burst",
    )
    await _tick(rig)
    assert rig.metrics.counter("quote.cancel_all") == 1
    for quote_id in ids:
        assert quote_id in rig.lifecycle._open  # noqa: SLF001
        assert quote_id in rig.lifecycle._exposure.open_quotes


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — a persistent write-side 429 with 8+ pending: BOUNDED and METERED
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("pending_n", [12, LIVE_MAX_OPEN_QUOTES])
async def test_delete_storm_is_bounded_by_the_write_bucket(
    tmp_path: Path, simulated_time: None, pending_n: int
) -> None:
    """DEFECT 2. N quotes, every DELETE 429s forever, and the exchange's
    open-quote list says all N are STILL RESTING — the worst case, where every
    single pending quote justifies a re-DELETE.

    What must hold:
      * every emitted DELETE SPENT write tokens (an unmetered write is
        unwritable);
      * the emitted token rate stays inside the bucket's own guarantee
        (capacity + rate x T in any window T), which is inside the OBSERVED
        advanced ceiling of 300 write tok/s;
      * the list READ is ONE per tick — O(1) in the pending count — on the READ
        bucket, which the write storm is not touching."""
    rig = await _rig(
        tmp_path,
        f"storm-{pending_n}.sqlite3",
        max_open_quotes=2 * pending_n,  # capacity is NOT the axis under test
        quotes=pending_n,
    )
    _bind_clock(rig)
    budget = _live_write_budget(rig)
    resting = set(rig.lifecycle._open)  # noqa: SLF001
    lister = _FakeLister(lambda: resting)
    _wire_lister(rig, lister)

    emitted: list[float] = []

    async def refuses(quote_id: str) -> dict[str, Any]:
        emitted.append(rig.h.clock.now().timestamp())
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]

    # Every RFQ dies on the exchange: N real, event-driven withdrawals, all 429.
    for rfq_id in list(rig.lifecycle._by_rfq):
        await rig.lifecycle.on_rfq_deleted(rfq_id, {})
    _mark_all_pending(rig)
    emitted.clear()

    t0 = rig.h.clock.now().timestamp()
    ticks = 12
    for _ in range(ticks):
        await _tick(rig)
    elapsed = rig.h.clock.now().timestamp() - t0

    n = len(emitted)
    tokens = n * DELETE_QUOTE_TOKEN_COST
    # The bucket's own guarantee, and the exchange ceiling it was sized against.
    allowed = budget.capacity + budget.rate_per_s * elapsed
    peak_1s = max(
        (sum(1 for t in emitted if s <= t < s + 1.0) * DELETE_QUOTE_TOKEN_COST)
        for s in emitted
    ) if emitted else 0
    reads = lister.calls

    print(
        f"\n  DELETE STORM  ({pending_n} pending, every DELETE 429, all PROVEN "
        f"resting)\n"
        f"    write bucket : capacity {budget.capacity} tok, "
        f"{budget.rate_per_s:.1f} tok/s  (operator knob {LIVE_WRITE_CAPACITY}/"
        f"{LIVE_WRITE_REFILL_S}s, clamped by observed tier {OBSERVED_WRITE_CAPACITY}"
        f"/{OBSERVED_WRITE_REFILL_PER_S})\n"
        f"    emitted      : {n} DELETEs = {tokens} write tokens over "
        f"{elapsed:.2f}s = {n / elapsed:.1f} req/s = {tokens / elapsed:.1f} tok/s\n"
        f"    bucket bound : capacity + rate*T = {budget.capacity} + "
        f"{budget.rate_per_s:.1f}*{elapsed:.2f} = {allowed:.0f} tok  "
        f"(emitted {tokens} <= {allowed:.0f})\n"
        f"    peak any 1s  : {peak_1s} tok  <= capacity + rate = "
        f"{budget.capacity + budget.rate_per_s:.0f} tok  "
        f"<< observed ceiling {OBSERVED_WRITE_REFILL_PER_S} tok/s\n"
        f"    list reads   : {reads} over {ticks} ticks = "
        f"{reads / ticks:.2f}/tick x {DEFAULT_ENDPOINT_TOKEN_COST} read tok = "
        f"{reads * DEFAULT_ENDPOINT_TOKEN_COST / ticks:.0f} read tok/tick "
        f"(O(1) in pending, separate bucket)\n"
        f"    DELETED driver: the reprice sweep re-drove ALL {pending_n} pending "
        f"every 0.5s tick, unmetered = "
        f"{pending_n * DELETE_QUOTE_TOKEN_COST} write tok/tick = "
        f"{pending_n * DELETE_QUOTE_TOKEN_COST / MAINTENANCE_TICK_INTERVAL_S:.0f} "
        f"tok/s vs the {OBSERVED_WRITE_REFILL_PER_S} tok/s ceiling  "
        f"({pending_n * ticks} DELETEs over these {ticks} ticks)\n"
    )

    # 1. Metered: tokens were actually spent, and the bucket shows the drain.
    assert n > 0
    assert budget.tokens < budget.capacity
    # 2. Bounded by the bucket, not by a cadence.
    assert tokens <= allowed + DELETE_QUOTE_TOKEN_COST
    assert peak_1s <= budget.capacity + budget.rate_per_s
    assert tokens / elapsed <= OBSERVED_WRITE_REFILL_PER_S
    assert n / elapsed <= OBSERVED_WRITE_CAPACITY
    # 3. The prover is O(1): at most one list read per tick.
    assert reads <= ticks
    assert reads * DEFAULT_ENDPOINT_TOKEN_COST <= ticks * OBSERVED_READ_REFILL_PER_S
    # 4. Nothing was forgotten: 429 proves nothing.
    assert set(rig.lifecycle._open) == resting  # noqa: SLF001
    # 5. The DELETED retry driver would have emitted one write per pending quote
    #    per tick, unmetered. At the live book that is over the ceiling.
    assert n < pending_n * ticks or pending_n * ticks * DELETE_QUOTE_TOKEN_COST / elapsed < (
        OBSERVED_WRITE_REFILL_PER_S
    )


async def test_the_read_is_windowed_and_scoped_like_the_kill_path(
    tmp_path: Path,
) -> None:
    """The prover reuses the SHARED enumerator, so it carries the mandatory
    min_ts/max_ts window that stops a full-history scan from tripping the
    exchange circuit breaker — the reason that helper exists at all."""
    rig = await _rig(tmp_path, "window.sqlite3", quotes=1)
    lister = _FakeLister(set)
    _wire_lister(rig, lister)

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(ReasonCode.HALT_MANUAL)
    await _tick(rig)

    assert lister.calls == 1
    params = lister.params[0]
    assert params["user_filter"] == "self"
    assert params["status"] == "open"
    assert params["min_ts"] < params["max_ts"]
    assert params["limit"] == 500


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — QUOTING CANNOT BRICK (the operator's standing ship rule)
# ══════════════════════════════════════════════════════════════════════════════


async def test_quoting_cannot_brick_at_the_live_max_open_quotes(
    tmp_path: Path, simulated_time: None
) -> None:
    """DEFECT 3, the ship rule. The book is FULL at the live
    ``max_open_quotes`` = 200, every DELETE 429s forever, and every one of the
    200 quotes is therefore an UNKNOWN withdrawal.

    Pre-fix that is a permanent brick: the mirror pins at 200, every RFQ is
    refused for capacity, and the retry driver's 400 write tok/tick sustains the
    429s that keep it there.

    Post-fix ONE read resolves the whole set. The dominant live case is exactly
    this: 1,104 of the incident's 1,137 withdrawals were DELETE_RFQ_GONE, whose
    quotes the exchange had already dropped with their RFQs — so the read proves
    them gone, the mirror empties, and quoting resumes on the SAME tick, with no
    successful DELETE anywhere in the story."""
    rig = await _rig(
        tmp_path,
        "brick.sqlite3",
        max_open_quotes=LIVE_MAX_OPEN_QUOTES,
        quotes=LIVE_MAX_OPEN_QUOTES,
    )
    _bind_clock(rig)
    _live_write_budget(rig)

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]

    # The book is FULL: a new RFQ is refused for capacity. That refusal is
    # CORRECT — 200 possibly-resting quotes are 200 quotes of real risk.
    created_before = len(rig.sender.created)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_new_full"))
    assert len(rig.sender.created) == created_before

    # Every quote's RFQ dies on the exchange; every DELETE 429s. 200 UNKNOWNs.
    for rfq_id in list(rig.lifecycle._by_rfq):
        await rig.lifecycle.on_rfq_deleted(rfq_id, {})
    pending = sum(
        1
        for s in rig.lifecycle._open.values()  # noqa: SLF001
        if s.withdraw_pending_reason is not None
    )
    assert pending == LIVE_MAX_OPEN_QUOTES
    assert rig.lifecycle.open_quote_count == LIVE_MAX_OPEN_QUOTES

    # The exchange dropped them with their RFQs — the read is the only thing
    # that can know it.
    lister = _FakeLister(set)
    _wire_lister(rig, lister)
    await _tick(rig)

    assert lister.calls == 1, "one READ resolved all 200 — O(1) in pending"
    assert rig.lifecycle.open_quote_count == 0
    assert rig.metrics.counter("withdraw_resolve.proven_gone") == LIVE_MAX_OPEN_QUOTES
    assert rig.metrics.counter("quote.deleted.delete_rfq_gone") == LIVE_MAX_OPEN_QUOTES
    assert rig.sender.deleted == [], "not one DELETE ever succeeded"

    # THE SHIP RULE: a new RFQ quotes, with a real price and a real size.
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_after"))
    assert len(rig.sender.created) == created_before + 1
    quote = rig.sender.created[-1]
    state = rig.lifecycle._open[str(quote["id"])]  # noqa: SLF001
    print(
        f"\n  BRICK TEST  (max_open_quotes={LIVE_MAX_OPEN_QUOTES}, every DELETE 429)\n"
        f"    at capacity   : new RFQ refused (correct — 200 quotes of real risk)\n"
        f"    one list READ : {DEFAULT_ENDPOINT_TOKEN_COST} read tokens resolved "
        f"all {LIVE_MAX_OPEN_QUOTES} pending; 0 successful DELETEs\n"
        f"    QUOTED AFTER  : yes {int(quote['yes'])/100:.2f}c  "
        f"no {int(quote['no'])/100:.2f}c  "
        f"risk_qty {int(state.risk_qty)/100:.2f} contracts\n"
    )
    assert int(quote["yes"]) > 0
    assert int(quote["no"]) > 0
    assert int(state.risk_qty) > 0


async def test_a_full_book_of_PROVEN_RESTING_quotes_takes_the_bounded_exit(
    tmp_path: Path, simulated_time: None
) -> None:
    """The other branch, and it must also terminate. If the 200 quotes really
    ARE still resting and the write path really cannot remove them, refusing to
    quote is CORRECT (the exposure is real) — so the state escalates instead of
    sitting bricked: HALT_NEEDS_RECONCILE, whose restart rebuilds the book from
    the exchange over this same endpoint. No branch converts UNKNOWN to gone."""
    rig = await _rig(
        tmp_path,
        "halt.sqlite3",
        max_open_quotes=LIVE_MAX_OPEN_QUOTES,
        quotes=LIVE_MAX_OPEN_QUOTES,
    )
    _bind_clock(rig)
    _live_write_budget(rig)
    resting = set(rig.lifecycle._open)  # noqa: SLF001
    _wire_lister(rig, _FakeLister(lambda: resting))

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    for rfq_id in list(rig.lifecycle._by_rfq):
        await rig.lifecycle.on_rfq_deleted(rfq_id, {})
    assert not rig.killswitch.halted

    await _tick(rig)

    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason is ReasonCode.HALT_NEEDS_RECONCILE
    assert rig.metrics.counter("withdraw_resolve.unprovable_halt") == 1
    # Still nothing forgotten — the halt is an escalation, not an amnesia.
    assert set(rig.lifecycle._open) == resting  # noqa: SLF001


async def test_a_partial_book_never_halts(tmp_path: Path) -> None:
    """The terminal predicate is a conjunction over EXISTING state and an
    EXISTING limit: below capacity, or with one non-pending quote, or with a
    successful read newer than the oldest ask, it does not fire."""
    rig = await _rig(tmp_path, "nohalt.sqlite3", max_open_quotes=4, quotes=3)
    resting = set(rig.lifecycle._open)  # noqa: SLF001
    _wire_lister(rig, _FakeLister(lambda: resting))

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS)
    await _tick(rig)
    assert not rig.killswitch.halted  # 3 open < cap 4


# ══════════════════════════════════════════════════════════════════════════════
# TEST 4 — a gone quote is reaped; a resting quote is never forgotten
# ══════════════════════════════════════════════════════════════════════════════


async def test_gone_is_reaped_and_resting_is_never_forgotten(tmp_path: Path) -> None:
    """Both halves in ONE book, resolved by ONE read: quote A is absent from the
    exchange's open-quote list (PROVEN off the wire, reaped) while quote B is
    present (PROVEN still resting, kept AND re-asked)."""
    rig = await _rig(tmp_path, "reap.sqlite3", max_open_quotes=10, quotes=2)
    a, b = list(rig.lifecycle._open)  # noqa: SLF001
    _wire_lister(rig, _FakeLister(lambda: {b}))
    asked: list[str] = []

    async def refuses(quote_id: str) -> dict[str, Any]:
        asked.append(quote_id)
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS)
    assert set(rig.lifecycle._open) == {a, b}  # noqa: SLF001 — 429 proves nothing
    asked.clear()

    await _tick(rig)

    # A: absent from the exchange ⇒ reaped, and counted as a real deletion.
    assert a not in rig.lifecycle._open  # noqa: SLF001
    assert a not in rig.lifecycle._exposure.open_quotes
    assert rig.metrics.counter("withdraw_resolve.proven_gone") == 1
    assert rig.metrics.counter("quote.deleted.halt_exchange_status") == 1
    # B: present ⇒ still ours, still counted by risk, and RE-ASKED (the
    # eviction/withdrawal intent is never lost).
    assert b in rig.lifecycle._open  # noqa: SLF001
    assert b in rig.lifecycle._exposure.open_quotes
    assert rig.lifecycle._open[b].withdraw_pending_reason == (  # noqa: SLF001
        ReasonCode.HALT_EXCHANGE_STATUS
    )
    assert asked == [b], "only the PROVEN-resting quote pays a write"


async def test_a_failed_read_resolves_nothing(tmp_path: Path) -> None:
    """UNKNOWN never silently becomes gone. A read that 429s / 5xxs / times out
    leaves every pending quote exactly where it was — the antithesis of the
    force-drop-after-N-ticks alternative the design rejected."""
    rig = await _rig(tmp_path, "readfail.sqlite3", max_open_quotes=10, quotes=2)
    ids = set(rig.lifecycle._open)  # noqa: SLF001
    lister = _FakeLister(set)  # would say "all gone" if it ever answered
    lister.fail = _429()
    _wire_lister(rig, lister)

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS)
    await _tick(rig)

    assert set(rig.lifecycle._open) == ids  # noqa: SLF001
    assert set(rig.lifecycle._exposure.open_quotes) == ids
    assert rig.metrics.counter("withdraw_resolve.proven_gone") == 0
    assert rig.metrics.counter("withdraw_resolve.read_failed") == 1


async def test_the_read_is_refused_rather_than_queued_when_out_of_read_tokens(
    tmp_path: Path,
) -> None:
    """The resolver spends the SAME read bucket every other read spends, and a
    refusal is a SKIP (the 0.5 s tick is the retry), never a queued wait on the
    maintenance loop."""
    rig = await _rig(tmp_path, "readbudget.sqlite3", max_open_quotes=10, quotes=1)
    resting = set(rig.lifecycle._open)  # noqa: SLF001 — stays pending across ticks
    lister = _FakeLister(lambda: resting)
    _wire_lister(rig, lister, read_budget=False)
    budget = TokenBudget.create(rig.h.clock, capacity=DEFAULT_ENDPOINT_TOKEN_COST, refill_s=60.0)
    rig.lifecycle._read_budget = budget  # noqa: SLF001

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS)

    await _tick(rig)  # spends the only 10 tokens
    assert lister.calls == 1
    await _tick(rig)  # bucket empty ⇒ refused, not queued
    assert lister.calls == 1
    assert rig.metrics.counter("withdraw_resolve.read_budget_deferred") == 1


async def test_happens_before_guard_rejects_a_delete_that_postdates_the_read(
    tmp_path: Path,
) -> None:
    """The read-after-write hazard is killed by CONSTRUCTION, not by a delay: a
    quote whose DELETE was issued AFTER the list request was sent cannot be
    resolved by that list's silence, because its ask does not strictly pre-date
    the read. Here B is re-asked from INSIDE the list call, so the exchange
    could not possibly have reflected that DELETE — and the resolver refuses to
    call B gone even though the list is empty. A, asked before the request went
    out, IS resolvable by the same read."""
    rig = await _rig(tmp_path, "hb.sqlite3", max_open_quotes=10, quotes=2)
    a, b = list(rig.lifecycle._open)  # noqa: SLF001

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    # Both pending BEFORE the read, both asks stamped in the past.
    await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
        [a, b], ReasonCode.DELETE_TTL_EXPIRED
    )

    class _RacingLister:
        calls = 0

        async def get_quotes(self, **params: Any) -> dict[str, Any]:
            _RacingLister.calls += 1
            # An in-flight DELETE for B lands AFTER this request went out.
            rig.h.clock.advance(0.001)
            await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
                [b], ReasonCode.DELETE_TTL_EXPIRED
            )
            return {"quotes": [], "cursor": ""}  # exchange reports NEITHER open

    rig.lifecycle._quote_lister = _RacingLister()  # noqa: SLF001
    await _tick(rig)

    assert a not in rig.lifecycle._open, "A's ask pre-dates the read ⇒ provable"  # noqa: SLF001
    assert b in rig.lifecycle._open, (  # noqa: SLF001
        "B's DELETE post-dates the list request — this read cannot speak about it"
    )
    assert rig.metrics.counter("withdraw_resolve.not_yet_readable") == 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST 5 — the pending set cannot grow without bound
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_pending_set_is_bounded_by_max_open_quotes(
    tmp_path: Path, simulated_time: None
) -> None:
    """THE BOUND, and where it comes from: a withdraw-pending quote STAYS in
    ``_open`` and in ``exposure.open_quotes``, so it keeps counting against
    ``risk.max_open_quotes`` — the same limit that bounds the resting book. The
    pending set is therefore a SUBSET of the open book and is bounded by that
    single existing cap, with no counter, TTL or attempt limit of its own.

    (Excluding pending quotes from the cap was explicitly rejected: it would let
    a write outage grow an unbounded book of possibly-resting quotes while we
    keep quoting on top of it — an uncapped mass-acceptance worst case.)"""
    cap = 6
    rig = await _rig(tmp_path, "bound.sqlite3", max_open_quotes=cap, quotes=cap)
    _bind_clock(rig)
    _live_write_budget(rig)
    resting = set(rig.lifecycle._open)  # noqa: SLF001
    _wire_lister(rig, _FakeLister(lambda: resting))

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    for rfq_id in list(rig.lifecycle._by_rfq):
        await rig.lifecycle.on_rfq_deleted(rfq_id, {})

    for i in range(8):
        # Every tick: try to add MORE quotes and re-run the resolver. Neither the
        # open book nor the pending set may exceed the cap.
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"extra_{i}"))
        # Keep driving past the bounded-exit halt so the BOUND itself is what is
        # being graded, not the escalation.
        rig.killswitch.clear("bound-test")
        await _tick(rig)
        pending = [
            q
            for q, s in rig.lifecycle._open.items()  # noqa: SLF001
            if s.withdraw_pending_reason is not None
        ]
        assert len(rig.lifecycle._open) <= cap  # noqa: SLF001
        assert len(pending) <= len(rig.lifecycle._open)  # noqa: SLF001
        assert set(pending) <= resting


# ══════════════════════════════════════════════════════════════════════════════
# TEST 6 — B1: the resolver must never touch a MID-CONFIRM (accepted) quote
#
# The one-step live path the adversarial gate proved with PoCs A and A2:
#   a TTL / RFQ-gone DELETE 429s  =>  the quote is UNKNOWN and STILL RESTING
#   =>  the taker ACCEPTS it      =>  on_quote_accepted sets accepted=True and
#      then AWAITS (store write + reservation + confirm REST), spanning several
#      0.5 s maintenance ticks.
# Inside that window the resolver was the ONLY withdrawal chooser without the
# ``not state.accepted`` guard the other three carry — and the only one that
# both REAPS and re-DELETEs.
# ══════════════════════════════════════════════════════════════════════════════


async def _pending_then_accepted(
    rig: Rig, quote_id: str
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Drive the real one-step path and PARK the accept mid-confirm.

    The DELETE 429s (UNKNOWN => the quote stays in the mirror, marked
    withdraw-pending), the taker then accepts, and the confirm REST call is held
    open on an Event so the quote sits ACCEPTED + withdraw-pending across
    maintenance ticks exactly as it does live."""
    from tests.test_lifecycle import accepted_msg

    async def refuses(qid: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
        [quote_id], ReasonCode.DELETE_TTL_EXPIRED
    )
    state = rig.lifecycle._open[quote_id]  # noqa: SLF001
    assert state.withdraw_pending_reason == ReasonCode.DELETE_TTL_EXPIRED
    assert not state.accepted

    gate = asyncio.Event()

    async def slow_confirm(qid: str) -> dict[str, Any]:
        rig.sender.confirmed.append(qid)
        await gate.wait()  # the confirm round trip, held open
        return {}

    rig.sender.confirm_quote = slow_confirm  # type: ignore[assignment]
    task = asyncio.create_task(rig.lifecycle.on_quote_accepted(accepted_msg(quote_id)))
    for _ in range(500):
        if rig.sender.confirmed:
            break
        await asyncio.sleep(0)
    assert rig.sender.confirmed == [quote_id], "the accept must be parked mid-confirm"
    assert rig.lifecycle._open[quote_id].accepted  # noqa: SLF001
    return (task, gate)


async def test_poc_a_an_accepted_quote_is_never_reaped(tmp_path: Path) -> None:
    """PoC A. An ACCEPTED quote is no longer OPEN on the exchange, so the
    open-quote list does not list it — and the unguarded resolver read that
    absence as PROOF of withdrawal: a quote that FILLED was booked as a proven
    deletion, dropped from the mirror and from exposure mid-confirm.

    Guarded, the quote is DEFERRED (counted, kept, still risk-bearing) while the
    NON-accepted pending quote beside it is resolved by the same read."""
    rig = await _rig(tmp_path, "poc-a.sqlite3", max_open_quotes=10, quotes=2)
    accepted_id, other_id = list(rig.lifecycle._open)  # noqa: SLF001
    # The exchange lists NEITHER: the accepted one because an accept is not open,
    # the other because its DELETE really did land.
    _wire_lister(rig, _FakeLister(set))
    task, gate = await _pending_then_accepted(rig, accepted_id)
    await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
        [other_id], ReasonCode.DELETE_TTL_EXPIRED
    )

    await _tick(rig)
    await _tick(rig)

    still_open = accepted_id in rig.lifecycle._open  # noqa: SLF001
    print(
        f"\n  PoC A  (accepted quote ABSENT from the exchange's open list)\n"
        f"    still in _open?          {still_open}\n"
        f"    withdraw_resolve.proven_gone = "
        f"{rig.metrics.counter('withdraw_resolve.proven_gone')}   "
        f"(the OTHER, non-accepted pending quote)\n"
        f"    withdraw_resolve.accepted_deferred = "
        f"{rig.metrics.counter('withdraw_resolve.accepted_deferred')}\n"
    )
    assert still_open, "a mid-confirm quote must never be reaped"
    assert rig.metrics.counter("withdraw_resolve.accepted_deferred") == 2  # 2 ticks
    assert rig.metrics.counter(f"quote.deleted.{ReasonCode.DELETE_TTL_EXPIRED}") == 1
    assert rig.metrics.counter("withdraw_resolve.proven_gone") == 1
    assert other_id not in rig.lifecycle._open  # noqa: SLF001

    gate.set()
    await task


async def test_poc_a2_no_delete_is_ever_issued_against_a_mid_confirm_quote(
    tmp_path: Path,
) -> None:
    """PoC A2. The other branch of the same read: the exchange still LISTS the
    quote, so the unguarded resolver re-DELETEd it — a fresh withdrawal aimed at
    a quote whose risk we have already reserved and are about to confirm."""
    rig = await _rig(tmp_path, "poc-a2.sqlite3", max_open_quotes=10, quotes=2)
    accepted_id, other_id = list(rig.lifecycle._open)  # noqa: SLF001
    resting = {accepted_id, other_id}
    _wire_lister(rig, _FakeLister(lambda: resting))
    task, gate = await _pending_then_accepted(rig, accepted_id)
    await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
        [other_id], ReasonCode.DELETE_TTL_EXPIRED
    )

    asked: list[str] = []

    async def refuses(qid: str) -> dict[str, Any]:
        asked.append(qid)
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await _tick(rig)
    await _tick(rig)

    print(
        f"\n  PoC A2  (accepted quote STILL LISTED open)\n"
        f"    DELETEs issued against a MID-CONFIRM quote: "
        f"{[q for q in asked if q == accepted_id]}\n"
        f"    DELETEs issued against the resting quote  : "
        f"{[q for q in asked if q == other_id]}\n"
    )
    assert accepted_id not in asked, "never re-DELETE a quote we are confirming"
    assert asked == [other_id, other_id], "the proven-resting quote is still driven"
    assert accepted_id in rig.lifecycle._open  # noqa: SLF001

    gate.set()
    await task


async def test_the_deferred_accepted_quote_resolves_when_the_confirm_completes(
    tmp_path: Path,
) -> None:
    """THE DEFERRAL TERMINATES (the inverse hazard). The guard must not strand
    the pending state: the accept path owns an accepted quote and drops it from
    the mirror in EVERY branch, so the deferral ends in exactly one confirm
    window. Here the confirm completes — the quote leaves ``_open`` (pending
    state and all), the position is booked, and the resolver has nothing left to
    defer."""
    rig = await _rig(tmp_path, "defer-term.sqlite3", max_open_quotes=10, quotes=1)
    (quote_id,) = list(rig.lifecycle._open)  # noqa: SLF001
    _wire_lister(rig, _FakeLister(set))
    task, gate = await _pending_then_accepted(rig, quote_id)

    for _ in range(3):
        await _tick(rig)
    assert quote_id in rig.lifecycle._open  # noqa: SLF001 — deferred, not stranded

    gate.set()
    await task

    assert quote_id not in rig.lifecycle._open  # noqa: SLF001
    assert quote_id not in rig.lifecycle._exposure.open_quotes
    await _tick(rig)
    pending = [
        q
        for q, s in rig.lifecycle._open.items()  # noqa: SLF001
        if s.withdraw_pending_reason is not None
    ]
    assert pending == []
    # The fill is REAL and booked — the position, not the resting quote, carries
    # the risk now (nothing unconfirmed was dropped: the quote was CONFIRMED).
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    assert len(rig.lifecycle._exposure.positions) == 1
    print(
        "\n  DEFERRAL TERMINATION  (confirm completes)\n"
        f"    open after confirm      : {rig.lifecycle.open_quote_count}\n"
        f"    pending after next tick : {len(pending)}\n"
        f"    positions booked        : {len(rig.lifecycle._exposure.positions)}\n"
    )


def _raiser(*a: Any, **k: Any) -> Any:
    raise RuntimeError("risk reservation exploded mid-confirm")


@pytest.mark.parametrize("branch", ["confirm_raises", "lapse"])
async def test_the_deferral_terminates_on_every_accept_branch(
    tmp_path: Path, branch: str
) -> None:
    """Termination does not depend on the network or on a happy path. Two more
    branches: a LAPSE (unreadable accepted_side — the deliberate no-confirm) and
    an EXCEPTION escaping mid-confirm (which quote_app's quote_event_worker logs
    and moves past, so before the wrapper's ``_drop_quote`` the state would have
    stayed accepted + withdraw-pending in the mirror FOREVER — the guard turned
    into a strand)."""
    from tests.test_lifecycle import accepted_msg

    rig = await _rig(tmp_path, f"defer-{branch}.sqlite3", max_open_quotes=10, quotes=1)
    (quote_id,) = list(rig.lifecycle._open)  # noqa: SLF001
    _wire_lister(rig, _FakeLister(set))

    async def refuses(qid: str) -> dict[str, Any]:
        raise _429()

    rig.sender.delete_quote = refuses  # type: ignore[assignment]
    await rig.lifecycle._withdraw_and_reconcile(  # noqa: SLF001
        [quote_id], ReasonCode.DELETE_TTL_EXPIRED
    )

    msg = accepted_msg(quote_id)
    if branch == "lapse":
        msg["accepted_side"] = "sideways"  # unreadable side => deliberate lapse
    else:
        # Raise between "accepted=True" and the drop — the strand window. The
        # reservation call is OUTSIDE the confirm try/except, so this escapes
        # to quote_app's quote_event_worker exactly as a real bug would.
        rig.lifecycle._reserve_headroom = _raiser  # type: ignore[assignment] # noqa: SLF001

    raised = False
    try:
        await rig.lifecycle.on_quote_accepted(msg)
    except Exception:
        raised = True

    assert quote_id not in rig.lifecycle._open, (  # noqa: SLF001
        "an accepted quote must leave the mirror on EVERY branch, or the "
        "resolver's deferral becomes a permanent strand"
    )
    await _tick(rig)
    assert rig.metrics.counter("withdraw_resolve.accepted_deferred") == 0
    assert raised == (branch == "confirm_raises")
    print(
        f"\n  DEFERRAL TERMINATION  (branch={branch}, raised={raised})\n"
        f"    open after accept : {rig.lifecycle.open_quote_count}\n"
        f"    errored metric    : {rig.metrics.counter('confirm.errored')}\n"
    )


def test_every_withdrawal_chooser_refuses_an_accepted_quote() -> None:
    """B1, ROOT CAUSE, checked in the SOURCE. All FOUR choosers of a quote to
    withdraw/yank must test ``state.accepted``: ``cancel_all``,
    ``cancel_quotes_touching``, ``_pick_eviction_victim`` — and now
    ``_resolve_withdraw_pending``, which was the only one without it."""
    fns = _fn_nodes(SRC)
    for name in (
        "cancel_all",
        "cancel_quotes_touching",
        "_pick_eviction_victim",
        "_resolve_withdraw_pending",
    ):
        attrs = [
            n.attr
            for n in ast.walk(fns[name])
            if isinstance(n, ast.Attribute) and n.attr == "accepted"
        ]
        assert attrs, f"{name} does not guard on state.accepted"


# ══════════════════════════════════════════════════════════════════════════════
# TEST 7 — B2: cancel_all must not hold a NON-TERMINAL caller for tens of
# seconds (the operator standing rule: THROUGHPUT NEVER REGRESSES)
# ══════════════════════════════════════════════════════════════════════════════


def _drain(budget: WriteBudget) -> None:
    while budget.try_spend(DELETE_QUOTE_TOKEN_COST):
        pass


@pytest.mark.parametrize("bucket", ["full", "empty"])
@pytest.mark.parametrize("n", [50, 100, LIVE_MAX_OPEN_QUOTES])
async def test_cancel_all_wall_time_bounded_for_non_terminal_callers(
    tmp_path: Path, simulated_time: None, n: int, bucket: str
) -> None:
    """The measurement, both ways round. Same book, same live bucket
    (200 tok / 20 tok/s), every DELETE ACKs — so the hold is PURE PACING:

      * ``budget_s=None`` (what every caller got before this fix, and what the
        two genuinely terminal callers still pass) reproduces the regression:
        the cliff at 100 quotes and a ten-to-twenty second hold at the live
        ``max_open_quotes`` = 200.
      * the default bound (``_WITHDRAW_RESOLVE_BUDGET_S`` — the read-resolver's
        own per-tick pass budget, an EXISTING primitive) holds no caller past
        it, and whatever did not fit stays in the mirror marked
        withdraw-pending for that resolver to finish."""
    budget_s = lifecycle_mod._WITHDRAW_RESOLVE_BUDGET_S  # noqa: SLF001

    async def _measure(db: str, *, bounded: bool) -> tuple[float, int, int]:
        rig = await _rig(tmp_path, db, max_open_quotes=n, quotes=n)
        _bind_clock(rig)
        budget = _live_write_budget(rig)
        if bucket == "empty":
            _drain(budget)
        acked: list[str] = []

        async def acks(quote_id: str) -> dict[str, Any]:
            acked.append(quote_id)
            return {}

        rig.sender.delete_quote = acks  # type: ignore[assignment]
        t0 = rig.h.clock.now().timestamp()
        if bounded:
            # HALT_EXCHANGE_STATUS — a NON-terminal caller (the status loop keeps
            # running), taking the default bound.
            await rig.lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS)
        else:
            # The terminal callers opt-out (on_halt / shutdown).
            await rig.lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS, budget_s=None)
        held = rig.h.clock.now().timestamp() - t0
        left = len(rig.lifecycle._open)  # noqa: SLF001
        # NOTHING UNCONFIRMED IS DROPPED: every quote that was not provably
        # deleted is still in the mirror AND in exposure, marked pending.
        for quote_id, state in rig.lifecycle._open.items():  # noqa: SLF001
            assert quote_id in rig.lifecycle._exposure.open_quotes
            assert state.withdraw_pending_reason == ReasonCode.HALT_EXCHANGE_STATUS
        assert len(acked) + left == n
        return (held, len(acked), left)

    unbounded_s, unbounded_done, _ = await _measure(
        f"wu-{n}-{bucket}.sqlite3", bounded=False
    )
    bounded_s, bounded_done, bounded_left = await _measure(
        f"wb-{n}-{bucket}.sqlite3", bounded=True
    )

    print(
        f"\n  CANCEL_ALL WALL TIME  (n={n}, bucket {bucket}, every DELETE ACKs, "
        f"live bucket {LIVE_WRITE_CAPACITY} tok / "
        f"{LIVE_WRITE_CAPACITY / LIVE_WRITE_REFILL_S:.0f} tok/s)\n"
        f"    budget_s=None (pre-fix / terminal callers): "
        f"{unbounded_s * 1000:8.1f} ms   deleted {unbounded_done}/{n}\n"
        f"    default bound ({budget_s}s, non-terminal)  : "
        f"{bounded_s * 1000:8.1f} ms   deleted {bounded_done}/{n}, "
        f"{bounded_left} left pending for the read-resolver\n"
    )
    assert unbounded_done == n  # terminal path still attempts every quote
    assert bounded_s <= budget_s + 0.2, (bounded_s, budget_s)
    assert bounded_s <= unbounded_s + 1e-9


async def test_the_ws_feed_paths_are_not_stalled(
    tmp_path: Path, simulated_time: None
) -> None:
    """The two WS paths that carry our flow, driven through the REAL objects
    with the REAL quote_app closures registered on them:

      * ``OrderbookFeed._handle_disconnect`` -> ``_fire_invalidate`` -> the
        ``on_invalidate`` handler (also fired from a seq GAP *before* the resync
        command is sent);
      * ``RfqIntake._handle_error`` (terminal code) -> ``on_channel_lost``,
        which force-reconnects the socket carrying our RFQ flow right after.

    Both are inline and NOT terminal, so neither may be held for tens of
    seconds."""
    from combomaker.rfq.intake import RfqIntake

    rig = await _rig(
        tmp_path,
        "wsstall.sqlite3",
        max_open_quotes=LIVE_MAX_OPEN_QUOTES,
        quotes=LIVE_MAX_OPEN_QUOTES,
    )
    _bind_clock(rig)
    _drain(_live_write_budget(rig))  # worst case: no tokens at all

    async def acks(quote_id: str) -> dict[str, Any]:
        return {}

    rig.sender.delete_quote = acks  # type: ignore[assignment]

    # The EXACT closures quote_app registers (quote_app.on_invalidate /
    # on_channel_lost), on the real feed and the real intake.
    async def on_invalidate(reason: str) -> None:
        await rig.lifecycle.cancel_all(reason)

    reconnected: list[str] = []

    async def on_channel_lost(reason: str) -> None:
        await rig.lifecycle.cancel_all(reason)
        reconnected.append(reason)  # stands in for ws.force_reconnect()

    rig.h.feed.on_invalidate(on_invalidate)
    intake = RfqIntake(rig.h.ws)
    intake.on_channel_lost(on_channel_lost)

    t0 = rig.h.clock.now().timestamp()
    await rig.h.feed._handle_disconnect()  # noqa: SLF001 — the real WS-drop path
    feed_s = rig.h.clock.now().timestamp() - t0

    t1 = rig.h.clock.now().timestamp()
    await intake._handle_error({"msg": {"code": 25, "msg": "too slow"}})  # noqa: SLF001
    lost_s = rig.h.clock.now().timestamp() - t1

    budget_s = lifecycle_mod._WITHDRAW_RESOLVE_BUDGET_S  # noqa: SLF001
    print(
        f"\n  WS PATHS  (book {LIVE_MAX_OPEN_QUOTES}, write bucket EMPTY — the "
        f"worst case)\n"
        f"    feed _handle_disconnect -> _fire_invalidate -> cancel_all : "
        f"{feed_s * 1000:7.1f} ms  (bound {budget_s * 1000:.0f} ms)\n"
        f"    intake _handle_error    -> on_channel_lost -> cancel_all  : "
        f"{lost_s * 1000:7.1f} ms, then force_reconnect fired={bool(reconnected)}\n"
    )
    assert feed_s <= budget_s + 0.2
    assert lost_s <= budget_s + 0.2
    assert reconnected == ["ws_terminal_error_25"]
    # Everything not provably deleted is still ours, pending for the resolver.
    for state in rig.lifecycle._open.values():  # noqa: SLF001
        assert state.withdraw_pending_reason is not None


def test_only_the_terminal_callers_opt_out_of_the_wall_budget() -> None:
    """Which of the 7 call sites passes ``budget_s=None``: exactly the two
    genuinely terminal ones (``on_halt`` -> ``_stop.set()``, and shutdown),
    where there is no next maintenance tick to inherit a deferred withdrawal.
    The other five keep running and take the bound."""
    opted_out: list[tuple[str, int]] = []
    bounded: list[tuple[str, int]] = []
    for path in (SRC, QUOTE_APP_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cancel_all"
            ):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            budget = kw.get("budget_s")
            if isinstance(budget, ast.Constant) and budget.value is None:
                opted_out.append((path.name, node.lineno))
            else:
                bounded.append((path.name, node.lineno))
    assert len(opted_out) == 2, opted_out
    assert len(bounded) == 5, bounded
    assert all(p == "quote_app.py" for p, _ in opted_out)
    # ...and the default IS an existing primitive, not a fresh literal.
    sig = inspect.signature(lifecycle_mod.QuoteLifecycle.cancel_all)
    assert (
        sig.parameters["budget_s"].default == lifecycle_mod._WITHDRAW_RESOLVE_BUDGET_S
    )
    assert (
        lifecycle_mod._WITHDRAW_RESOLVE_BUDGET_S == lifecycle_mod._REPRICE_SWEEP_BUDGET_S
    )
