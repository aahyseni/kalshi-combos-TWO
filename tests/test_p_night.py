"""P(NIGHT) KPI (operator 2026-07-25: "we just want the day to end
positive" — p_book resets as winners settle out; p_night keeps the banked
edge: P(realized-so-far + open book > 0))."""

from __future__ import annotations

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import compute_book_risk


def _pos(pid: str, ticker: str, event: str, *, price_cc: int = 5_000) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(100),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=(LegRef(ticker, event, "yes"),),
    )


def _snap(realized: int | None):
    model = build_book_model(
        [_pos("a", "A", "KX-G1"), _pos("b", "B", "KX-G2")],
        marginals=lambda t: 0.5,
        within_game_rho=None,
    )
    return compute_book_risk(
        model, n_samples=20_000, seed=5, realized_pnl_cc=realized
    )


def test_no_realized_feed_equals_p_book() -> None:
    snap = _snap(None)
    assert snap.p_night == snap.p_profit


def test_banked_profit_pins_p_night_up() -> None:
    # Realized profit far above the open book's worst case (2 x $50 premium
    # = 100_000cc max loss): the night cannot end negative.
    snap = _snap(realized=500_000)
    assert snap.p_night == 1.0
    assert snap.p_profit < 1.0  # the open book alone is still ~a coin flip


def test_realized_loss_drags_p_night_down() -> None:
    up = _snap(realized=20_000)
    down = _snap(realized=-20_000)
    assert up.p_night > up.p_profit > down.p_night


class TestPBookNonDecreasingGate:
    """Operator doctrine 2026-07-25: "anything we take in should push it up,
    or neutral" — v2 INDEPENDENCE BENCHMARK (the v1 absolute floor misfired
    live: 11 refusals of ordinary growth fills on a knife-edge book — parity
    artifact, not concentration). The gate declines only fills measurably
    WORSE than an independent bet of identical per-scenario P&L (correlation
    drag); parity/size drag passes and stays the size caps' job."""

    def _book(self):
        # Five independent +EV coin flips at 30c on a 50c fair: sum > 0 iff
        # ≥2 of 5 miss ⇒ p_book ≈ 0.81.
        return [
            _pos(f"c{i}", f"L{i}", f"KX-G{i}", price_cc=3_000) for i in range(5)
        ]

    def _run(self, cand: OpenPosition, **kw):
        from combomaker.sim.book_risk import evaluate_candidate_book_risk

        return evaluate_candidate_book_risk(
            self._book(), cand, marginals=lambda t: 0.5,
            n_samples=20_000, seed=9, **kw,
        )

    def test_correlated_same_leg_add_declined(self) -> None:
        # A 3x-size SAME-LEG add: identical marginal P&L to an independent
        # coin of that size, but perfectly correlated with c0 — its measured
        # ΔP(book) is far worse than its shuffled (independent) twin's.
        corr = OpenPosition(
            position_id="corr", combo_ticker="COMBO-corr", collection=None,
            our_side=Side.NO, contracts=CentiContracts(300),
            entry_price_cc=3_000,  # type: ignore[arg-type]
            legs=(LegRef("L0", "KX-G0", "yes"),),
        )
        off = self._run(corr)
        on = self._run(corr, require_p_book_non_decreasing=True)
        assert off.confirm  # pre-doctrine behavior: admitted
        assert not on.confirm
        assert on.decline_reason == "lowers_p_book"

    def test_independent_same_size_admitted(self) -> None:
        # The SAME size on a brand-new game: its measured ΔP tracks the
        # independence benchmark by construction — admitted (this is the
        # exact shape the v1 floor misfired on).
        indep = OpenPosition(
            position_id="ind", combo_ticker="COMBO-ind", collection=None,
            our_side=Side.NO, contracts=CentiContracts(300),
            entry_price_cc=3_000,  # type: ignore[arg-type]
            legs=(LegRef("L9", "KX-G9", "yes"),),
        )
        on = self._run(indep, require_p_book_non_decreasing=True)
        assert on.confirm, on.decline_reason

    def test_diversifier_admitted(self) -> None:
        cand = _pos("new", "L9", "KX-G9", price_cc=3_000)
        on = self._run(cand, require_p_book_non_decreasing=True)
        assert on.confirm, on.decline_reason
