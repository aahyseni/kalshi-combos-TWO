"""CONFIRM-WINDOW REBUILD (2026-07-27): a won auction is never lost to our own clock.

THE DEFECT. The candidate gate ran on a TYPED 1.0s budget and its expiry resolved
to DECLINE. Live, that discarded 70 already-won auctions over two days (254.0
contracts, $110.33 of our own premium, $479.21 of taker target cost) — and 23 of
24 losses in one session never ran a single Monte Carlo sample: the O(T^2) INPUT
BUILD alone blew the budget. Every one was recorded as
``confirm.declined.decline_candidate_risk``, a stopwatch wearing a risk reason
code, which is why no decline analysis ever surfaced it. It was self-stalling:
each fill grew the book, which grew the build, which timed out the next fill,
until the loss rate on won auctions was 100%.

WHAT THIS FILE PINS (the operator's five constraints, in order):

  1. A TIMEOUT IS NOT A DECLINE. On expiry the gate resolves from state we
     ALREADY have — the held reservation + the ENFORCED DETERMINISTIC caps,
     re-checked against the live book. It CONFIRMS when they allow and DECLINES
     when they do not. No Monte Carlo is involved either way.
  2. The cost is moved OFF the critical path where it can be: the rho
     resolution is SCOPED to the same-game pairs ``build_book_model`` actually
     consumes (96.0% of the old all-pairs work was computed and discarded) and
     memoised as the pure function it is (``test_within_game_rho_memo.py``).
     What remains is BOUNDED, never predicted — the build is interruptible and
     the MC carries the window that is actually left as its deadline. There is
     no cost estimator, and therefore no measurement history that can switch the
     risk gate off (section 6).
  3. EVERY BOUND IS DERIVED: the budget is the exchange's own confirm window
     minus the time already burnt since the accept minus a MEASURED reserve
     (confirm RTT high quantile + its observed dispersion + the measured cost of
     the deterministic check). It MOVES when the measured latency moves.
  4. THE SAFETY PROPERTY HOLDS: last look exists because the book can change
     between quote and accept — so the deterministic re-check at confirm is
     MANDATORY, and a book that changed under the gate can still decline.
  5. NO MISLABELLING: a latency-degraded refusal records
     ``DECLINE_CANDIDATE_GATE_TIMEOUT``, never ``DECLINE_CANDIDATE_RISK``.
"""

from __future__ import annotations

from pathlib import Path

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.pricing_pool import CandidateBookRiskInputs
from combomaker.rfq.lifecycle import (
    EXCHANGE_CONFIRM_WINDOW_NS,
    LifecycleConfig,
    _GateBudgetExceeded,
)
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_candidate_gate_atomic import (
    ScriptedPool,
    _held,
    _make,
    _verdict,
)
from tests.test_lifecycle import accepted_msg, rfq
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo

# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _committed(pid: str, contracts: int = 100) -> OpenPosition:
    """A committed book position on the SAME real combo ``_held`` uses, so it
    decomposes with known marginals and passes the real limit machinery."""
    return OpenPosition(
        position_id=pid,
        combo_ticker="KXMVE-C1",
        collection="KXMVESPORTS",
        our_side=Side.YES,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(3_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
        risk_modeled=True,
    )


def _same_game_holding(pid: str = "stall-pair") -> OpenPosition:
    """A MINIMAL holding whose two legs share a game (both under ``E1``), so the
    gate's input build has a real within-game pair to resolve rho for.

    Needed since the 2026-07-27 pair scoping: the build now resolves rho ONLY for
    pairs that SHARE A GAME (the 96%-waste fix), and this file's fixture combo is
    deliberately CROSS-EVENT (E1 x E2) — so a book of nothing but that combo
    resolves exactly ZERO rho pairs. Without this, a stall installed on the rho
    provider would never fire and every test below would silently pass while
    testing nothing. 1 centi-contract at 1 cc is 0.01 contracts of $0.0001
    premium: inert to every cap these tests exercise."""
    return OpenPosition(
        position_id=pid,
        combo_ticker="KXMVE-C1",
        collection="KXMVESPORTS",
        our_side=Side.YES,
        contracts=CentiContracts(1),
        entry_price_cc=CentiCents(1),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "no")),
        risk_modeled=True,
    )


