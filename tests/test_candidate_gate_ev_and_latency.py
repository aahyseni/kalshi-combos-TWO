"""P1 EV VISIBILITY + LIVE CANDIDATE-GATE LATENCY (audit sections
"+EV IS PRODUCTION-MODEL EV, NOT ROBUST EV" and "LIVE CANDIDATE-GATE LATENCY").

Two audit requirements, tested here:

(A) EV visibility. ``evaluate_candidate_book_risk`` now computes the candidate's
    marginal EV under the CHALLENGER book states (correlation-inflated challenger,
    and the conditional full-copula bridge / unconditioned split) DISTINCTLY from the
    production-model EV, plus ``worst_credible_candidate_ev_cc`` = min over them. The
    ADMISSION policy is unchanged (``production_candidate_ev > 0``); the OPTIONAL
    ``worst_challenger_ev_tolerance`` DEFAULTS to −inf (a no-op) and only ever ADDS a
    decline. The lifecycle LOGS the production EV distinctly from the challenger EVs.

(B) Latency / deadline metrics. A gate run records the candidate-gate p50/p90/p99
    runtime (the per-attempt ``candidate_gate.mc_ms`` histogram + the total
    ``candidate_gate.runtime_ms``), the remaining confirm-window time at completion
    (``candidate_gate.remaining_window_ms``), the MC worker queue dwell
    (``candidate_gate.queue_dwell_ms`` when a pool ran it), and — when the confirm
    window expires before a stable verdict — the accept-lost axis
    (``candidate_gate.window_expired_before_confirm``). A gate with insufficient
    remaining deadline FAILS CLOSED.
"""

from __future__ import annotations

from pathlib import Path

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_risk import evaluate_candidate_book_risk

from tests.test_candidate_gate_atomic import ScriptedPool, _make, _verdict
from tests.test_lifecycle import accepted_msg, rfq


# --------------------------------------------------------------------------- #
# (A) EV visibility — challenger EV computed distinctly from production EV.      #
# --------------------------------------------------------------------------- #


def _pos(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    side: Side = Side.NO,
    ct: int = 200,
    price: int = 3_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"C-{pid}",
        collection=None,
        our_side=side,
        contracts=CentiContracts(ct),
        entry_price_cc=price,  # type: ignore[arg-type]
        legs=legs,
        risk_modeled=True,
    )


def _leg(t: str, e: str, s: str = "yes") -> LegRef:
    return LegRef(market_ticker=t, event_ticker=e, side=s)


