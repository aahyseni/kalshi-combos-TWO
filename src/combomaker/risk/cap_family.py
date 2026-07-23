"""Correlation-adaptive risk caps — the deploy caps are a nightly function of
MEASURED within-game vol (``sigma1``) and cross-game correlation (``-> G_eff``),
not hand-set fractions (NOTES.md "Cap refactor").

Principle: the caps are NOT the safety mechanism — measured correlation is. The
hard-trip KILL is anchored at ``kill_anchor`` (0.12); everything else is solved
around keeping KILL at ``k_trip`` sigma of daily P&L vol. Deploy caps
(slate/game/per_combo) breathe UP as within-game vol falls (genuine
diversification), and a FAST cross-game ratchet snaps them DOWN the instant
cross-game rho rises (a lumpy "all-overs"/chalk night) — faster to tighten than
to loosen. The small bootstrap numbers are "we have not measured MLB's rho yet",
not "small is safe"; once rho is measured the formula EARNS the bigger caps.

Vol model (diversified book of ``G_eff`` independent games):
    sigma_day = sigma1 * f_slate * bankroll / sqrt(G_eff)
    f_slate solved so k_trip * sigma_day / bank == kill_anchor:
        f_slate = (kill_anchor / k_trip) * sqrt(G_eff) / sigma1
    => sigma_day/bank == kill_anchor/k_trip  (independent of sigma1/G_eff).

The derivation works in float (probability/vol space). Conversion to the exact
``Fraction`` thresholds ``RiskLimits`` consumes happens at the wiring boundary
(``as_fractions``); floats are never used as a live threshold (house rule).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

# Defaults (operator 2026-07-22). k = target z-scores; kill anchored at 12%.
K_DAILY = 3.0
K_DD = 4.0
K_TRIP = 5.0
KILL_ANCHOR = 0.12
PER_COMBO_FRAC = 0.01            # fixed, tightened from the WC 0.05
RUIN_FLOOR_FRAC = 0.30           # unchanged, scale-free
F_SLATE_PROVISIONAL_CAP = 0.15   # clamp until MLB sigma1/rho measured on real nights
CROSS_RHO_RATCHET_GATE = 0.05    # >= this blocks any loosen; the formula shrinks above it
MC_HEADROOM = 1.3                # directional/det_max/cvar = MC_HEADROOM * MC(projected)


@dataclass(frozen=True)
class CapFractions:
    """Derived caps as fractions of live bankroll (the values RiskLimits reads)."""
    per_combo_loss_frac: float
    game_loss_frac: float
    slate_loss_frac: float          # = f_slate
    daily_loss_frac: float
    drawdown_frac: float
    hard_trip_frac: float
    ruin_floor_frac: float
    # MC-derived book caps (MC_HEADROOM * portfolio MC on the projected book);
    # None when the MC value was not supplied (caller keeps the prior/absolute).
    directional_frac: float | None
    portfolio_det_max_frac: float | None
    portfolio_cvar_frac: float | None
    # provenance
    sigma1: float | None
    g_eff: float | None
    expected_games: int
    cross_game_rho: float | None
    within_game_rho: float | None
    sigma_day_over_bank: float
    measured: bool
    provisional: bool
    ratchet_held: bool              # a solved INCREASE was blocked by the cross-rho gate

    def as_fractions(self) -> dict[str, Fraction]:
        """Exact ``Fraction`` view of the loss/halt caps for RiskLimits (6-dp
        quantized — floats are banned as live thresholds)."""
        def frac(x: float | None) -> Fraction | None:
            return None if x is None else Fraction(round(x * 1_000_000), 1_000_000)
        out = {
            "per_combo_loss_frac": frac(self.per_combo_loss_frac),
            "game_loss_frac": frac(self.game_loss_frac),
            "slate_loss_frac": frac(self.slate_loss_frac),
            "daily_loss_frac": frac(self.daily_loss_frac),
            "drawdown_frac": frac(self.drawdown_frac),
            "hard_trip_frac": frac(self.hard_trip_frac),
            "directional_frac": frac(self.directional_frac),
            "portfolio_det_max_frac": frac(self.portfolio_det_max_frac),
            "portfolio_cvar_frac": frac(self.portfolio_cvar_frac),
        }
        return {k: v for k, v in out.items() if v is not None}


def g_eff_from_cross_rho(n_games: int, cross_rho: float) -> float:
    """Effective independent games from equicorrelated per-game P&L. rho≈0 ->
    G_eff≈n_games; rho up -> G_eff collapses toward 1. Clamped to [1, n_games]."""
    if n_games < 1:
        raise ValueError("n_games must be >= 1")
    denom = 1.0 + (n_games - 1) * max(cross_rho, 0.0)
    return min(float(n_games), max(1.0, n_games / denom))


def derive_cap_fractions(
    *,
    expected_games: int,
    sigma1: float | None = None,          # per-$-premium within-game P&L std
    g_eff: float | None = None,           # effective independent games
    cross_game_rho: float | None = None,  # measured/projected cross-game P&L rho
    within_game_rho: float | None = None, # informational (drives sigma1); provenance only
    f_slate_prev: float | None = None,    # last-honored slate frac (ratchet baseline)
    provisional: bool = True,             # True until a stable multi-week MLB read
    mc_directional: float | None = None,  # portfolio MC on the PROJECTED book (frac of bank)
    mc_det_max: float | None = None,
    mc_cvar: float | None = None,
    k_daily: float = K_DAILY,
    k_dd: float = K_DD,
    k_trip: float = K_TRIP,
    kill_anchor: float = KILL_ANCHOR,
    per_combo: float = PER_COMBO_FRAC,
    ruin_floor: float = RUIN_FLOOR_FRAC,
    f_slate_provisional_cap: float = F_SLATE_PROVISIONAL_CAP,
    rho_gate: float = CROSS_RHO_RATCHET_GATE,
    mc_headroom: float = MC_HEADROOM,
) -> CapFractions:
    if expected_games < 1:
        raise ValueError("expected_games must be >= 1")
    if not (0 < kill_anchor < 1) or k_trip <= 0:
        raise ValueError("kill_anchor in (0,1) and k_trip > 0 required")

    # Halts: the KILL anchor pins sigma_day/bank; daily/dd/trip are FIXED z-anchors.
    sdob = kill_anchor / k_trip                       # 0.024 @ (0.12, 5)
    daily = k_daily * sdob                             # 0.072
    drawdown = k_dd * sdob                             # 0.096
    hard_trip = kill_anchor                            # 0.12

    measured = sigma1 is not None and g_eff is not None
    held = False
    if sigma1 is not None and g_eff is not None:
        if sigma1 <= 0 or g_eff <= 0:
            raise ValueError("measured sigma1 and g_eff must be > 0")
        # Solve f_slate so the KILL sits at exactly k_trip sigma. Higher measured
        # vol (sigma1) or lower G_eff (higher cross-rho) -> smaller f_slate.
        f_slate = kill_anchor * math.sqrt(g_eff) / (k_trip * sigma1)
        # RATCHET: an INCREASE over the last-honored level requires proven low
        # cross-game correlation. The formula already SHRINKS via G_eff when
        # cross-rho rises; the gate only blocks loosening. (Fast tighten / slow
        # loosen: tightening flows straight through the shrinking G_eff.)
        if f_slate_prev is not None and f_slate > f_slate_prev:
            if cross_game_rho is None or cross_game_rho >= rho_gate:
                f_slate = f_slate_prev
                held = True
    else:
        # Bootstrap: MLB rho unmeasured -> we cannot yet tell a safe big-cap night
        # from a ruinous one, so deploy the conservative provisional budget.
        f_slate = f_slate_provisional_cap

    # Provisional clamp: never exceed the provisional cap until a stable read.
    if provisional:
        f_slate = min(f_slate, f_slate_provisional_cap)

    game = f_slate / expected_games                    # forces spreading across games

    def mc(x: float | None) -> float | None:
        return None if x is None else mc_headroom * x

    return CapFractions(
        per_combo_loss_frac=per_combo,
        game_loss_frac=game,
        slate_loss_frac=f_slate,
        daily_loss_frac=daily,
        drawdown_frac=drawdown,
        hard_trip_frac=hard_trip,
        ruin_floor_frac=ruin_floor,
        directional_frac=mc(mc_directional),
        portfolio_det_max_frac=mc(mc_det_max),
        portfolio_cvar_frac=mc(mc_cvar),
        sigma1=sigma1, g_eff=g_eff, expected_games=expected_games,
        cross_game_rho=cross_game_rho, within_game_rho=within_game_rho,
        sigma_day_over_bank=sdob, measured=measured, provisional=provisional,
        ratchet_held=held,
    )