def _stall_the_gate(lifecycle, seconds: float = 5.0, *, on_first=None):
    """Artificially SLOW the gate's input build: every rho pair burns wall time on
    the injected fake clock. This is the live failure mode reproduced exactly —
    the build, not the MC, is what ran out of window.

    Seeds ``_same_game_holding`` first so there IS a rho pair to resolve (see
    that helper: after the pair scoping, a cross-event-only book resolves none).
    The stall stays on the rho provider rather than on the marginal resolution
    because ``_marginals`` is also called on the ACCEPT path before the gate
    starts — stalling it would burn the whole confirm window before the gate is
    even entered, and the test would no longer exercise the interruptible build.

    ``on_first`` fires once, INSIDE the stalled build — the honest simulation of
    "the book moved while we were slow", which is precisely the case the
    deterministic re-check has to catch."""
    lifecycle._exposure.add_position(_same_game_holding())  # noqa: SLF001
    original = lifecycle._within_game_rho  # noqa: SLF001
    fired = []

    def slow_rho(a: str, b: str):
        if on_first is not None and not fired:
            fired.append(True)
            on_first()
        lifecycle._clock.advance(seconds)  # noqa: SLF001
        return None if original is None else original(a, b)

    lifecycle._within_game_rho = slow_rho  # noqa: SLF001


def _warm_latency(lifecycle, rtt_ms: float = 5.0, samples: int = 20) -> None:
    """Give the derived budget a MEASURED confirm round trip to work from."""
    for _ in range(samples):
        lifecycle._metrics.observe_ms("confirm.rtt_ms", rtt_ms)  # noqa: SLF001
        lifecycle._metrics.observe_ms(  # noqa: SLF001
            "confirm.deterministic_check_ms", 0.2
        )


# --------------------------------------------------------------------------- #
# 1. A GATE THAT EXCEEDS ITS BUDGET STILL CONFIRMS WHEN THE CAPS ALLOW.        #
# --------------------------------------------------------------------------- #


async def test_over_budget_gate_confirms_when_deterministic_caps_allow(
    tmp_path: Path,
) -> None:
    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    _warm_latency(lifecycle)
    _stall_the_gate(lifecycle)
    lifecycle._config = LifecycleConfig(  # noqa: SLF001
        quote_ttl_s=30.0,
        reprice_threshold_cc=100,
        candidate_gate_enabled=True,
        candidate_gate_build_check_pairs=1,  # check the deadline every pair
    )

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    # The MC never ran (the build was abandoned at its deadline) …
    assert pool.calls == []
    assert lifecycle._metrics.counter("candidate_gate.build_aborted") == 1  # noqa: SLF001
    # … and yet the auction we already WON was CONFIRMED, on the deterministic
    # caps, and its reservation committed. This is the whole fix.
    assert sender.confirmed == ["q1"]
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.timeout_fallback_confirm"
    ) == 1
    assert not reservation.is_outstanding("fill:q1")  # committed, not released
    assert "fill:q1" in exposure.positions


# --------------------------------------------------------------------------- #
# 2. …AND STILL DECLINES WHEN THEY DO NOT — WITH THE DISTINCT REASON CODE.     #
# --------------------------------------------------------------------------- #


async def test_over_budget_gate_declines_when_deterministic_caps_refuse(
    tmp_path: Path,
) -> None:
    # The gross-notional wall is ALWAYS-ENFORCED (never shadow-split). The
    # reservation is granted against an empty book, and only THEN — while the
    # gate is stalled — the book grows past the wall.
    checker = LimitChecker(
        RiskLimits(caps_shadow_mode=True, max_gross_notional_dollars=50.0)
    )
    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, sender, exposure, reservation = await _make(
        tmp_path, pool=pool, limits=checker, db="budget_decline.sqlite3"
    )
    _warm_latency(lifecycle)
    _stall_the_gate(
        lifecycle,
        on_first=lambda: [
            exposure.add_position(_committed(f"late{i}", contracts=10_000))
            for i in range(4)
        ],
    )
    lifecycle._config = LifecycleConfig(  # noqa: SLF001
        quote_ttl_s=30.0,
        reprice_threshold_cc=100,
        candidate_gate_enabled=True,
        candidate_gate_build_check_pairs=1,
    )

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert pool.calls == []  # the MC never ran — this is the timeout path
    assert sender.confirmed == []  # and the caps refused
    # THE DISTINCT REASON CODE: a latency-degraded refusal is NOT a risk refusal.
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_GATE_TIMEOUT}"
    ) == 1
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 0
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.timeout_fallback_decline"
    ) == 1
    # Headroom never lingers for a fill we are not making.
    assert not reservation.is_outstanding("fill:q1")
    assert "fill:q1" not in exposure.positions


