"""V5/V6/V7 — THE CONFIRM PATH, where a won auction turns into money or is thrown away.

  R4 (2026-07-27) the ACCEPTED-quote guard was a SNAPSHOT taken before the
     resolver's awaits. Accepts land on another task, and an ACCEPTED quote is
     not OPEN — so "absent from the open list" was the EXPECTED answer and
     reaping a FILLED quote was the DEFAULT outcome. Live: proven_gone=2 on a
     quote that then logged confirm_ok. The discriminating variable is the
     INTERLEAVING POINT, and it must be injected INSIDE the await.

  R5 (2026-07-27) the location/tail axis split made compute_book_risk ~16%
     slower and was benchmarked against the QUOTE path. The CONFIRM path has a
     hard 3 s exchange window and was never benchmarked. Live cost that day: 70
     candidate_gate_deadline events against 267 confirms, and every timeout was
     recorded as ``confirm.declined.decline_candidate_risk`` — a stopwatch
     wearing a risk reason code, invisible to every decline analysis.

  R5's CURE then produced its own defect in the working tree (the predictive
  deadline guard skipped straight to the deterministic fallback before the MC
  was ever attempted, and the fallback's rule is "latency is not a risk verdict
  -> confirm"). So V7 asserts BOTH directions: the MC must actually RUN when
  there is budget, and the taxonomy must be honest when there is not.
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import time
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.rfq.lifecycle import EXCHANGE_CONFIRM_WINDOW_S
from combomaker.risk.exposure import LegRef, OpenPosition
from tools.vitals.result import FAST, PRESHIP, Probe, Result, Vital

V5 = Vital(
    key="V5",
    incident="R4 accepted-quote guard TOCTOU (2026-07-27)",
    name="FUNNEL CONSERVATION — a quote never leaves by two exits",
    degraded="a taker ACCEPT lands INSIDE the resolver's open-quote read, on "
    "another task, while the confirm REST call is still open",
    money="a FILLED quote is reaped as a proven withdrawal: the fill is not "
    "booked, the position is invisible to every cap, and the book is wrong",
    tier=FAST,
)

V7 = Vital(
    key="V7",
    incident="R5 latency wearing a risk reason code (2026-07-27) + its cure's "
    "own regression",
    name="METRIC TRUTH — the MC runs when there is budget, and a timeout is "
    "never a risk verdict",
    degraded="(a) full budget  (b) the O(T^2) input build burns the whole "
    "window  (c) the deterministic caps refuse under a timeout",
    money="(a) a candidate the risk model REFUSES gets confirmed — we buy the "
    "concentration the gate exists to refuse; (b) timeouts are filed as risk "
    "declines and every decline analysis is corrupted",
    tier=FAST,
)

V6 = Vital(
    key="V6",
    incident="R5 location/tail axis split benchmarked on the WRONG path "
    "(2026-07-27) + B1: 96% of the gate's rho build was dead work",
    name="CONFIRM WINDOW — MC fits at the live book, build fits at every size",
    degraded="the live book at 1x / 3x / 5x its measured position count, with "
    "per-pair rho so the location and tail joints genuinely DIFFER, and the "
    "rho stage charged COLD (the first accept of a process)",
    money="either won auctions are discarded on our own clock (70 candidate_gate "
    "deadline events vs 267 confirms on the tape, all already won), or — worse — "
    "the joint-tail / P(ruin) / delta-P(book) gate never runs on the book we "
    "actually hold and every fill is admitted by the deterministic caps alone",
    tier=PRESHIP,
)


# --------------------------------------------------------------------------- #
# V5 — the interleaving.
# --------------------------------------------------------------------------- #


async def _v5(probe: Probe, d: Any) -> tuple[bool, str, str, str]:
    from tests.test_withdraw_accept_race import (
        _hold_confirm,
        _pend_both,
        _RacyLister,
    )
    from tests.test_withdraw_resolution import _FakeLister, _rig, _tick, _wire_lister

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rig = await _rig(tmp, "vitals-race.sqlite3", max_open_quotes=10, quotes=2)
        q1, q2 = list(rig.lifecycle._open)  # noqa: SLF001
        await _pend_both(rig, [q1, q2])
        gate = await _hold_confirm(rig)

        lister = _RacyLister(rig, q1, answer=set())  # the exchange lists NEITHER
        _wire_lister(rig, _FakeLister(set))
        rig.lifecycle._quote_lister = lister  # noqa: SLF001

        await _tick(rig)

        still_open = q1 in rig.lifecycle._open  # noqa: SLF001
        proven_gone = rig.metrics.counter("withdraw_resolve.proven_gone")
        deferred = rig.metrics.counter("withdraw_resolve.accepted_deferred")
        confirmed = list(rig.sender.confirmed)
        gate.set()
        if lister.task is not None:
            await lister.task
        await rig.lifecycle._store.close()  # noqa: SLF001

    # THE INVARIANT, and it needs no number: the set of quotes proven gone and
    # the set of quotes we confirmed are DISJOINT.
    reaped_a_confirm = (not still_open) and q1 in confirmed
    ok = still_open and not reaped_a_confirm
    probe.note(f"accept landed inside the read; quote still tracked = {still_open}")
    probe.note(
        f"withdraw_resolve.proven_gone={proven_gone}  accepted_deferred={deferred}  "
        f"confirm.sent={confirmed}"
    )
    return (
        ok,
        f"proven_gone n confirm.sent = "
        f"{'{' + q1 + '}' if reaped_a_confirm else 'EMPTY'}",
        "the two sets must be disjoint for every quote_id (0/1 invariant)",
        "" if ok else "a mid-confirm quote was REAPED as a proven withdrawal",
    )


# --------------------------------------------------------------------------- #
# V7 — the taxonomy, both directions.
# --------------------------------------------------------------------------- #


async def _v7(probe: Probe, d: Any) -> tuple[bool, str, str, str]:
    from combomaker.rfq.lifecycle import LifecycleConfig
    from tests.test_candidate_gate_wiring import (
        StubBookRiskPool,
        _make_rig,
        _verdict,
    )
    from tests.test_confirm_window_budget import _stall_the_gate, _warm_latency
    from tests.test_lifecycle import accepted_msg, rfq

    failures: list[str] = []

    # (a) FULL BUDGET: the MC must actually RUN, and a risk DECLINE must stand.
    with tempfile.TemporaryDirectory() as td:
        pool = StubBookRiskPool(_verdict(confirm=False, reason="post_ruin_prob_over_budget"))
        rig = await _make_rig(Path(td), pool=pool, db="vitals-gate-a.sqlite3")
        _warm_latency(rig.lifecycle)
        await rig.lifecycle.handle_rfq(rfq())
        await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
        mc_ran = bool(pool.calls)
        confirmed = list(rig.sender.confirmed)
        risk_declines = rig.metrics.counter(
            f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
        )
        probe.note(
            f"(a) full budget: MC ran={mc_ran}, confirmed={confirmed}, "
            f"decline_candidate_risk={risk_declines}"
        )
        if not mc_ran:
            failures.append(
                "(a) the candidate MC NEVER RAN with a full budget — the gate "
                "skipped to its latency fallback"
            )
        if confirmed:
            failures.append(
                "(a) a candidate the risk gate DECLINED (post_ruin_prob_over_budget) "
                "was CONFIRMED"
            )
        await rig.lifecycle._store.close()  # noqa: SLF001

    # (b) BUDGET BURNT BY THE BUILD: the auction is resolved from the
    #     deterministic caps, and whatever it resolves to, it is NOT filed as a
    #     risk verdict.
    with tempfile.TemporaryDirectory() as td:
        pool = StubBookRiskPool(_verdict(confirm=True))
        rig = await _make_rig(Path(td), pool=pool, db="vitals-gate-b.sqlite3")
        _warm_latency(rig.lifecycle)
        _stall_the_gate(rig.lifecycle)
        rig.lifecycle._config = LifecycleConfig(  # noqa: SLF001
            quote_ttl_s=30.0,
            reprice_threshold_cc=100,
            candidate_gate_enabled=True,
            candidate_gate_build_check_pairs=1,
        )
        await rig.lifecycle.handle_rfq(rfq())
        await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
        m = rig.metrics
        expired = m.counter("candidate_gate.window_expired_before_confirm")
        fb_confirm = m.counter("candidate_gate.timeout_fallback_confirm")
        fb_decline = m.counter("candidate_gate.timeout_fallback_decline")
        risk_declines = m.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}")
        timeout_declines = m.counter(
            f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_GATE_TIMEOUT}"
        )
        probe.note(
            f"(b) starved gate: window_expired={expired}, "
            f"timeout_fallback_confirm={fb_confirm}, "
            f"timeout_fallback_decline={fb_decline}, "
            f"decline_candidate_risk={risk_declines}, "
            f"decline_candidate_gate_timeout={timeout_declines}"
        )
        if risk_declines:
            failures.append(
                "(b) a LATENCY exit was recorded as decline_candidate_risk — a "
                "timeout wearing a risk reason code (R5 exactly)"
            )
        if not (fb_confirm or fb_decline or timeout_declines):
            failures.append(
                "(b) the budget expired but no latency-attributed counter moved — "
                "the exit is unattributed"
            )
        await rig.lifecycle._store.close()  # noqa: SLF001

    ok = not failures
    return (
        ok,
        f"MC-ran-on-full-budget={not any(f.startswith('(a)') for f in failures)}, "
        f"timeout-taxonomy-honest={not any(f.startswith('(b)') for f in failures)}",
        "decline_candidate_risk == 0 on any latency exit; pool.calls > 0 on any "
        "full-budget accept",
        "; ".join(failures),
    )


# --------------------------------------------------------------------------- #
# V6 — does the confirm-path MC fit in the exchange's window? (PRE-SHIP)
# --------------------------------------------------------------------------- #


def _scaled_live_book(rows: list[dict[str, Any]], mult: int) -> list[OpenPosition]:
    """The REAL open book, replicated ``mult`` times with each copy re-homed onto
    its own game suffix. Positions AND distinct tickers both scale, which is what
    a bigger book actually looks like — and the ticker count is what drives the
    O(T^2) rho resolution the live timeouts died in."""
    out: list[OpenPosition] = []
    for copy in range(mult):
        suffix = "" if copy == 0 else f"#{copy}"
        for row in rows:
            legs = tuple(
                LegRef(
                    market_ticker=f"{leg['market_ticker']}{suffix}",
                    event_ticker=f"{leg['event_ticker']}{suffix}",
                    side=leg["side"],
                )
                for leg in row["legs"]
            )
            if not legs:
                continue
            out.append(
                OpenPosition(
                    position_id=f"{row['position_id']}{suffix}",
                    combo_ticker=f"COMBO-{row['position_id']}{suffix}",
                    collection=None,
                    our_side=Side.NO if row["our_side"] == "no" else Side.YES,
                    contracts=CentiContracts(max(1, int(row["contracts_centi"]))),
                    entry_price_cc=int(row["entry_price_cc"]),
                    legs=legs,
                )
            )
    return out


def _live_rho_provider() -> Any:
    """The SHIPPED within-game rho provider, built from the LIVE config's
    correlation tables — the exact object the lifecycle resolves every pair
    through, memo and all."""
    import yaml

    from combomaker.ops.config import PricingConfig
    from combomaker.pricing.engine import PricingEngine
    from combomaker.sim.within_game_rho import sgp_within_game_rho_provider
    from tools.vitals.derive import _live_config_path
    from tools.vitals.v_quoting import CONVENTIONS

    with _live_config_path().open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    engine = PricingEngine(None, None, CONVENTIONS, PricingConfig(**(cfg.get("pricing") or {})))  # type: ignore[arg-type]
    return sgp_within_game_rho_provider(engine.sgp_params)


def check_confirm_window(probe: Probe, d: Any) -> Result:
    """Does the confirm path deliver a decision inside the exchange's window, and
    does the RISK GATE actually get to run on the book we trade?

    MEASURED IN THE SHIPPED COMPOSITION, and that is not a detail — the first
    version of this check measured a composition that does not ship (a rho rate
    times ALL ticker pairs, and the MC through the LIVE provider inline) and so
    could see neither the 96% waste nor the fix for it. Every stage below is the
    object the confirm path really runs:

      A  rho resolution over ``within_game_pair_tickers`` — the SHARED helper
         ``build_book_model`` itself groups with, so this benchmark cannot drift
         from the code. COLD (first accept of a process, the case the live
         timeouts died in) is what is charged.
      B  the rest of the input build: the marginal dict + the committed-only
         model the P(ruin) equity basis needs.
      C  ``_worker_candidate_book_risk`` with the DICT-backed providers — byte
         identically what the worker process runs.

    THREE ASSERTIONS, each one a thing that costs money when it is false:

      1. RISK COVERAGE AT THE LIVE BOOK. build + attempts x MC must fit the
         budget. When it does not, the joint-tail / P(ruin) / delta-P(book) gate
         never runs on the book we actually hold and every fill is admitted by
         the deterministic caps alone. Pre-2026-07-27 the build alone was
         3,855 ms before a single sample was drawn.
      2. THE BUILD FITS AT EVERY SIZE. The MC cannot start until the build
         finishes, so a build that cannot complete inside the budget makes the
         risk gate STRUCTURALLY DARK at that book size — the live "23 of 24
         losses never sampled once", at scale.
      3. NO UN-INTERRUPTIBLE STAGE EXCEEDS THE BUDGET ON ITS OWN. The build
         honours its deadline by CHECKING it between stages; a single stage
         longer than the whole budget can therefore overshoot the window no
         matter what the deadline says.

    What this check deliberately does NOT assert is that the MC fits at 3x/5x.
    It does not, and no scoping fixes that: at 500 positions the 20,000-sample MC
    is ~4.8 s on its own. The system's answer there is the bounded MC plus the
    deterministic fallback (V7 asserts that taxonomy), so the honest thing to
    publish is WHERE the MC stops fitting — which this prints — rather than a
    bound that no implementation can meet."""
    from combomaker.ops.pricing_pool import (
        CandidateBookRiskInputs,
        _worker_candidate_book_risk,
    )
    from combomaker.risk.exposure import ExposureBook
    from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits
    from combomaker.sim.book_model import build_book_model, within_game_pair_tickers
    from combomaker.sim.book_risk import evaluate_candidate_book_risk
    from tools.vitals.derive import live_open_positions
    from tools.vitals.v_quoting import CONVENTIONS

    sig = inspect.signature(evaluate_candidate_book_risk).parameters
    # The MC size the CONFIRM path really runs, read out of the live signature —
    # a code fact, not a number this gate picked.
    n_samples = int(sig["n_samples"].default)
    attempts = d.gate_attempts  # max attempt index seen on the tape, +1
    rows, source = live_open_positions()
    probe.note(f"1x book = the REAL open book: {len(rows)} positions [{source}]")

    # The reserve term we can MEASURE with no network: the deterministic cap
    # re-check that MUST still run after the gate gives up. The confirm RTT term
    # is NOT subtracted (no recorded RTT distribution exists on the tape), so
    # this bound is deliberately GENEROUS — a FAIL is unambiguous, a PASS is
    # necessary but not sufficient.
    det_book = ExposureBook(CONVENTIONS)
    for pos in _scaled_live_book(rows, 1):
        det_book.add_position(pos)
    checker = LimitChecker(RiskLimits())
    cand_det = _scaled_live_book(rows, 1)[0]
    t0 = time.perf_counter()
    for _ in range(5):
        checker.check(
            det_book,
            lambda t: 0.5,
            DailyPnl(),
            candidate_positions=[cand_det],
            risk_bankroll_cc=d.bankroll_cc,
        )
    det_ms = (time.perf_counter() - t0) / 5 * 1000
    window_ms = EXCHANGE_CONFIRM_WINDOW_S * 1000
    budget_ms = window_ms - det_ms

    rho = _live_rho_provider()
    failures: list[str] = []
    live_total = 0.0
    worst_build = 0.0
    worst_build_label = ""
    worst_stage = 0.0
    worst_stage_label = ""
    mc_fits_upto = ""
    for mult in (1, 3, 5):
        positions = _scaled_live_book(rows, mult)
        candidate = positions[0]
        committed = tuple(positions[1:])
        merged = (*committed, candidate)
        tickers = sorted({leg.market_ticker for p in positions for leg in p.legs})
        marginals = {t: 0.45 for t in tickers}
        all_pairs = len(tickers) * (len(tickers) - 1) // 2

        # --- STAGE A: rho, over the pair set the MODEL consumes, COLD --------
        # The pair set comes from the SHARED helper, so this cannot benchmark a
        # scoping the shipped build does not use.
        sg_pairs = within_game_pair_tickers(merged, marginals.__contains__)
        rho.invalidate_sgp_cache()
        t0 = time.perf_counter()
        rho_dict: dict[frozenset[str], tuple[float, float, float]] = {}
        for a, b in sg_pairs:
            band = rho(a, b)
            if band is not None:
                rho_dict[frozenset((a, b))] = band
        a_ms = (time.perf_counter() - t0) * 1000

        # --- STAGE B: marginals + the committed-only (P(ruin) basis) model ----
        t0 = time.perf_counter()
        build_book_model(
            list(committed),
            marginals=marginals.get,
            within_game_rho=rho,
        )
        b_ms = (time.perf_counter() - t0) * 1000
        build_ms = a_ms + b_ms

        # --- STAGE C: the MC, in the WORKER's own composition -----------------
        inputs = CandidateBookRiskInputs(
            committed=committed,
            candidate=candidate,
            reservations=(),
            marginals=marginals,
            within_game_rho_pairs=rho_dict,
            structural_cfg=None,
            n_samples=n_samples,
            seed=0,
            band="high",
            bankroll_cc=float(d.bankroll_cc),
            current_equity_cc=float(d.bankroll_cc),
            ruin_floor_frac=sig["ruin_floor_frac"].default,
            ruin_prob_ci_z=sig["ruin_prob_ci_z"].default,
            portfolio_cvar_frac=sig["portfolio_cvar_frac"].default,
            portfolio_det_max_frac=sig["portfolio_det_max_frac"].default,
            portfolio_ruin_prob_budget=sig["portfolio_ruin_prob_budget"].default,
            absolute_notional_multiple=sig["absolute_notional_multiple"].default,
            hedge_cost_budget_cc=sig["hedge_cost_budget_cc"].default,
            allow_negative_ev_hedge=sig["allow_negative_ev_hedge"].default,
        )
        times: list[float] = []
        for _rep in range(3):
            t1 = time.perf_counter()
            _worker_candidate_book_risk(inputs)
            times.append((time.perf_counter() - t1) * 1000)
        times.sort()
        mc_ms = times[len(times) // 2]

        total = build_ms + mc_ms * attempts
        if total <= budget_ms:
            mc_fits_upto = f"{mult}x ({len(positions)} positions)"
        if mult == 1:
            live_total = total
        if build_ms > worst_build:
            worst_build, worst_build_label = build_ms, f"{mult}x"
        # The un-interruptible stages: the build checks its deadline BETWEEN
        # stages (and every N pairs inside A), so B is the longest stretch that
        # cannot be abandoned once entered.
        if b_ms > worst_stage:
            worst_stage, worst_stage_label = b_ms, f"{mult}x"

        probe.note(
            f"{mult}x  n_pos={len(positions):4d}  tickers={len(tickers):4d}  "
            f"pairs {all_pairs:7,d} all -> {len(sg_pairs):6,d} same-game "
            f"({100.0 * (all_pairs - len(sg_pairs)) / max(1, all_pairs):5.2f}% "
            f"was dead work)   A(rho,cold) {a_ms:7.1f}ms + B(build) {b_ms:7.1f}ms "
            f"+ MC {mc_ms:7.1f}ms x{attempts} = {total:8.1f}ms = "
            f"{total / window_ms * 100:6.1f}% of the {EXCHANGE_CONFIRM_WINDOW_S:.1f}s "
            f"window   margin {budget_ms - total:+9.1f}ms"
        )

    if live_total > budget_ms:
        failures.append(
            f"the MC does NOT fit at the LIVE book ({live_total:.0f}ms vs "
            f"{budget_ms:.0f}ms) - the joint-tail/P(ruin) gate never runs on the "
            f"book we hold"
        )
    if worst_build > budget_ms:
        failures.append(
            f"the input build alone exceeds the budget at {worst_build_label} "
            f"({worst_build:.0f}ms vs {budget_ms:.0f}ms) - the MC is structurally "
            f"unreachable at that book size"
        )
    if worst_stage > budget_ms:
        failures.append(
            f"an UN-INTERRUPTIBLE stage exceeds the budget at {worst_stage_label} "
            f"({worst_stage:.0f}ms) - the build's deadline cannot be honoured "
            f"once that stage is entered"
        )

    probe.note(
        f"deterministic cap re-check (the reserve we CAN measure): {det_ms:.2f}ms"
    )
    probe.note(
        f"the MC fits inside the window up to {mc_fits_upto or 'NO book size'}; "
        f"beyond it the bounded MC times out and the deterministic caps resolve "
        f"the auction INSIDE the window (V7 asserts that taxonomy)"
    )
    probe.note(
        f"longest UN-INTERRUPTIBLE stage across all sizes: {worst_stage:.0f}ms at "
        f"{worst_stage_label} (the committed-only P(ruin)-basis model)"
    )
    probe.note(
        f"live cost on the tape: {d.tape['candidate_gate_deadline_events']} "
        f"candidate_gate_deadline events vs "
        f"{d.tape['candidate_gate_confirm_events']} confirms"
    )
    ok = not failures
    return probe.grade(
        ok,
        measured=f"live {live_total:.0f}ms ({live_total / window_ms * 100:.0f}% win), "
        f"build<={worst_build:.0f}ms",
        bound=f"live build+{attempts}xMC, AND the build at every size, AND every "
        f"un-interruptible stage < {window_ms:.0f}ms window - {det_ms:.1f}ms "
        f"measured det-check = {budget_ms:.0f}ms",
        detail="; ".join(failures),
    )


def check_accept_race(probe: Probe, d: Any) -> Result:
    ok, measured, bound, detail = asyncio.run(_v5(probe, d))
    return probe.grade(ok, measured=measured, bound=bound, detail=detail)


def check_metric_truth(probe: Probe, d: Any) -> Result:
    ok, measured, bound, detail = asyncio.run(_v7(probe, d))
    return probe.grade(ok, measured=measured, bound=bound, detail=detail)


CHECKS = [
    (V5, check_accept_race),
    (V7, check_metric_truth),
    (V6, check_confirm_window),
]
