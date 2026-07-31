"""CROSS-GAME RESIDUAL CORRELATION — INSTRUMENTATION ONLY, NEVER A GATE.

OPERATOR RULING (ratified 2026-07-29):

    "Cross games are independent for sure, one MLB game doesn't affect another
    game, and that goes for any sport, same with esports."

So ``sim/book_model.DEFAULT_CROSS_EVENT_RHO = 0.0`` STANDS and no family is
quarantined for want of a measured cross-game rho. This module does not
challenge that and **never gates anything** — no caller may branch a quote, a
confirm, a cap or a halt on its output.

WHY IT EXISTS ANYWAY. The exposure is not causal linkage between games; it is
CORRELATED MODEL ERROR. If our K model runs rich it is rich on every pitcher at
once, and independent games then behave like ONE bet in the tail. The measured
sensitivity is not academic: cross-rho 0.00 -> 0.25 moves the live book's ES99
$221.64 -> $280.65 and modeled EV +$10.84 -> -$4.25 — the entire edge. The
assumption therefore has to be ANSWERABLE FROM DATA rather than merely believed,
and settlements are the one ruler the model cannot bend (quiet-failure defense
#5). This emits a per-slate estimate of the realized cross-game residual
correlation, from our own fair vs the realized outcome, so a drift shows up as a
number the operator can read.

THE ESTIMATOR. For every settled position i we hold a modeled probability of
LOSS ``p_i`` (our fair said the parlay hits with probability p_i; we are long
NO, so a hit is our loss) and the realized indicator ``y_i in {0,1}``. The
standardized residual

    z_i = (y_i - p_i) / sqrt(p_i (1 - p_i))

has mean 0 and variance 1 under a CORRECT marginal model, whatever the
correlation structure. Pool them per GAME g (positions inside one game are
correlated by construction — that is the within-game rho we already model) into
a game-level residual

    r_g = mean_i in g (z_i)  , standardized to unit variance by construction of
                              the equicorrelated pool it is compared against

and then, across the G games of ONE slate, use the moment identity for an
equicorrelated family with E[r_g] = 0, Var(r_g) = 1:

    E[ (sum_g r_g)^2 - sum_g r_g^2 ] = G (G - 1) rho

    ==>  rho_hat = ( (sum_g r_g)^2 - sum_g r_g^2 ) / ( G (G - 1) )

This is an UNBIASED single-slate estimator of the equicorrelation — exactly the
rho that ``cap_family.g_eff_from_cross_rho`` consumes — and it needs only ONE
night (there is one draw per game per night; the cross-sectional spread across
games carries the information, not a time series). Under H0 (rho = 0,
independent games) its standard error is sqrt(2 / (G (G - 1))), which is what
``z_score`` reports. Pool ``rho_hat`` across nights with ``pool_slates`` for a
tighter read; a single slate at G = 10 has SE 0.149, so ONE night can only see a
gross violation.

READ IT LIKE THIS. rho_hat ~ 0 with a small z is the operator's ruling holding
up. A persistently POSITIVE rho_hat across slates is the correlated-model-error
signature (we are rich or cheap on every game at once) — that is a MODEL finding
to be repaired in the model, not a cap to be tightened.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "CrossGameResidual",
    "SettledLeg",
    "pool_slates",
    "slate_cross_game_rho",
]

# Marginals this close to 0 or 1 make the standardization explode on a single
# flip (1/sqrt(p(1-p)) -> inf). They are DROPPED, not clamped: a clamp would
# invent a residual size the data does not support, and dropping is reported.
_MIN_P = 0.01
_MAX_P = 0.99


@dataclass(frozen=True, slots=True)
class SettledLeg:
    """One SETTLED position, reduced to what the estimator needs.

    ``game`` is the game grouping key (``pricing.grouping.game_key``).
    ``p_loss`` is OUR modeled probability that this position LOSES (for a
    long-NO combo: the probability the parlay HITS) at the price we traded.
    ``lost`` is the realized outcome from the exchange's settlement.
    """

    game: str
    p_loss: float
    lost: bool


@dataclass(frozen=True, slots=True)
class CrossGameResidual:
    """One slate's estimate. Every field is a REPORT; none is a control input."""

    slate: str
    games: int
    positions: int
    dropped_positions: int          # marginals outside [_MIN_P, _MAX_P]
    rho_hat: float                  # equicorrelation of per-game residuals
    se_h0: float                    # SE under H0 (rho = 0)
    z_score: float                  # rho_hat / se_h0
    mean_residual: float            # slate-level calibration drift (signed)
    usable: bool                    # False ⇒ fewer than 2 games; rho undefined

    def as_log_fields(self) -> dict[str, object]:
        """Flat fields for one ``cross_game_residual_rho`` log line."""
        return {
            "slate": self.slate,
            "games": self.games,
            "positions": self.positions,
            "dropped_positions": self.dropped_positions,
            "rho_hat": round(self.rho_hat, 4),
            "se_h0": round(self.se_h0, 4),
            "z_score": round(self.z_score, 3),
            "mean_residual": round(self.mean_residual, 4),
            "usable": self.usable,
            "gate": False,  # pinned: this number NEVER gates (operator ruling)
        }