# --------------------------------------------------------------------------- #
# 3. THE DECISION IS CORRECT AT BOOK SIZES 10 / 35 / 80, GATE ARTIFICIALLY     #
#    SLOWED. (Live, the gate crossed its budget between 49 and 55 positions and #
#    the loss rate went to 100% — deterministic, not stochastic.)              #
# --------------------------------------------------------------------------- #


async def test_decision_is_correct_at_every_book_size_with_a_slow_gate(
    tmp_path: Path,
) -> None:
    for n_positions in (10, 35, 80):
        # --- caps ALLOW ⇒ CONFIRM ------------------------------------------
        pool = ScriptedPool([_verdict(confirm=True)])
        lifecycle, sender, exposure, reservation = await _make(
            tmp_path, pool=pool, db=f"size_ok_{n_positions}.sqlite3"
        )
        _warm_latency(lifecycle)
        _stall_the_gate(lifecycle)
        lifecycle._config = LifecycleConfig(  # noqa: SLF001
            quote_ttl_s=30.0,
            reprice_threshold_cc=100,
            candidate_gate_enabled=True,
            candidate_gate_build_check_pairs=1,
        )
        for i in range(n_positions):
            exposure.add_position(_committed(f"ok{i}", contracts=1))
        await lifecycle.handle_rfq(rfq())
        await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
        assert sender.confirmed == ["q1"], f"n={n_positions}: won auction lost"
        assert pool.calls == [], f"n={n_positions}: MC should not have run"

        # --- caps REFUSE ⇒ DECLINE, same book size, same slow gate ----------
        # The wall sits just above the book the reservation was granted against;
        # the book crosses it WHILE the gate is stalled.
        checker = LimitChecker(
            RiskLimits(
                caps_shadow_mode=True,
                max_gross_notional_dollars=float(n_positions) + 20.0,
            )
        )
        pool2 = ScriptedPool([_verdict(confirm=True)])
        lifecycle2, sender2, exposure2, reservation2 = await _make(
            tmp_path,
            pool=pool2,
            limits=checker,
            db=f"size_no_{n_positions}.sqlite3",
        )
        # ORDER MATTERS, and it is the real semantics: the leg universe places a
        # ticker by its FIRST occurrence, so ``_same_game_holding`` (which
        # ``_stall_the_gate`` seeds) has to land BEFORE the cross-event
        # ``_committed`` rows — otherwise M2 is keyed to E2 by those and there is
        # no same-game pair left for the stall to fire on.
        _warm_latency(lifecycle2)
        _stall_the_gate(
            lifecycle2,
            on_first=lambda ex=exposure2: [
                ex.add_position(_committed(f"late{i}", contracts=10_000))
                for i in range(4)
            ],
        )
        for i in range(n_positions):
            exposure2.add_position(_committed(f"no{i}", contracts=100))
        lifecycle2._config = LifecycleConfig(  # noqa: SLF001
            quote_ttl_s=30.0,
            reprice_threshold_cc=100,
            candidate_gate_enabled=True,
            candidate_gate_build_check_pairs=1,
        )
        await lifecycle2.handle_rfq(rfq())
        await lifecycle2.on_quote_accepted(accepted_msg("q1", "yes"))
        assert sender2.confirmed == [], f"n={n_positions}: breached book confirmed"
        assert lifecycle2._metrics.counter(  # noqa: SLF001
            f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_GATE_TIMEOUT}"
        ) == 1, f"n={n_positions}"