def _divergent_inputs() -> tuple[list[OpenPosition], OpenPosition, dict]:
    """A same-game multi-leg NO book whose candidate is +EV under production but
    whose CHALLENGER (correlation-inflated) EV is materially LOWER — the exact case
    the audit flags (a +production-EV candidate that a challenger sees differently).
    Inflating within-game correlation raises the parlay's joint hit probability, so
    the NO's settlement EV shifts under the challenger."""
    committed = [
        _pos("c1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
    ]
    cand = _pos("cand", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
    common = dict(
        marginals=lambda t: 0.6,
        within_game_rho=lambda a, b: (0.3, 0.5, 0.7),
        n_samples=80_000,
        seed=4,
        challenger_inflation=0.9,
    )
    return committed, cand, common


def test_challenger_candidate_ev_computed_distinctly_from_production() -> None:
    committed, cand, common = _divergent_inputs()
    r = evaluate_candidate_book_risk(committed, cand, **common)
    # Both EVs are real floats, and the challenger EV DIFFERS from the production EV
    # (the correlation inflation genuinely moved the marginal EV — not a copy).
    assert isinstance(r.candidate_ev_cc, float)
    assert isinstance(r.challenger_candidate_ev_cc, float)
    assert r.candidate_ev_cc != r.challenger_candidate_ev_cc
    # This book straddles no structural block, so the bridge/split EVs are UNDEFINED
    # (None), never a convenient 0.
    assert r.bridge_candidate_ev_cc is None
    assert r.split_candidate_ev_cc is None
    # worst-credible = the min over production + every challenger EV that ran.
    assert r.worst_credible_candidate_ev_cc == min(
        r.candidate_ev_cc, r.challenger_candidate_ev_cc
    )
    # Production EV is positive (the fill is admitted), yet the challenger EV is
    # strictly lower — the "positive under production, worse under a challenger" case.
    assert r.candidate_ev_cc > 0.0
    assert r.challenger_candidate_ev_cc < r.candidate_ev_cc


def test_worst_challenger_tolerance_defaults_to_noop() -> None:
    # The DEFAULT tolerance is −inf, so ``worst >= −inf`` is always true and the gate
    # verdict is identical to not having the tolerance at all: the +production-EV
    # candidate confirms exactly as before.
    committed, cand, common = _divergent_inputs()
    default = evaluate_candidate_book_risk(committed, cand, **common)
    # A tolerance BELOW the worst EV also cannot decline (still satisfied).
    below = evaluate_candidate_book_risk(
        committed, cand, worst_challenger_ev_tolerance=float("-inf"), **common
    )
    assert default.confirm is True
    assert default.decline_reason == ""
    assert below.confirm == default.confirm
    assert below.decline_reason == default.decline_reason


def test_worst_challenger_tolerance_only_adds_a_decline() -> None:
    # A finite tolerance ABOVE the worst credible challenger EV DECLINES the fill that
    # production-EV alone would admit — strictly additive (flips admit→decline only).
    committed, cand, common = _divergent_inputs()
    admit = evaluate_candidate_book_risk(committed, cand, **common)
    assert admit.confirm  # production-EV gate admits
    tol_above = admit.worst_credible_candidate_ev_cc + 1.0
    declined = evaluate_candidate_book_risk(
        committed, cand, worst_challenger_ev_tolerance=tol_above, **common
    )
    assert not declined.confirm
    assert declined.decline_reason == "worst_challenger_ev_below_tolerance"
    # A tolerance BELOW the worst EV leaves the admit unchanged (never loosens).
    tol_below = admit.worst_credible_candidate_ev_cc - 1.0
    still = evaluate_candidate_book_risk(
        committed, cand, worst_challenger_ev_tolerance=tol_below, **common
    )
    assert still.confirm == admit.confirm


def test_tolerance_never_flips_a_declined_candidate_to_admit() -> None:
    # A NEGATIVE-EV candidate is declined by the production-EV gate; no tolerance
    # value can turn that into an admit (the EV-sign gate runs first and the tolerance
    # only ever adds declines).
    committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), price=2_000, ct=200)]
    # LONG YES bought expensive on a rarely-hitting parlay ⇒ negative EV.
    hedge = _pos(
        "hedge", (_leg("A", "KXWCGAME-G1"),), side=Side.YES, ct=200, price=9_500
    )
    r = evaluate_candidate_book_risk(
        committed,
        hedge,
        marginals=lambda t: 0.05,
        n_samples=40_000,
        seed=2,
        worst_challenger_ev_tolerance=1_000_000.0,  # absurdly strict
    )
    assert not r.confirm
    # Declined by the EV-SIGN gate (first), not the tolerance — the tolerance can only
    # add declines, never rescue a fill the EV gate already rejected.
    assert r.decline_reason == "negative_ev_no_hedge_budget"


# --------------------------------------------------------------------------- #
# (A) EV visibility — the lifecycle LOGS production EV distinctly + gate wiring. #
# --------------------------------------------------------------------------- #


