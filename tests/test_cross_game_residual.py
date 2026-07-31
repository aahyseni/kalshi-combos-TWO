"""CROSS-GAME RESIDUAL CORRELATION diagnostic (2026-07-29) — instrumentation.

The operator ruled cross games INDEPENDENT ("one MLB game doesn't affect another
game, and that goes for any sport, same with esports"), so
``DEFAULT_CROSS_EVENT_RHO = 0.0`` stands and nothing here gates. What the
estimator answers is the OTHER exposure: CORRELATED MODEL ERROR. If our K model
runs rich it is rich on every pitcher at once and independent games behave like
ONE bet in the tail — measured sensitivity cross-rho 0.00 -> 0.25 moves the live
book's ES99 $221.64 -> $280.65 and modeled EV +$10.84 -> -$4.25.

Pinned: the estimator is unbiased at rho = 0 under a correct model; it detects a
planted common factor; it is NOT fooled by a busy game carrying many tickets; it
refuses to report on fewer than 2 games; and it can never gate.
"""

from __future__ import annotations

import random
import statistics

from combomaker.risk.cross_game_residual import (
    SettledLeg,
    pool_slates,
    slate_cross_game_rho,
)


def _independent_slate(rng: random.Random, games: int, p: float):
    return [
        SettledLeg(game=f"G{g}", p_loss=p, lost=rng.random() < p)
        for g in range(games)
    ]


def _common_factor_slate(rng: random.Random, games: int, p: float, rho: float):
    """A slate where ONE latent factor tilts every game the same way — the
    correlated-model-error shape (we are rich on everything at once)."""
    shock = rng.gauss(0.0, 1.0)
    out = []
    for g in range(games):
        idio = rng.gauss(0.0, 1.0)
        z = (rho**0.5) * shock + ((1.0 - rho) ** 0.5) * idio
        # Gaussian-copula the standard normal onto the loss indicator: the
        # marginal stays exactly p, only the DEPENDENCE changes.
        out.append(SettledLeg(game=f"G{g}", p_loss=p, lost=z > _z_for(1.0 - p)))
    return out


def _z_for(q: float) -> float:
    """Normal quantile via bisection (no scipy in the runtime deps)."""
    lo, hi = -8.0, 8.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if statistics.NormalDist().cdf(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class TestUnbiasedAtZero:
    def test_independent_slates_average_to_zero(self) -> None:
        rng = random.Random(4)
        est = [
            slate_cross_game_rho(f"s{i}", _independent_slate(rng, 12, 0.25))
            for i in range(400)
        ]
        mean = sum(c.rho_hat for c in est) / len(est)
        # SE of the mean over 400 slates at G=12: 0.1348 / sqrt(400) = 0.0067.
        assert abs(mean) < 0.02, mean
        assert all(c.usable for c in est)

    def test_h0_standard_error_matches_the_realized_spread(self) -> None:
        rng = random.Random(9)
        est = [
            slate_cross_game_rho(f"s{i}", _independent_slate(rng, 10, 0.30))
            for i in range(600)
        ]
        realized = statistics.pstdev([c.rho_hat for c in est])
        assert est[0].se_h0 == (2.0 / (10 * 9)) ** 0.5
        assert realized == __import__("pytest").approx(est[0].se_h0, rel=0.15)


class TestDetectsCorrelatedModelError:
    def test_planted_common_factor_shows_up_positive(self) -> None:
        rng = random.Random(17)
        per = {
            f"s{i}": slate_cross_game_rho(
                f"s{i}", _common_factor_slate(rng, 12, 0.25, 0.30)
            )
            for i in range(300)
        }
        rho, se, n = pool_slates(per)
        assert n == 300
        assert rho > 0.15, rho          # a 0.30 latent factor is visible
        assert rho / se > 5.0           # and unmistakable once pooled

    def test_pool_is_zero_when_nothing_usable(self) -> None:
        one_game = slate_cross_game_rho(
            "s", [SettledLeg("G0", 0.2, True), SettledLeg("G0", 0.2, False)]
        )
        assert not one_game.usable
        assert pool_slates({"s": one_game}) == (0.0, 0.0, 0)


class TestShapeGuards:
    def test_busy_game_does_not_outweigh_a_quiet_one(self) -> None:
        """One game carrying 6 tickets must not dominate: the estimator works on
        GAME-level residuals (one draw per game), not per-ticket."""
        many = [SettledLeg("G0", 0.2, True) for _ in range(6)]
        one = [SettledLeg("G1", 0.2, True)]
        a = slate_cross_game_rho("s", many + one)
        b = slate_cross_game_rho(
            "s", [SettledLeg("G0", 0.2, True), SettledLeg("G1", 0.2, True)]
        )
        assert a.rho_hat == b.rho_hat
        assert a.positions == 7 and b.positions == 2

    def test_degenerate_marginals_are_dropped_not_clamped(self) -> None:
        legs = [
            SettledLeg("G0", 0.30, True),
            SettledLeg("G1", 0.30, False),
            SettledLeg("G2", 0.0005, True),   # 1/sqrt(p(1-p)) would explode
            SettledLeg("G3", 0.9999, False),
        ]
        c = slate_cross_game_rho("s", legs)
        assert c.dropped_positions == 2
        assert c.positions == 2
        assert c.games == 2

    def test_fewer_than_two_games_is_not_usable(self) -> None:
        c = slate_cross_game_rho("s", [SettledLeg("G0", 0.4, True)])
        assert not c.usable
        assert c.rho_hat == 0.0
        assert c.games == 1


class TestNeverGates:
    def test_log_fields_pin_gate_false(self) -> None:
        c = slate_cross_game_rho(
            "s",
            [
                SettledLeg("G0", 0.3, True),
                SettledLeg("G1", 0.3, False),
                SettledLeg("G2", 0.3, True),
            ],
        )
        fields = c.as_log_fields()
        assert fields["gate"] is False
        assert set(fields) == {
            "slate",
            "games",
            "positions",
            "dropped_positions",
            "rho_hat",
            "se_h0",
            "z_score",
            "mean_residual",
            "usable",
            "gate",
        }

    def test_module_is_imported_by_no_decision_path(self) -> None:
        """ARCHITECTURE PIN: instrumentation only. If a gate ever starts reading
        this module, this test is the tripwire that says so out loud."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "combomaker"
        importers = [
            p
            for p in root.rglob("*.py")
            if "cross_game_residual" in p.read_text(encoding="utf-8")
            and p.name != "cross_game_residual.py"
        ]
        assert importers == [], importers