# --------------------------------------------------------------------------- #
# 4. THE DEADLINE ADAPTS AS MEASURED LATENCY MOVES.                            #
# --------------------------------------------------------------------------- #


async def test_budget_is_derived_and_moves_with_measured_latency(
    tmp_path: Path,
) -> None:
    lifecycle, _sender, _exposure, _reservation = await _make(
        tmp_path, pool=None, db="budget_adapts.sqlite3"
    )
    now = lifecycle._clock.monotonic_ns()  # noqa: SLF001

    # UNMEASURED ⇒ worst case: the whole window is reserved, the budget is zero,
    # and the accept resolves on the deterministic caps (a refinement is lost,
    # never an auction).
    assert lifecycle._confirm_window_reserve_ns() == EXCHANGE_CONFIRM_WINDOW_NS  # noqa: SLF001
    assert lifecycle._candidate_gate_budget_ns(now) == 0  # noqa: SLF001

    # A FAST link: small reserve, nearly the whole window is available to the MC.
    _warm_latency(lifecycle, rtt_ms=1.0, samples=50)
    fast_reserve = lifecycle._confirm_window_reserve_ns()  # noqa: SLF001
    fast_budget = lifecycle._candidate_gate_budget_ns(now)  # noqa: SLF001
    assert 0 < fast_reserve < EXCHANGE_CONFIRM_WINDOW_NS
    assert fast_budget > 2_500_000_000  # > 2.5s of the 3s window

    # The link DEGRADES — measured, not configured. The reserve grows and the
    # budget shrinks, continuously. Nobody edited a number.
    for _ in range(200):
        lifecycle._metrics.observe_ms("confirm.rtt_ms", 900.0)  # noqa: SLF001
    slow_reserve = lifecycle._confirm_window_reserve_ns()  # noqa: SLF001
    slow_budget = lifecycle._candidate_gate_budget_ns(now)  # noqa: SLF001
    assert slow_reserve > fast_reserve
    assert slow_budget < fast_budget

    # And time ALREADY BURNT since the accept is deducted, because the budget is
    # anchored on the accept — not on the gate's own start. Everything the accept
    # path spent first (last look, fill velocity, the reservation, the waiver)
    # comes out automatically; there is no static split to keep in sync by hand.
    earlier = now - int(1.0e9)
    assert lifecycle._candidate_gate_budget_ns(earlier) < slow_budget - 900_000_000  # noqa: SLF001

    # Degrade past the whole window ⇒ the budget floors at zero (never negative).
    for _ in range(2_000):
        lifecycle._metrics.observe_ms("confirm.rtt_ms", 4_000.0)  # noqa: SLF001
    assert lifecycle._candidate_gate_budget_ns(now) == 0  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 5. A BOOK THAT CHANGED BETWEEN SEND AND ACCEPT IS RE-VALIDATED AND CAN STILL #
#    DECLINE. (The safety property last look exists for.)                      #
# --------------------------------------------------------------------------- #


async def test_book_that_changed_under_the_gate_is_revalidated_and_declines(
    tmp_path: Path,
) -> None:
    checker = LimitChecker(
        RiskLimits(caps_shadow_mode=True, max_gross_notional_dollars=50.0)
    )

    async def on_call(idx: int, inputs: CandidateBookRiskInputs) -> None:
        # WHILE the MC is in flight the book MOVES — a big position lands that
        # takes the gross past the enforced wall, and the position generation
        # change means every MC verdict prices a book that no longer exists.
        exposure.add_position(_committed(f"late{idx}", contracts=10_000))

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(
        tmp_path, pool=pool, limits=checker, db="changed_book.sqlite3"
    )
    _warm_latency(lifecycle)

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    # Retries were exhausted (the book never settled), so the gate fell back —
    # and the fallback saw the CHANGED book, not the one the reservation was
    # granted against. The enforced caps refuse it.
    assert lifecycle._metrics.counter("candidate_gate.retries_exhausted") == 1  # noqa: SLF001
    assert sender.confirmed == []
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_GATE_TIMEOUT}"
    ) == 1
    assert not reservation.is_outstanding("fill:q1")