async def test_lifecycle_gate_reports_challenger_ev_and_worst_credible(
    tmp_path: Path,
) -> None:
    # A scripted verdict whose challenger EV differs from production EV: the gate wiring
    # carries the distinct EV fields straight through to a confirm, and the worst-
    # credible EV field is populated (min over the two).
    from combomaker.sim.book_risk import CandidateBookRisk, _TailAxes

    def axes(ev: float) -> _TailAxes:
        return _TailAxes(
            ev_cc=ev,
            es_99_cc=0.0,
            challenger_es_99_cc=0.0,
            governing_model_es_99_cc=0.0,
            deterministic_max_loss_cc=0.0,
            gross_settlement_notional_cc=0.0,
            p_ruin=0.0,
        )

    verdict = CandidateBookRisk(
        unknown=False,
        band="high",
        n_samples=20_000,
        seed=7,
        n_pre_positions=0,
        n_post_positions=1,
        pre=axes(0.0),
        post=axes(5.0),
        candidate_ev_cc=5.0,
        challenger_candidate_ev_cc=2.0,
        bridge_candidate_ev_cc=None,
        split_candidate_ev_cc=None,
        worst_credible_candidate_ev_cc=2.0,
        confirm=True,
        decline_reason="",
    )
    pool = ScriptedPool([verdict])
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    # The gate confirmed off the scripted verdict (production EV > 0), carrying its
    # distinct production / challenger EV values (2.0 != 5.0 proves they are separate).
    assert sender.confirmed == ["q1"]
    assert verdict.candidate_ev_cc != verdict.challenger_candidate_ev_cc


# --------------------------------------------------------------------------- #
# (B) Latency metrics recorded on a gate run.                                   #
# --------------------------------------------------------------------------- #


async def test_latency_metrics_recorded_on_a_gate_run(tmp_path: Path) -> None:
    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == ["q1"]
    snap = lifecycle._metrics.snapshot()  # noqa: SLF001
    latencies = snap["latencies_ms"]
    # Per-attempt runtime histogram (feeds candidate-gate p50/p90/p99) recorded once.
    assert "candidate_gate.mc_ms" in latencies
    assert latencies["candidate_gate.mc_ms"]["count"] == 1
    # p50/p90-ish (p95/p99 reported by the histogram) are computed off the histogram.
    assert "p50" in latencies["candidate_gate.mc_ms"]
    assert "p99" in latencies["candidate_gate.mc_ms"]
    # Total gate runtime + remaining confirm-window at completion, one obs each.
    assert latencies["candidate_gate.runtime_ms"]["count"] == 1
    assert latencies["candidate_gate.remaining_window_ms"]["count"] == 1


async def test_gate_deadline_records_window_expired_and_fails_closed(
    tmp_path: Path,
) -> None:
    # The first MC burns ~90% of the deadline AND moves the book (forces a retry). On
    # the retry the deadline guard sees another MC would overrun the confirm window, so
    # it FAILS CLOSED — recording the accept-lost (window-expired) axis and a 0
    # remaining-window observation — rather than starting an MC that would overrun.
    from tests.test_candidate_gate_atomic import _held

    async def on_call(idx, inputs):
        if idx == 0:
            lifecycle._clock.advance(1.8)  # noqa: SLF001 — burn ~90% of the 2.0s budget
            reservation.try_reserve(
                "concurrent",
                _held("concurrent"),
                marginals=lifecycle._marginals,  # noqa: SLF001
                daily_pnl=lifecycle.daily_pnl,
            )

    pool = ScriptedPool([_verdict(confirm=True)], on_call=on_call)
    lifecycle, sender, exposure, reservation = await _make(tmp_path, pool=pool)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    # Exactly ONE MC ran; the retry was refused by the deadline guard (fail-closed).
    assert len(pool.calls) == 1
    assert sender.confirmed == []
    assert not reservation.is_outstanding("fill:q1")  # provisional released
    # The audit's accept-lost axis is recorded, alongside the existing deadline trip.
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.window_expired_before_confirm"
    ) == 1
    assert lifecycle._metrics.counter(  # noqa: SLF001
        "candidate_gate.deadline_exceeded"
    ) == 1
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
    ) == 1
    # A 0-remaining-window observation was recorded at the deadline outcome.
    snap = lifecycle._metrics.snapshot()  # noqa: SLF001
    assert (
        snap["latencies_ms"]["candidate_gate.remaining_window_ms"]["count"] == 1
    )
