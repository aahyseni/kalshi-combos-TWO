"""The 2026-07-27 MAINTENANCE STALL — an UNBOUNDED wait for write tokens inside
a bounded loop, and the UNFAIR bucket that let a waiter starve there.

What the tape says
------------------
The supervisor emergency-killed a HEALTHY bot: maintenance progress age 30.9 s
against its 30.5 s bound (``heartbeat_timeout_s`` + the 0.5 s tick), one tick
over. 4.2 s later that same bot withdrew all 67 quotes in 218 ms — the exchange
was reachable the whole time, so nothing about the exchange caused the stall.

The stall itself is a BUG, not a policy call:

  ``_delete_quote`` → ``_withdraw_and_reconcile`` (``budget_s`` defaulting to
  ``None`` = UNBOUNDED) → ``_withdraw_batch._one``'s token gate, which waited for
  bucket refill with no wall bound at all. It is called from INSIDE the reprice
  sweep, and the sweep checks its own 2.5 s wall budget only at the TOP of an
  iteration — so ONE starved delete holds the entire maintenance loop for as
  long as the bucket is dry, and the loop blows its progress bound from inside a
  single await.

Two defects, two fixes, pinned here:

 1. BOUNDED BY DEFAULT. ``_delete_quote`` defaults to the EXISTING withdrawal
    pass bound (``_WITHDRAW_RESOLVE_BUDGET_S`` = ``_REPRICE_SWEEP_BUDGET_S``),
    and the reprice sweep threads what is LEFT of its own budget into all three
    of its call sites. The ONLY unbounded withdrawal left is the genuinely
    terminal ``cancel_all(budget_s=None)`` from ``on_halt`` / shutdown, where
    there IS no next tick to inherit the leftovers — preserved and pinned below.
 2. FAIR ADMISSION. A token bucket is not a queue: whoever calls ``try_spend``
    when tokens exist wins them, so a waiter sleeping on ``seconds_until`` is
    beatable forever by later arrivals. ``_spend_withdraw_tokens`` puts an
    ``asyncio.Lock`` (FIFO waiters) around the gate.

RESIDUAL RISK, ACCEPTED: a budget-deferred delete leaves a quote resting up to
one extra 0.5 s maintenance tick, where it can fill. Same exposure the batch path
already accepts; bounded by last look at confirm; the quote keeps
``withdraw_pending_reason``, keeps counting for risk in the mirror, and
``_resolve_withdraw_pending`` re-drives it next tick. Pinned by
``test_a_budget_deferred_delete_stays_in_the_mirror_and_in_exposure``.

TIME IN THESE TESTS is the injected ``FakeClock``: ``asyncio.sleep`` is patched
to ADVANCE it and yield, so a 30 s token wait costs no wall time and the measured
tick durations are exact, not timing-flaky. That is also the clock the progress
ledger and every budget in the withdrawal path read.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import DELETE_QUOTE_TOKEN_COST
from combomaker.ops.config import SupervisorConfig as SupervisorKnobs
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import MAINTENANCE_TICK_INTERVAL_S
from combomaker.ops.write_budget import WriteBudget
from combomaker.rfq import lifecycle as lifecycle_mod
from tests.test_filters import Harness
from tests.test_lifecycle import Rig
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

# The maintenance loop's DERIVED stall bound — ``ProgressLedger.register``:
# the operator's ONE wedge-tolerance anchor plus the loop's own cadence. Read
# from the config object, exactly as the bot derives it, so this test measures
# against whatever the anchor is set to (the SHIPPED DEFAULT here; the live
# prod YAML raises heartbeat_timeout_s to 30.0, which is the 30.5 s bound the
# 07-27 kill measured 30.9 s against). The property under test is scale-free.
STALL_AFTER_S = SupervisorKnobs().heartbeat_timeout_s + MAINTENANCE_TICK_INTERVAL_S

# The two EXISTING whole-pass wall budgets a bounded tick may spend: the
# withdraw-resolver's pass at the top of the tick, then the reprice sweep's.
BOUNDED_TICK_CEILING_S = (
    lifecycle_mod._WITHDRAW_RESOLVE_BUDGET_S  # noqa: SLF001 (module constant)
    + lifecycle_mod._REPRICE_SWEEP_BUDGET_S  # noqa: SLF001
)


def _starved_budget(clock: Any) -> WriteBudget:
    """A bucket that holds EXACTLY one delete and refills it no faster than the
    maintenance loop's own stall bound. Both numbers are borrowed, not invented:
    the cost is the exchange's documented ``DELETE_QUOTE_TOKEN_COST`` and the
    window is the loop's derived progress bound — so "empty bucket" means
    "cannot pay for one delete before the supervisor calls this loop wedged",
    which is the incident's condition stated as arithmetic."""
    return WriteBudget.create(
        clock, capacity=DELETE_QUOTE_TOKEN_COST, refill_s=STALL_AFTER_S
    )