async def test_revalidate_is_read_only_and_fails_closed_on_an_unknown_id(
    tmp_path: Path,
) -> None:
    lifecycle, _sender, _exposure, reservation = await _make(
        tmp_path, pool=None, db="revalidate.sqlite3"
    )
    reservation.try_reserve(
        "r1",
        _held("r1"),
        marginals=lifecycle._marginals,  # noqa: SLF001
        daily_pnl=lifecycle.daily_pnl,
    )
    version_before = reservation.version
    breaches = reservation.revalidate(
        "r1",
        marginals=lifecycle._marginals,  # noqa: SLF001
        daily_pnl=lifecycle.daily_pnl,
    )
    assert breaches == []
    # READ-ONLY: no version bump, nothing released, nothing committed.
    assert reservation.version == version_before
    assert reservation.is_outstanding("r1")

    # FAIL-CLOSED on an id that holds no headroom — never a deceptive all-clear.
    unknown = reservation.revalidate(
        "nope",
        marginals=lifecycle._marginals,  # noqa: SLF001
        daily_pnl=lifecycle.daily_pnl,
    )
    assert len(unknown) == 1
    assert unknown[0].reason is ReasonCode.DECLINE_RISK_LIMIT


# --------------------------------------------------------------------------- #
# 6. NO COST PREDICTOR: the MC cannot be switched off by measurement history.  #
# --------------------------------------------------------------------------- #
#
# The first cure for the R5 confirm-window loss was a PREDICTIVE guard: a
# ``_MeasuredRate`` ms/unit estimator, and "do not start work the prediction says
# cannot fit". It carried a defect that was strictly worse than the bug it fixed.
# ``predict_ms`` returned max(samples) x units, and samples only arrived when a
# build actually RAN. The first accept aborted its build at the deadline and
# recorded the cold rate; the guard then skipped every subsequent build; no new
# sample could ever arrive; the max never decayed. The joint-tail / P(ruin) /
# delta-P(book) gate — the whole reason the candidate gate exists — went DARK
# PERMANENTLY, with only a counter to show for it.
#
# It is GONE. The build is interruptible against the derived deadline and the MC
# is bounded by the window that is actually LEFT, so both are attempted on every
# accept and neither can be disabled by anything except the clock. These tests
# pin that there is no state to poison.


async def test_a_slow_mc_never_stops_the_next_mc_from_running(
    tmp_path: Path,
) -> None:
    """THE REGRESSION THAT KILLED THE PREDICTOR, as an executable property.

    Accept #1's MC blows the window (5 s of MC against a ~3 s budget) and
    resolves as a LATENCY timeout. Under the predictive guard, the rate it
    recorded then skipped every later accept forever. Here accept #2 — with a
    fast MC — must still RUN its MC and still take its risk verdict."""
    lifecycle, sender, _exposure, _reservation = await _make(
        tmp_path, pool=None, db="no_starve.sqlite3"
    )
    slow = ScriptedPool(
        [_verdict(confirm=True)], mc_ms=5_000.0, clock=lifecycle._clock  # noqa: SLF001
    )
    lifecycle._book_risk_pool = slow  # noqa: SLF001
    _warm_latency(lifecycle)

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    # The MC was STARTED (that is the point) and timed out inside the window.
    assert len(slow.calls) == 1
    assert lifecycle._metrics.counter("candidate_gate.mc_timeout") == 1  # noqa: SLF001
    # A timeout is never a risk verdict.
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 0

    # --- accept #2, same lifecycle, same process, FAST MC --------------------
    fast = ScriptedPool([_verdict(confirm=False, reason="post_cvar_over_budget")])
    lifecycle._book_risk_pool = fast  # noqa: SLF001
    await lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_2"))
    await lifecycle.on_quote_accepted(accepted_msg("q2", "yes"))

    # THE INVARIANT: the MC ran again, and its DECLINE stood. Nothing the first
    # accept measured could suppress it, because nothing is measured.
    assert len(fast.calls) == 1, "the candidate MC went dark after one slow run"
    assert "q2" not in sender.confirmed
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1