def _game_residuals(
    legs: Iterable[SettledLeg],
) -> tuple[dict[str, float], int, int]:
    """Per-game standardized residuals, the kept count and the dropped count."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    kept = 0
    dropped = 0
    for leg in legs:
        p = float(leg.p_loss)
        if not (_MIN_P <= p <= _MAX_P) or not math.isfinite(p):
            dropped += 1
            continue
        z = ((1.0 if leg.lost else 0.0) - p) / math.sqrt(p * (1.0 - p))
        sums[leg.game] = sums.get(leg.game, 0.0) + z
        counts[leg.game] = counts.get(leg.game, 0) + 1
        kept += 1
    # Mean over the game's positions: a game carrying 6 tickets must not
    # outweigh a game carrying 1 simply for being busier — the estimator is
    # about the GAME-level common factor, one draw per game.
    return (
        {g: s / counts[g] for g, s in sums.items()},
        kept,
        dropped,
    )


def slate_cross_game_rho(
    slate: str, legs: Sequence[SettledLeg]
) -> CrossGameResidual:
    """Estimate one slate's cross-game residual equicorrelation. Pure."""
    residuals, kept, dropped = _game_residuals(legs)
    n_games = len(residuals)
    if n_games < 2:
        return CrossGameResidual(
            slate=slate,
            games=n_games,
            positions=kept,
            dropped_positions=dropped,
            rho_hat=0.0,
            se_h0=0.0,
            z_score=0.0,
            mean_residual=(
                sum(residuals.values()) / n_games if n_games else 0.0
            ),
            usable=False,
        )
    r = list(residuals.values())
    total = math.fsum(r)
    sq = math.fsum(x * x for x in r)
    denom = float(n_games * (n_games - 1))
    rho_hat = (total * total - sq) / denom
    se = math.sqrt(2.0 / denom)
    return CrossGameResidual(
        slate=slate,
        games=n_games,
        positions=kept,
        dropped_positions=dropped,
        rho_hat=rho_hat,
        se_h0=se,
        z_score=rho_hat / se if se > 0 else 0.0,
        mean_residual=total / n_games,
        usable=True,
    )


def pool_slates(
    per_slate: Mapping[str, CrossGameResidual],
) -> tuple[float, float, int]:
    """Inverse-variance pool of usable slates -> ``(rho_hat, se, n_slates)``.

    One night at G = 10 has SE 0.149 — enough only for a gross violation — so
    the operator's read is the POOL. Returns ``(0.0, 0.0, 0)`` when nothing is
    usable (never an invented number).
    """
    usable = [c for c in per_slate.values() if c.usable and c.se_h0 > 0]
    if not usable:
        return 0.0, 0.0, 0
    weights = [1.0 / (c.se_h0 * c.se_h0) for c in usable]
    w_sum = math.fsum(weights)
    rho = math.fsum(w * c.rho_hat for w, c in zip(weights, usable, strict=True))
    return rho / w_sum, math.sqrt(1.0 / w_sum), len(usable)