async def _settle(rounds: int = 6) -> None:
    """Let every pending task reach its next await (task creation inside
    ``asyncio.gather`` costs a scheduling round of its own)."""
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.fixture()
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``asyncio.sleep`` ADVANCES the attached FakeClock instead of real time.

    The token gate derives every wait from the bucket's own rate on the injected
    clock, so simulating the wait keeps the measurement exact while the test runs
    in microseconds. Still yields to the event loop, so task interleaving (the
    fairness tests) is real. Returns ``attach(clock)``."""
    real_sleep = asyncio.sleep
    clocks: list[Any] = []

    async def _sleep(delay: float = 0.0, *args: Any, **kwargs: Any) -> Any:
        if delay > 0.0 and clocks:
            clocks[0].advance(delay)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    def attach(clock: Any) -> None:
        clocks.append(clock)

    return attach


async def _rig(tmp_path: Path, db: str, quotes: int) -> tuple[Rig, list[str]]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    rig = Rig(h, store)
    for i in range(quotes):
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"rfq_{i + 1}"))
    ids = list(rig.lifecycle._open)  # noqa: SLF001 (test seam)
    assert len(ids) == quotes, f"rig did not post {quotes} quotes: {ids}"
    return rig, ids


async def _stalling_tick(
    tmp_path: Path,
    db: str,
    *,
    unbounded: bool,
    monkeypatch: pytest.MonkeyPatch,
    attach: Any,
) -> tuple[Rig, float, list[str]]:
    """ONE maintenance tick under a dry bucket, in the incident's shape: a quote
    left withdraw-PENDING by the previous tick (the resolver's drain) plus a
    TTL-expired quote (the reprice sweep's delete). Returns the tick's duration
    in the clock the supervisor judges progress on."""
    rig, ids = await _rig(tmp_path, db, 2)
    h = rig.h
    lc = rig.lifecycle

    # Wire the fake clock into the patched sleep, then age the book past TTL.
    attach(h.clock)
    h.clock.advance(lc._config.quote_ttl_s + 1.0)  # noqa: SLF001

    # A quote whose withdrawal is already UNKNOWN — the resolver's drain owns it.
    pending, ttl_expired = ids
    state = lc._open[pending]  # noqa: SLF001
    state.withdraw_pending_reason = ReasonCode.DELETE_TTL_EXPIRED
    state.withdraw_asked_mono_ns = h.clock.monotonic_ns()

    # DRY BUCKET, after every clock advance above (a refill would undo it).
    budget = _starved_budget(h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST), "bucket must start full"
    assert budget.tokens == 0
    lc._withdraw_budget = budget  # noqa: SLF001

    if unbounded:
        # THE PRE-FIX CODE, exactly: `_delete_quote` passed no budget and
        # inherited `_withdraw_and_reconcile`'s unbounded default.
        original = lifecycle_mod.QuoteLifecycle._delete_quote

        async def _pre_fix(
            self: Any, quote_id: str, reason: ReasonCode, **_kw: Any
        ) -> None:
            await original(self, quote_id, reason, budget_s=None)

        monkeypatch.setattr(
            lifecycle_mod.QuoteLifecycle, "_delete_quote", _pre_fix
        )

    start_ns = h.clock.monotonic_ns()
    await lc.maintenance_tick()
    elapsed_s = (h.clock.monotonic_ns() - start_ns) / 1e9
    return rig, elapsed_s, [pending, ttl_expired]


# ==========================================================================
# 1 — a starved bucket no longer stalls the maintenance loop past its bound
# ==========================================================================


async def test_before_the_fix_one_starved_delete_stalls_the_tick_past_the_bound(
    tmp_path: Path, fake_sleep: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEFORE. The unbounded token wait sits inside the sweep's iteration, and
    the sweep only checks its wall budget at the TOP of one — so the tick runs
    past the 30.5 s progress bound the supervisor kills on, with the exchange
    perfectly reachable and one quote's DELETE the only thing outstanding."""
    _rig_, elapsed_s, _ids = await _stalling_tick(
        tmp_path,
        "stall-before.sqlite3",
        unbounded=True,
        monkeypatch=monkeypatch,
        attach=fake_sleep,
    )
    assert elapsed_s > STALL_AFTER_S, (
        f"expected the pre-fix tick to breach the {STALL_AFTER_S}s progress "
        f"bound; measured {elapsed_s}s"
    )
    print(f"\nBEFORE: maintenance tick = {elapsed_s:.3f}s  (bound {STALL_AFTER_S}s)")


async def test_after_the_fix_the_same_tick_stays_inside_its_progress_bound(
    tmp_path: Path, fake_sleep: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AFTER. Same dry bucket, same two quotes, same tick — now bounded by the
    two EXISTING whole-pass budgets (resolver + sweep) and comfortably inside the
    progress bound. Nothing is abandoned: see the residual-risk test below."""
    rig, elapsed_s, ids = await _stalling_tick(
        tmp_path,
        "stall-after.sqlite3",
        unbounded=False,
        monkeypatch=monkeypatch,
        attach=fake_sleep,
    )
    assert elapsed_s <= BOUNDED_TICK_CEILING_S, (
        f"tick spent {elapsed_s}s against the two pass budgets' "
        f"{BOUNDED_TICK_CEILING_S}s"
    )
    assert elapsed_s < STALL_AFTER_S
    # The tick made PROGRESS on the thing it is judged for: it is still holding
    # both quotes, both marked for the next tick's re-drive.
    for quote_id in ids:
        assert quote_id in rig.lifecycle._open  # noqa: SLF001
    print(
        f"\nAFTER:  maintenance tick = {elapsed_s:.3f}s  "
        f"(pass budgets {BOUNDED_TICK_CEILING_S}s, bound {STALL_AFTER_S}s)"
    )


# ==========================================================================
# 2 — the residual risk, stated: deferred is NOT abandoned
# ==========================================================================


async def test_a_budget_deferred_delete_stays_in_the_mirror_and_in_exposure(
    tmp_path: Path, fake_sleep: Any
) -> None:
    """The price of the bound. A delete the budget defers was NEVER ASKED, so
    the quote is still resting and can still fill — and it is treated exactly
    like every other not-provably-withdrawn quote: it stays in the mirror, it
    stays in the EXPOSURE book (risk keeps counting it), and it carries the
    withdraw-pending reason the 0.5 s resolver re-drives it on."""
    rig, ids = await _rig(tmp_path, "deferred.sqlite3", 1)
    quote_id = ids[0]
    lc = rig.lifecycle
    attach = fake_sleep
    attach(rig.h.clock)

    budget = _starved_budget(rig.h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST)
    lc._withdraw_budget = budget  # noqa: SLF001

    await lc._delete_quote(quote_id, ReasonCode.DELETE_TTL_EXPIRED)  # noqa: SLF001

    assert rig.sender.deleted == [], "a deferred delete must never reach the wire"
    assert quote_id in lc._open  # noqa: SLF001
    assert quote_id in lc._exposure.open_quotes  # noqa: SLF001 — risk counts it
    state = lc._open[quote_id]  # noqa: SLF001
    assert state.withdraw_pending_reason is ReasonCode.DELETE_TTL_EXPIRED
    assert state.withdraw_asked_mono_ns > 0
    assert rig.metrics.counter("quote.delete_deferred") == 1
    assert rig.metrics.counter("quote.deleted.delete_ttl_expired") == 0

    # …and the very next tick re-drives it once the bucket has paid up.
    rig.h.clock.advance(STALL_AFTER_S)
    await lc.maintenance_tick()
    assert rig.sender.deleted == [quote_id]
    assert quote_id not in lc._open  # noqa: SLF001
    assert rig.metrics.counter("quote.deleted.delete_ttl_expired") == 1


# ==========================================================================
# 3 — the ONE unbounded path that must stay unbounded
# ==========================================================================


def test_only_the_terminal_cancel_all_callers_ask_for_an_unbounded_withdrawal() -> None:
    """``budget_s=None`` is the unbounded pass. It is correct ONLY where there is
    no next tick to inherit the leftovers: ``on_halt`` (which then sets ``_stop``)
    and shutdown. This pins the census — a new ``budget_s=None`` anywhere in the
    quote path re-opens the 07-27 stall and must fail here first."""
    # Read the AST, not the text: comments and docstrings discuss the unbounded
    # pass at length, and only a CALL can actually take it.
    def _unbounded_calls(path: str) -> list[str]:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        out: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "budget_s"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is None
                ):
                    out.append(ast.unparse(node.func))
        return out

    assert _unbounded_calls("src/combomaker/ops/quote_app.py") == [
        "lifecycle.cancel_all",  # on_halt → _stop.set(): no next tick
        "lifecycle.cancel_all",  # shutdown: loops already cancelled
    ]
    # And no call INSIDE the lifecycle asks for it — the only
    # ``budget_s: float | None = None`` left there are SIGNATURE defaults on the
    # batch helpers, which every caller now overrides.
    assert _unbounded_calls("src/combomaker/rfq/lifecycle.py") == []


async def test_the_terminal_cancel_all_still_waits_for_the_bucket(
    tmp_path: Path, fake_sleep: Any
) -> None:
    """The halt path's CANCEL half is load-bearing: it must attempt EVERY quote,
    waiting on the write bucket for as long as that takes, because nothing runs
    after it. A dry bucket therefore does NOT defer here — it waits, and every
    quote comes off the wire."""
    rig, ids = await _rig(tmp_path, "terminal.sqlite3", 2)
    lc = rig.lifecycle
    attach = fake_sleep
    attach(rig.h.clock)

    budget = _starved_budget(rig.h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST)
    lc._withdraw_budget = budget  # noqa: SLF001

    start_ns = rig.h.clock.monotonic_ns()
    await lc.cancel_all(ReasonCode.HALT_MANUAL, budget_s=None)
    waited_s = (rig.h.clock.monotonic_ns() - start_ns) / 1e9

    assert sorted(rig.sender.deleted) == sorted(ids)
    assert lc._open == {}  # noqa: SLF001
    assert waited_s > BOUNDED_TICK_CEILING_S, (
        "the terminal pass must be free to wait past any tick budget; it waited "
        f"{waited_s}s"
    )
    assert rig.metrics.counter("quote.delete_deferred") == 0


async def test_the_bounded_cancel_all_default_is_unchanged(
    tmp_path: Path, fake_sleep: Any
) -> None:
    """…and the NON-terminal callers still get the bounded pass they got before
    this fix — this change adds a bound to `_delete_quote`, it does not move
    `cancel_all`'s."""
    rig, ids = await _rig(tmp_path, "bounded-cancel.sqlite3", 2)
    lc = rig.lifecycle
    attach = fake_sleep
    attach(rig.h.clock)
    budget = _starved_budget(rig.h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST)
    lc._withdraw_budget = budget  # noqa: SLF001

    start_ns = rig.h.clock.monotonic_ns()
    await lc.cancel_all(ReasonCode.DECLINE_FILL_VELOCITY)
    waited_s = (rig.h.clock.monotonic_ns() - start_ns) / 1e9

    assert waited_s <= lifecycle_mod._WITHDRAW_RESOLVE_BUDGET_S  # noqa: SLF001
    assert rig.sender.deleted == []
    for quote_id in ids:  # deferred, never abandoned
        assert lc._open[quote_id].withdraw_pending_reason is not None  # noqa: SLF001


# ==========================================================================
# 4 — FAIRNESS: no waiter starves while later arrivals win the bucket
# ==========================================================================


async def test_a_later_arrival_cannot_take_the_tokens_a_waiter_is_sleeping_for(
    tmp_path: Path, fake_sleep: Any
) -> None:
    """THE STARVATION. A token bucket is not a queue. The victim (A) finds it
    empty and sleeps on ``seconds_until``; while it sleeps the bucket refills;
    a task that arrives LATER (B) calls ``try_spend`` on entry and takes exactly
    those tokens. Repeat and A never runs.

    With FIFO admission B queues BEHIND A, so A — which asked first — is served
    first, no matter how many later arrivals there are."""
    rig, ids = await _rig(tmp_path, "fair.sqlite3", 3)
    lc = rig.lifecycle
    attach = fake_sleep
    attach(rig.h.clock)
    budget = _starved_budget(rig.h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST)
    lc._withdraw_budget = budget  # noqa: SLF001

    victim, *latecomers = ids

    # A arrives first and blocks on the dry bucket.
    a = asyncio.create_task(lc._withdraw_batch([victim]))  # noqa: SLF001
    await _settle()  # let A reach the gate and start waiting

    # …now the latecomers arrive, one at a time, each fully scheduled.
    others = []
    for quote_id in latecomers:
        others.append(asyncio.create_task(lc._withdraw_batch([quote_id])))  # noqa: SLF001
        await _settle()

    await asyncio.gather(a, *others)

    assert rig.sender.deleted[0] == victim, (
        "the FIRST waiter was overtaken by a later arrival — this is the "
        f"starvation the gate exists to prevent: {rig.sender.deleted}"
    )
    assert sorted(rig.sender.deleted) == sorted(ids)


async def test_admission_is_first_come_first_served_under_contention(
    tmp_path: Path, fake_sleep: Any
) -> None:
    """The general property: under a bucket that can only pay for one delete at
    a time, every waiter is served, in ARRIVAL order, and none is skipped."""
    rig, ids = await _rig(tmp_path, "fifo.sqlite3", 4)
    lc = rig.lifecycle
    attach = fake_sleep
    attach(rig.h.clock)
    budget = _starved_budget(rig.h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST)
    lc._withdraw_budget = budget  # noqa: SLF001

    tasks = []
    for quote_id in ids:
        tasks.append(asyncio.create_task(lc._withdraw_batch([quote_id])))  # noqa: SLF001
        await _settle()
    await asyncio.gather(*tasks)

    assert rig.sender.deleted == ids, (
        f"admission order {rig.sender.deleted} != arrival order {ids}"
    )


async def test_a_bounded_waiter_queued_behind_an_unbounded_one_still_gives_up(
    tmp_path: Path, fake_sleep: Any
) -> None:
    """The gate must not become the stall by another door. Fairness means a
    bounded caller can end up QUEUED behind the terminal unbounded pass — so the
    LOCK wait is bounded by the caller's budget too, and the bounded caller
    defers on time instead of inheriting an unbounded wait."""
    rig, ids = await _rig(tmp_path, "queued.sqlite3", 2)
    lc = rig.lifecycle
    attach = fake_sleep
    attach(rig.h.clock)
    budget = _starved_budget(rig.h.clock)
    assert budget.try_spend(DELETE_QUOTE_TOKEN_COST)
    lc._withdraw_budget = budget  # noqa: SLF001

    terminal, bounded = ids
    holder = asyncio.create_task(
        lc._withdraw_batch([terminal], budget_s=None)  # noqa: SLF001
    )
    await _settle()  # holder owns the gate and is waiting on refill

    _gone, _unresolved, deferred = await lc._withdraw_batch(  # noqa: SLF001
        [bounded], budget_s=lifecycle_mod._REPRICE_SWEEP_BUDGET_S  # noqa: SLF001
    )
    assert deferred == [bounded], "a bounded waiter inherited an unbounded wait"
    await holder
    assert rig.sender.deleted == [terminal]