async def test_the_mc_is_bounded_by_the_window_that_is_actually_left(
    tmp_path: Path,
) -> None:
    """The MC's deadline is MEASURED, not typed: it is whatever remains of the
    derived confirm budget after the input build, and it shrinks when the build
    is slower."""
    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, sender, _exposure, _reservation = await _make(
        tmp_path, pool=pool, db="mc_deadline.sqlite3"
    )
    _warm_latency(lifecycle)

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert len(pool.deadlines) == 1
    remaining_s = pool.deadlines[0]
    # Strictly positive (an MC is never started with no window) and strictly
    # inside the exchange's own 3.0 s window (the derived reserve is subtracted).
    assert 0.0 < remaining_s < EXCHANGE_CONFIRM_WINDOW_NS / 1e9
    assert sender.confirmed == ["q1"]


async def test_an_mc_that_overruns_the_window_is_latency_not_a_risk_decline(
    tmp_path: Path,
) -> None:
    """A TimeoutError out of the bounded MC resolves through the deterministic
    caps — it CONFIRMS when they allow, and it is filed as a timeout."""
    lifecycle, sender, _exposure, _reservation = await _make(
        tmp_path, pool=None, db="mc_timeout.sqlite3"
    )
    pool = ScriptedPool(
        [_verdict(confirm=True)], mc_ms=9_000.0, clock=lifecycle._clock  # noqa: SLF001
    )
    lifecycle._book_risk_pool = pool  # noqa: SLF001
    _warm_latency(lifecycle)

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert lifecycle._metrics.counter("candidate_gate.mc_timeout") == 1  # noqa: SLF001
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.window_expired_before_confirm"
    ) == 1
    # A won auction is NOT discarded on our own clock...
    assert sender.confirmed == ["q1"]
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.timeout_fallback_confirm"
    ) == 1
    # ...and it is NEVER filed as a risk verdict.
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 0


async def test_a_slow_build_still_cannot_discard_a_won_auction(
    tmp_path: Path,
) -> None:
    """The build (not the MC) was the live killer. It is interruptible, it
    abandons itself at the deadline, and the auction still resolves on the
    deterministic caps — never on a stopwatch wearing a risk reason code."""
    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, sender, _exposure, _reservation = await _make(
        tmp_path, pool=pool, db="slow_build.sqlite3"
    )
    _warm_latency(lifecycle)
    _stall_the_gate(lifecycle)
    lifecycle._config = LifecycleConfig(  # noqa: SLF001
        quote_ttl_s=30.0,
        reprice_threshold_cc=100,
        candidate_gate_enabled=True,
        candidate_gate_build_check_pairs=1,
    )

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert lifecycle._metrics.counter("candidate_gate.build_aborted") == 1  # noqa: SLF001
    assert pool.calls == []  # the build never finished, so no MC could run
    assert sender.confirmed == ["q1"]
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 0


async def test_build_deadline_check_raises_the_budget_exception(
    tmp_path: Path,
) -> None:
    lifecycle, _sender, _exposure, _reservation = await _make(
        tmp_path, pool=None, db="build_check.sqlite3"
    )
    past = lifecycle._clock.monotonic_ns() - 1  # noqa: SLF001
    try:
        lifecycle._gate_build_deadline_check(past, "unit")  # noqa: SLF001
    except _GateBudgetExceeded as exc:
        assert exc.stage == "unit"
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("the build deadline check did not fire")
    # None = no deadline: every non-confirm caller is unchanged.
    lifecycle._gate_build_deadline_check(None, "unit")  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 7. ROLLBACK: the pre-2026-07-27 behaviour is one flag away.                  #
# --------------------------------------------------------------------------- #


async def test_rollback_flag_restores_decline_on_timeout(tmp_path: Path) -> None:
    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, sender, exposure, reservation = await _make(
        tmp_path, pool=pool, db="rollback.sqlite3"
    )
    _warm_latency(lifecycle)
    _stall_the_gate(lifecycle)
    lifecycle._config = LifecycleConfig(  # noqa: SLF001
        quote_ttl_s=30.0,
        reprice_threshold_cc=100,
        candidate_gate_enabled=True,
        candidate_gate_build_check_pairs=1,
        candidate_gate_timeout_fallback=False,  # rollback
    )

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert sender.confirmed == []
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1
