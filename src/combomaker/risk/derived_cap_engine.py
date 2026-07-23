"""Wiring adapter: the correlation-adaptive cap BRAIN -> the live ``RiskLimits``
the ``LimitChecker`` enforces (CLAUDE.md North Star).

FULLY ADAPTIVE (operator directive 2026-07-22: "do not enforce any manual numbers,
it should all be adaptive"). Every enforced cap comes ONLY from:
  - layer 1, MEASURED: per-game vol sigma1, cross-game rho -> G_eff, the portfolio
    MC (1.3xMC book caps);
  - layer 2, POLICY ANCHORS (the operator's risk APPETITE, stated once, not knobs):
    KILL 12% = 5sigma -> the z-anchored halts (daily 3s / dd 4s / trip 5s),
    per-combo 1%.
NONE of the static config fractions (the old WC-tuned game/slate/daily/... numbers)
govern under enforce — ``dataclasses.replace`` overrides ALL nine cap axes, so the
base's cap values are inert (they survive only as the off-mode / fail-safe
fallback). There is no clamp to a hand-set config value; the safety is the KILL
anchor + measured vol, not an aging manual ceiling.

BOOK-CAP bootstrap floors are themselves ADAPTIVE, not constants: a fresh/empty
book has MC ~ 0 (NOTES.md C3) so a pure 1.3xMC cap would be 0 and block the first
fill. Until real MC accrues the book caps are floored at the DERIVED budget —
directional / det-max at the slate loss budget, CVaR (ES99 tail) at the 4-sigma
drawdown anchor — then 1.3xMC governs as det-max / ES / directional risk builds.
Everything non-cap (max_contracts, max_open_quotes, delta caps, ...) passes through
from the base unchanged. The caller decides shadow (log) vs enforce
(``LimitChecker.set_limits``)."""
from __future__ import annotations

import dataclasses
from fractions import Fraction

from combomaker.risk.adaptive_caps import compute_nightly_caps
from combomaker.risk.cap_family import KILL_ANCHOR, CapFractions
from combomaker.risk.limits import RiskLimits
from combomaker.risk.pnl_measurement import NightPnl, VolCorrEstimate


def _to_frac(x: float) -> Fraction:
    """6-dp exact Fraction (floats are banned as live thresholds; house rule)."""
    return Fraction(round(x * 1_000_000), 1_000_000)


class DerivedCapEngine:
    def __init__(self, base: RiskLimits, *, kill_anchor: float = KILL_ANCHOR) -> None:
        self._base = base
        self._kill_anchor = kill_anchor    # operator drawdown tolerance (layer-2 anchor)
        self._f_slate_prev: float | None = None

    @property
    def f_slate_prev(self) -> float | None:
        return self._f_slate_prev

    def refresh(
        self,
        *,
        expected_games: int,
        pnl_history: list[NightPnl],
        mc_directional: float | None = None,
        mc_det_max: float | None = None,
        mc_cvar: float | None = None,
        force_provisional: bool = False,
    ) -> tuple[RiskLimits, CapFractions, VolCorrEstimate]:
        """Tonight's derived ``RiskLimits`` from measured state + policy anchors.
        Returns ``(new_limits, caps, estimate)``; the caller logs the diff and
        decides shadow vs enforce. No static config fraction enters the result."""
        caps, est = compute_nightly_caps(
            pnl_history=pnl_history,
            expected_games=expected_games,
            f_slate_prev=self._f_slate_prev,
            mc_directional=mc_directional,
            mc_det_max=mc_det_max,
            mc_cvar=mc_cvar,
            force_provisional=force_provisional,
            kill_anchor=self._kill_anchor,
        )
        self._f_slate_prev = caps.slate_loss_frac

        # Adaptive book-cap floors: the DERIVED budget, never a hand-set constant.
        slate_f = _to_frac(caps.slate_loss_frac)   # directional / det-max floor
        dd_f = _to_frac(caps.drawdown_frac)         # CVaR (ES99 tail) floor = 4s anchor

        def book(mc_1p3: float | None, floor: Fraction) -> Fraction:
            # caps.<book>_frac is already 1.3xMC (cap_family applies the headroom);
            # None when no MC was supplied (startup / empty book) -> the adaptive
            # floor. Once real MC accrues, 1.3xMC governs.
            return floor if mc_1p3 is None else max(floor, _to_frac(mc_1p3))

        new = dataclasses.replace(
            self._base,
            per_combo_loss_frac=_to_frac(caps.per_combo_loss_frac),
            game_loss_frac=_to_frac(caps.game_loss_frac),
            slate_loss_frac=slate_f,
            daily_loss_frac=_to_frac(caps.daily_loss_frac),
            drawdown_frac=dd_f,
            hard_trip_frac=_to_frac(caps.hard_trip_frac),
            directional_frac=book(caps.directional_frac, slate_f),
            portfolio_det_max_frac=book(caps.portfolio_det_max_frac, slate_f),
            portfolio_cvar_frac=book(caps.portfolio_cvar_frac, dd_f),
        )
        return new, caps, est
