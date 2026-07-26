"""Regression tests for the 2026-07-26 BOOK-RISK AXIS SPLIT.

The book model now carries TWO EXPLICITLY NAMED JOINTS and each AXIS picks one BY
NAME (``sim/book_model.py``):

  * ``corr_location_point`` — the EXACT PER-PAIR matrix at the pricing band. The
    joint the fills were PRICED on, and the only one the LOCATION axis (ev_cc,
    ev_stderr_cc, std_cc, p_profit/P(book), p_night, p_loss_worse_than) may use.
  * ``corr_tail_stress_{low,point,high}`` — the game-wide COLLAPSE (max of a
    game's pair rhos at ``high``, min at ``low``, mean at ``point``, written onto
    every pair in that game). Deliberately coarser and deliberately more adverse:
    a crude stand-in for the TAIL DEPENDENCE a Gaussian copula at low per-pair rho
    does not have (real games blow up together). EVERY ENFORCED GATE rides it.

DEFECT A — GAME-RHO COLLAPSE ON THE LOCATION AXIS. Marking the book on the
collapse inflated 636 ordered pairs and deflated ZERO on the live 48-position
book, lifted the within-game off-diagonal mean +0.077 -> +0.755, and FLIPPED 53
measured-NEGATIVE pairs to +0.95 — measured within-game HEDGES re-marked as
near-comonotone. It was also NON-STATIONARY: adding ANY leg to a game can only
raise that game's max, so the marked book degraded monotonically as the book grew
in a game, INCLUDING when the added position was a hedge.

DEFECT B — BAND MISMATCH. ``p_book`` / ``ev_cc`` / ``p_night`` were published from
the ADVERSE book while the quotes were PRICED from the point joint, so a fill sold
at fair+markup marked NEGATIVE on arrival BY CONSTRUCTION.

THE CONSERVATIVE RESOLUTION (what these tests pin): fixing A and B must NOT touch
the gates. ``TestGatingIsBitIdenticalToThePreSplitBuild`` is the load-bearing
guard — every enforced gating field must be bit-identical to the pre-split
(game-collapsed) build and must be provably INDEPENDENT of the pricing joint.

Testing isolation (hard rule 8): nothing here edits a live module. The PRE-SPLIT
collapse is reimplemented HERE, test-side, and applied by swapping matrices onto a
frozen ``BookModel`` via ``dataclasses.replace`` — the live builder is only ever
called, never modified.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.pricing.copula import build_block_corr
from combomaker.pricing.grouping import game_key
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim import book_risk as book_risk_module
from combomaker.sim.book_model import (
    DEFAULT_FLAT_BAND,
    BookModel,
    WithinGameRhoProvider,
    build_book_model,
)
from combomaker.sim.book_risk import (
    LOCATION_JOINT,
    NO_JOINT,
    TAIL_STRESS_JOINT,
    compute_book_risk,
    evaluate_candidate_book_risk,
)
from combomaker.sim.engine import simulate

GAME = "KXWCGAME-26TESTX"
OTHER_GAME = "KXWCGAME-26TESTY"

# EVERY field ``risk/limits.py`` enforces off a book-risk snapshot: the
# tail-probability KILL form (limits.py portfolio_kill_tail_prob reads
# ``loss_quantiles_cc``), the CVaR gate (portfolio_cvar_frac reads
# ``governing_model_es_99_cc``), the ruin gate (portfolio_ruin_prob_budget reads
# ``p_ruin`` / ``p_ruin_upper``), and the deterministic budget.
GATING_FIELDS = (
    "governing_model_es_99_cc",
    "es_99_cc",
    "production_es_99_cc",
    "challenger_es_99_cc",
    "bridge_es_99_cc",
    "var_99_cc",
    "p_ruin",
    "p_ruin_upper",
    "loss_quantiles_cc",
    "deterministic_max_loss_cc",
    "mutex_aware_det_max_cc",
    "per_game_tail_cc",
    "per_leg_tail_cc",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    price_cc: int = 4_000,
    centi: int = 100,
    side: Side = Side.NO,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=side,
        contracts=CentiContracts(centi),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
    )


def _band_provider(
    bands: dict[frozenset[str], tuple[float, float, float]],
) -> WithinGameRhoProvider:
    """A ``WithinGameRhoProvider`` over an explicit unordered-pair → band map.
    Unmapped pairs return None (the builder then uses its flat default), exactly
    as the live ``_DictWithinGameRho`` worker provider behaves."""

    def provider(a: str, b: str) -> tuple[float, float, float] | None:
        return bands.get(frozenset((a, b)))

    return provider


def _old_collapse(
    model: BookModel,
    provider: WithinGameRhoProvider,
    band_idx: int,
    *,
    flat_band: tuple[float, float, float] = DEFAULT_FLAT_BAND,
) -> np.ndarray:
    """THE PRE-SPLIT PATH, reimplemented test-side (never imported from live code).

    One ``(game_members, rho)`` block per GAME with rho = max(pair rhos) at the
    high band, min at low, mean at point. This is what git HEAD built for ALL
    THREE bands and fed to every axis; after the split it is what the TAIL axis
    still rides (``corr_tail_stress_*``) and what the LOCATION axis must NOT."""
    n = len(model.legs)
    ticker_by_index = [""] * n
    for ticker, i in model.leg_index.items():
        ticker_by_index[i] = ticker
    members: dict[str, list[int]] = {}
    for i in range(n):
        event = model.event_by_index.get(i)
        if event:
            members.setdefault(game_key(event), []).append(i)
    blocks: list[tuple[list[int], float]] = []
    for mem in members.values():
        if len(mem) < 2:
            continue
        rhos: list[float] = []
        for a in range(len(mem)):
            for b in range(a + 1, len(mem)):
                band = provider(ticker_by_index[mem[a]], ticker_by_index[mem[b]])
                rhos.append((band or flat_band)[band_idx])
        if band_idx == 2:
            rho = max(rhos)
        elif band_idx == 0:
            rho = min(rhos)
        else:
            rho = float(np.mean(rhos))
        blocks.append((mem, rho))
    return build_block_corr(n, blocks, default_rho=0.0)


def _location_is_the_collapse(
    model: BookModel, provider: WithinGameRhoProvider
) -> BookModel:
    """The SAME book with the PRE-SPLIT collapse swapped onto the LOCATION joint
    (the tail joint is left alone — it already IS the collapse). Reproduces the
    pre-split behaviour of the location axis exactly."""
    return dataclasses.replace(
        model, corr_location_point=_old_collapse(model, provider, 1)
    )


# ---------------------------------------------------------------------------
# 1. SIGN-FLIP GUARD (location joint)
# ---------------------------------------------------------------------------
class TestSignFlipGuard:
    """A measured-NEGATIVE within-game pair must STAY negative in the PRICING
    joint. The game-wide max wrote the game's most-positive rho onto it (53 live
    pairs flipped from measured-negative to +0.95 — real hedges re-marked as
    near-comonotone)."""

    # A,B measured NEGATIVE (a same-game hedge pair); A,C and B,C positive.
    BANDS = {
        frozenset(("A", "B")): (-0.60, -0.40, -0.25),
        frozenset(("A", "C")): (+0.20, +0.35, +0.50),
        frozenset(("B", "C")): (+0.20, +0.35, +0.50),
    }

    def _model(self) -> tuple[BookModel, WithinGameRhoProvider]:
        provider = _band_provider(self.BANDS)
        position = _pos(
            "p1",
            (
                LegRef("A", GAME, "yes"),
                LegRef("B", GAME, "yes"),
                LegRef("C", GAME, "yes"),
            ),
        )
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        return model, provider

    def test_measured_negative_pair_stays_negative_in_the_pricing_joint(
        self,
    ) -> None:
        model, _ = self._model()
        ia, ib = model.leg_index["A"], model.leg_index["B"]
        m = model.corr_location_point
        assert m[ia, ib] < 0.0, "hedge pair flipped positive in the pricing joint"
        assert m[ia, ib] == pytest.approx(-0.40, abs=1e-9)
        assert m[ib, ia] == pytest.approx(-0.40, abs=1e-9)

    def test_every_pair_carries_its_own_measured_rho_in_the_pricing_joint(
        self,
    ) -> None:
        # The exact per-pair matrix: NO pair inherits another pair's rho.
        model, _ = self._model()
        idx = model.leg_index
        for pair, band in self.BANDS.items():
            a, b = sorted(pair)
            i, j = idx[a], idx[b]
            assert model.corr_location_point[i, j] == pytest.approx(
                band[1], abs=1e-9
            )

    def test_tail_stress_joint_keeps_the_conservative_collapse(self) -> None:
        # THE OTHER HALF OF THE SPLIT: the tail joint deliberately does NOT carry
        # the per-pair rho — it keeps the game-wide collapse, bit-for-bit.
        model, provider = self._model()
        ia, ib = model.leg_index["A"], model.leg_index["B"]
        assert model.corr_tail_stress_high[ia, ib] == pytest.approx(+0.50)
        for band_idx, name in ((0, "low"), (1, "point"), (2, "high")):
            assert np.array_equal(
                model.corr_tail_stress_for_band(name),
                _old_collapse(model, provider, band_idx),
            ), f"tail stress joint drifted from the pre-split collapse at {name}"

    def test_cross_game_pairs_untouched_by_the_per_pair_build(self) -> None:
        # The block-diagonal-by-game structure must survive: a leg in ANOTHER
        # game still sits at cross_event_rho (0), never at a within-game rho.
        provider = _band_provider(self.BANDS)
        position = _pos(
            "p1",
            (
                LegRef("A", GAME, "yes"),
                LegRef("B", GAME, "yes"),
                LegRef("D", OTHER_GAME, "yes"),
            ),
        )
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        ia, ib, idd = (model.leg_index[t] for t in ("A", "B", "D"))
        assert model.corr_location_point[ia, ib] == pytest.approx(-0.40)
        assert model.corr_location_point[ia, idd] == pytest.approx(0.0)
        assert model.corr_location_point[ib, idd] == pytest.approx(0.0)
        assert model.corr_tail_stress_high[ia, idd] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. UNIFORM-GAME PARITY (strict generalisation)
# ---------------------------------------------------------------------------
class TestUniformGameParity:
    """On a game whose pairs all share ONE rho the per-pair location joint must be
    BYTE-IDENTICAL to the collapse (max == mean == min == that rho). The two
    joints then coincide, so the split degenerates to the pre-split single-sample
    path — proof that the change is a strict generalisation, not a re-tune."""

    def _uniform_model(self) -> tuple[BookModel, WithinGameRhoProvider]:
        band = (-0.15, 0.22, 0.63)
        tickers = ("A", "B", "C", "D")
        bands = {
            frozenset((a, b)): band
            for i, a in enumerate(tickers)
            for b in tickers[i + 1 :]
        }
        provider = _band_provider(bands)
        position = _pos(
            "p1", tuple(LegRef(t, GAME, "yes") for t in tickers)
        )
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        return model, provider

    def test_uniform_game_location_joint_matches_the_collapse(self) -> None:
        model, provider = self._uniform_model()
        # The collapse AVERAGES at the point band, so on a uniform game it
        # reproduces the shared rho only up to the float rounding of ``np.mean``
        # (the mean of three 0.10s is 0.10000000000000002). The per-pair build
        # carries the EXACT provider value: same number, minus the averaging noise.
        assert np.allclose(
            model.corr_location_point,
            _old_collapse(model, provider, 1),
            rtol=1e-15,
            atol=0.0,
        )
        # ... and the tail joint is byte-identical to the collapse on every band.
        for band_idx, name in ((0, "low"), (1, "point"), (2, "high")):
            assert np.array_equal(
                model.corr_tail_stress_for_band(name),
                _old_collapse(model, provider, band_idx),
            )

    def test_flat_default_game_matches_the_collapse(self) -> None:
        # A game with NO calibrated prior on any pair: every pair falls to the
        # SAME flat band, so location and collapse agree again.
        provider = _band_provider({})
        position = _pos(
            "p1",
            tuple(LegRef(t, GAME, "yes") for t in ("A", "B", "C")),
        )
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        assert np.allclose(
            model.corr_location_point,
            _old_collapse(model, provider, 1),
            rtol=1e-15,
            atol=0.0,
        )
        lo, pt, hi = DEFAULT_FLAT_BAND
        ia, ib = model.leg_index["A"], model.leg_index["B"]
        assert model.corr_location_point[ia, ib] == pt  # EXACT, not a float mean
        assert model.corr_tail_stress_low[ia, ib] == lo
        assert model.corr_tail_stress_high[ia, ib] == hi

    def test_parity_check_is_not_vacuous_on_a_mixed_game(self) -> None:
        # Same harness, MIXED pair rhos ⇒ the two joints MUST differ (otherwise
        # the two tests above would prove nothing).
        bands = {
            frozenset(("A", "B")): (-0.60, -0.40, -0.25),
            frozenset(("A", "C")): (+0.20, +0.35, +0.50),
            frozenset(("B", "C")): (+0.20, +0.35, +0.50),
        }
        provider = _band_provider(bands)
        position = _pos(
            "p1", tuple(LegRef(t, GAME, "yes") for t in ("A", "B", "C"))
        )
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        assert not np.array_equal(
            model.corr_location_point, model.corr_tail_stress_high
        )
        # And the collapse inflated: every off-diagonal moved UP, none down.
        off = ~np.eye(len(model.legs), dtype=bool)
        delta = (
            model.corr_tail_stress_high[off] - model.corr_location_point[off]
        )
        assert (delta >= -1e-12).all()
        assert (delta > 0).any()

    def test_single_pair_game_is_exact_under_both_joints(self) -> None:
        # A 2-leg game has exactly one pair, so max == mean == min == that rho:
        # the historical behaviour is preserved exactly and both joints coincide.
        bands = {frozenset(("A", "B")): (-0.30, 0.10, 0.77)}
        provider = _band_provider(bands)
        position = _pos("p1", (LegRef("A", GAME, "yes"), LegRef("B", GAME, "yes")))
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        assert np.array_equal(
            model.corr_location_point, _old_collapse(model, provider, 1)
        )
        for band_idx, name in ((0, "low"), (1, "point"), (2, "high")):
            assert np.array_equal(
                model.corr_tail_stress_for_band(name),
                _old_collapse(model, provider, band_idx),
            )


# ---------------------------------------------------------------------------
# 3. HEDGE MONOTONICITY GUARD (LOCATION axis)
# ---------------------------------------------------------------------------
class TestHedgeMonotonicity:
    """Adding a HEDGING position to a game must not LOWER P(book).

    P(book) is a LOCATION-axis number, so it rides the exact per-pair pricing
    joint. The collapse could only ever raise a game's ``max``, so a new leg
    re-correlated every EXISTING pair in that game upward — destroying a measured
    same-game hedge already in the book. Book here:

      pre : NO on A + NO on B, with a measured rho(A,B) = -0.90 hedge pair.
      post: + a NO whose leg C is selected NO (pays when C hits), sized so its
            own downside is small — it WINS in exactly the state that sinks the
            pre-book (A and B both hitting).

    On the pricing joint P(book) RISES. With the collapse marked onto the location
    axis (the pre-split behaviour) the game max jumps to +0.40, rho(A,B) flips
    -0.90 → +0.40, and P(book) FALLS below the pre-book.
    """

    BANDS = {
        frozenset(("A", "B")): (-0.90, -0.90, -0.90),  # measured same-game hedge
        frozenset(("A", "C")): (+0.40, +0.40, +0.40),
        frozenset(("B", "C")): (-0.40, -0.40, -0.40),
    }
    N = 200_000
    SEED = 11

    def _books(self) -> tuple[BookModel, BookModel, WithinGameRhoProvider]:
        provider = _band_provider(self.BANDS)
        p1 = _pos("p1", (LegRef("A", GAME, "yes"),), price_cc=4_000)
        p2 = _pos("p2", (LegRef("B", GAME, "yes"),), price_cc=4_000)
        # HEDGE: leg C selected NO ⇒ the combo pays v_C ⇒ this NO position WINS
        # when C hits. Priced at 1_000cc so its own loss (1_000) cannot sink the
        # book while its win (9_000) covers the pre-book's 8_000 joint loss.
        hedge = _pos("p3", (LegRef("C", GAME, "no"),), price_cc=1_000)
        marginals = lambda t: 0.5  # noqa: E731
        pre = build_book_model(
            [p1, p2], marginals=marginals, within_game_rho=provider
        )
        post = build_book_model(
            [p1, p2, hedge], marginals=marginals, within_game_rho=provider
        )
        return pre, post, provider

    def _p_book(self, model: BookModel) -> float:
        return compute_book_risk(
            model, n_samples=self.N, seed=self.SEED, band="high"
        ).p_profit

    def test_hedge_does_not_lower_p_book(self) -> None:
        pre, post, _ = self._books()
        p_pre, p_post = self._p_book(pre), self._p_book(post)
        # MC standard error at n=200k is <= 1.2e-3; a 5-sigma band is 6e-3.
        assert p_post >= p_pre - 6e-3, (
            f"hedge LOWERED P(book): pre={p_pre:.4f} post={p_post:.4f}"
        )
        # and it should measurably HELP, not merely not-hurt.
        assert p_post > p_pre

    def test_guard_still_holds_when_the_gating_band_is_point_or_low(self) -> None:
        # The location axis is band-independent BY CONSTRUCTION (it always rides
        # the pricing joint), so the guard cannot be evaded by the caller's band.
        pre, post, _ = self._books()
        for band in ("point", "low", "high"):
            p_pre = compute_book_risk(
                pre, n_samples=self.N, seed=self.SEED, band=band
            ).p_profit
            p_post = compute_book_risk(
                post, n_samples=self.N, seed=self.SEED, band=band
            ).p_profit
            assert p_post > p_pre, f"hedge lowered P(book) at band={band}"

    def test_pre_split_location_axis_fails_this_guard(self) -> None:
        # Non-vacuity: the SAME book, MARKED on the collapse (the pre-split
        # location axis), reports the hedge as a DEGRADATION.
        pre, post, provider = self._books()
        p_pre = self._p_book(_location_is_the_collapse(pre, provider))
        p_post = self._p_book(_location_is_the_collapse(post, provider))
        assert p_post < p_pre - 6e-3, (
            "pre-split path unexpectedly passed the monotonicity guard "
            f"(pre={p_pre:.4f} post={p_post:.4f})"
        )

    def test_collapse_inflated_the_existing_hedge_pair(self) -> None:
        # The mechanism, pinned: adding leg C left rho(A,B) alone in the pricing
        # joint and flipped it -0.90 → +0.40 in the collapse.
        _, post, provider = self._books()
        ia, ib = post.leg_index["A"], post.leg_index["B"]
        assert post.corr_location_point[ia, ib] == pytest.approx(-0.90)
        assert post.corr_tail_stress_high[ia, ib] == pytest.approx(+0.40)
        assert _old_collapse(post, provider, 2)[ia, ib] == pytest.approx(+0.40)


# ---------------------------------------------------------------------------
# 4. GATING BIT-IDENTITY (the conservative resolution — load-bearing)
# ---------------------------------------------------------------------------
class TestGatingIsBitIdenticalToThePreSplitBuild:
    """NO ENFORCED GATE MAY MOVE BECAUSE OF THIS CHANGE.

    Two complementary halves, both bit-exact:

      1. The TAIL joint the gates sample is byte-identical to the pre-split
         game-collapsed construction (``_old_collapse``, reimplemented test-side
         from git HEAD's build) on every band.
      2. Every enforced gating field is INVARIANT to the pricing joint: replace
         ``corr_location_point`` wholesale (with the collapse, with the identity
         matrix, with a comonotone matrix) and every gating field is bit-identical
         — so no gate can possibly be reading it.

    Together those pin the gating numbers to git HEAD's exactly: the gates sample
    the same matrix HEAD built, and nothing else this change introduced can reach
    them. A Gaussian copula at a low per-pair rho has almost no tail dependence,
    so had the exact per-pair matrix been fed to the gates instead they would have
    collapsed (measured on the live-shaped book: governing ES 103,893 -> 0,
    P(ruin) 0.0137 -> 0.0000)."""

    BANDS = {
        frozenset(("A", "B")): (-0.55, -0.35, -0.20),
        frozenset(("A", "C")): (+0.10, +0.25, +0.45),
        frozenset(("B", "C")): (+0.30, +0.55, +0.80),
        frozenset(("A", "D")): (-0.10, +0.05, +0.30),
        frozenset(("B", "D")): (+0.15, +0.40, +0.65),
        frozenset(("C", "D")): (+0.05, +0.20, +0.60),
    }
    N = 40_000
    SEED = 7
    BANKROLL = 217_974.0  # live equity cc ($2,179.74)

    def _model(self) -> tuple[BookModel, WithinGameRhoProvider]:
        provider = _band_provider(self.BANDS)
        legs = ("A", "B", "C", "D")
        positions = [
            _pos(
                f"p{i}",
                tuple(
                    LegRef(t, GAME, "yes") for t in (legs[i % 4], legs[(i + 1) % 4])
                ),
                price_cc=3_000 + 100 * i,
                centi=100 + 10 * i,
            )
            for i in range(8)
        ]
        # A second game so the block structure (and per-game tail attribution) is
        # exercised, not just one block.
        positions.append(
            _pos(
                "p-other",
                (LegRef("E", OTHER_GAME, "yes"), LegRef("F", OTHER_GAME, "yes")),
                price_cc=2_500,
            )
        )
        model = build_book_model(
            positions, marginals=lambda t: 0.55, within_game_rho=provider
        )
        return model, provider

    def _snap(self, model: BookModel, band: str = "high"):
        return compute_book_risk(
            model,
            n_samples=self.N,
            seed=self.SEED,
            band=band,
            bankroll_cc=self.BANKROLL,
            current_equity_cc=self.BANKROLL,
            ruin_fractions=(0.1, 0.3),
        )

    def test_tail_joint_is_byte_identical_to_the_pre_split_collapse(self) -> None:
        model, provider = self._model()
        for band_idx, name in ((0, "low"), (1, "point"), (2, "high")):
            assert np.array_equal(
                model.corr_tail_stress_for_band(name),
                _old_collapse(model, provider, band_idx),
            ), f"tail joint != pre-split collapse at band={name}"

    def test_the_two_joints_actually_differ(self) -> None:
        # Non-vacuity for everything below.
        model, _ = self._model()
        assert not np.array_equal(
            model.corr_location_point, model.corr_tail_stress_point
        )
        assert not np.array_equal(
            model.corr_location_point, model.corr_tail_stress_high
        )

    @pytest.mark.parametrize("band", ["high", "point", "low"])
    def test_every_gating_field_is_invariant_to_the_pricing_joint(
        self, band: str
    ) -> None:
        model, provider = self._model()
        n = len(model.legs)
        variants = {
            "pre-split collapse": _old_collapse(model, provider, 1),
            "identity": np.eye(n, dtype=np.float64),
            "comonotone": build_block_corr(
                n, [(list(range(n)), 0.999)], default_rho=0.0
            ),
        }
        base = self._snap(model, band)
        assert base.usable
        for label, corr_loc in variants.items():
            other = self._snap(
                dataclasses.replace(model, corr_location_point=corr_loc), band
            )
            for field in GATING_FIELDS:
                a, b = getattr(base, field), getattr(other, field)
                assert a == b, (
                    f"GATE MOVED: {field} changed when the pricing joint was "
                    f"replaced by {label} at band={band}: {a!r} != {b!r}"
                )

    def test_gating_fields_are_non_trivial(self) -> None:
        # The invariance above would be vacuous on an all-zero tail.
        model, _ = self._model()
        snap = self._snap(model)
        assert snap.governing_model_es_99_cc > 0.0
        assert snap.es_99_cc > 0.0
        assert snap.challenger_es_99_cc > 0.0
        assert snap.deterministic_max_loss_cc > 0.0
        assert max(snap.loss_quantiles_cc) > 0.0
        assert snap.per_game_tail_cc

    def test_the_quote_time_gate_never_reads_the_pricing_joint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``evaluate_candidate_book_risk`` is 100% gating (its candidate EV is a
        # PRE/POST difference on common random numbers that feeds the confirm
        # decision), so replacing the pricing joint wholesale must not move ONE
        # field of it. The evaluator builds its own merged model, so the swap is
        # injected by wrapping the builder it calls (test-side only — no live
        # module is edited).
        provider = _band_provider(self.BANDS)
        held = [
            _pos(
                "h1",
                (LegRef("A", GAME, "yes"), LegRef("B", GAME, "yes")),
                price_cc=3_000,
            )
        ]
        candidate = _pos(
            "cand",
            (LegRef("C", GAME, "yes"), LegRef("D", GAME, "yes")),
            price_cc=3_500,
        )

        def _eval():
            return evaluate_candidate_book_risk(
                held,
                candidate,
                marginals=lambda t: 0.55,
                within_game_rho=provider,
                n_samples=self.N,
                seed=self.SEED,
                band="high",
                bankroll_cc=int(self.BANKROLL),
                current_equity_cc=int(self.BANKROLL),
            )

        base = _eval()
        real_builder = book_risk_module.build_book_model
        n_swapped = 0

        def _swapped_builder(*args, **kwargs):
            nonlocal n_swapped
            model = real_builder(*args, **kwargs)
            if not model.legs:
                return model
            n_swapped += 1
            comonotone = build_block_corr(
                len(model.legs),
                [(list(range(len(model.legs))), 0.999)],
                default_rho=0.0,
            )
            return dataclasses.replace(model, corr_location_point=comonotone)

        monkeypatch.setattr(
            book_risk_module, "build_book_model", _swapped_builder
        )
        swapped = _eval()
        assert n_swapped == 1, "the swap never reached the evaluator's model"
        assert dataclasses.astuple(base) == dataclasses.astuple(swapped)
        assert base.candidate_ev_cc == swapped.candidate_ev_cc
        assert base.confirm == swapped.confirm


# ---------------------------------------------------------------------------
# 5. AXIS SPLIT (Defect B) — location rides the pricing joint, tail the stress
# ---------------------------------------------------------------------------
class TestAxisSplit:
    """The TAIL axes gate on the collapsed stress joint at the adverse band, while
    the LOCATION axes (ev / p_profit / p_night) ride the exact per-pair pricing
    joint."""

    # Deliberately WIDE band so point and high are far apart.
    WIDE_BAND = (-0.30, 0.05, 0.90)
    TICKERS = tuple(f"L{i}" for i in range(10))
    N = 200_000
    SEED = 7

    def _model(self) -> BookModel:
        # FIVE 2-leg NO parlays over ten legs of ONE game. Multi-leg parlays are
        # essential here: a NO parlay's payout is a PRODUCT of leg values, so its
        # MEAN moves with rho (P(all legs hit) rises with correlation). A book of
        # single-leg positions would have a correlation-INVARIANT EV by linearity
        # of expectation and could not exhibit the mismatch at all.
        provider = _band_provider(
            {
                frozenset((a, b)): self.WIDE_BAND
                for i, a in enumerate(self.TICKERS)
                for b in self.TICKERS[i + 1 :]
            }
        )
        positions = [
            _pos(
                f"parlay-{i}",
                (
                    LegRef(self.TICKERS[2 * i], GAME, "yes"),
                    LegRef(self.TICKERS[2 * i + 1], GAME, "yes"),
                ),
                price_cc=6_000,
            )
            for i in range(5)
        ]
        return build_book_model(
            positions, marginals=lambda t: 0.5, within_game_rho=provider
        )

    def test_snapshot_stamps_both_bands_and_both_joints(self) -> None:
        snap = compute_book_risk(
            self._model(), n_samples=20_000, seed=self.SEED, band="high"
        )
        assert snap.band == "high"
        assert snap.tail_joint == TAIL_STRESS_JOINT
        assert snap.location_band == "point"
        assert snap.location_joint == LOCATION_JOINT

    def test_ev_and_p_profit_ride_the_pricing_joint(self) -> None:
        model = self._model()
        snap = compute_book_risk(
            model, n_samples=self.N, seed=self.SEED, band="high"
        )
        # The pricing joint's own truth, straight from the engine.
        priced = simulate(
            model.legs,
            model.corr_location_point,
            list(model.positions),
            n_samples=self.N,
            seed=self.SEED,
        )
        assert snap.p_profit == pytest.approx(priced.p_profit, abs=0.01)
        tol = 6.0 * max(snap.ev_stderr_cc, 1.0)
        assert abs(snap.ev_cc - priced.ev_cc) < tol
        # ... and must NOT equal the adverse-band location, which is materially
        # worse (this is the +5.78pp / +$33 the defect was costing).
        adverse = simulate(
            model.legs,
            model.corr_tail_stress_high,
            list(model.positions),
            n_samples=self.N,
            seed=self.SEED,
        )
        assert adverse.p_profit < snap.p_profit - 0.02
        assert adverse.ev_cc < snap.ev_cc - tol

    def test_location_axis_does_not_move_with_the_gating_band(self) -> None:
        model = self._model()
        snaps = {
            band: compute_book_risk(
                model, n_samples=self.N, seed=self.SEED, band=band
            )
            for band in ("low", "point", "high")
        }
        # Same joint, same seed substream ⇒ bit-identical location axis.
        ref = snaps["high"]
        for band, snap in snaps.items():
            assert snap.ev_cc == ref.ev_cc, band
            assert snap.p_profit == ref.p_profit, band
            assert snap.std_cc == ref.std_cc, band

    def test_tail_axes_still_ride_the_adverse_stress_joint(self) -> None:
        model = self._model()
        snap_high = compute_book_risk(
            model, n_samples=self.N, seed=self.SEED, band="high"
        )
        snap_point = compute_book_risk(
            model, n_samples=self.N, seed=self.SEED, band="point"
        )
        # ES / VaR / governing ES must be the ADVERSE numbers, strictly worse
        # than the point-band stress joint's — the split must not relax the gate.
        assert snap_high.es_99_cc > snap_point.es_99_cc
        assert snap_high.var_99_cc > snap_point.var_99_cc
        assert snap_high.governing_model_es_99_cc >= snap_point.governing_model_es_99_cc
        assert snap_high.challenger_es_99_cc >= snap_point.challenger_es_99_cc
        # The GATING tail-probability object (risk/limits reads this one) is the
        # adverse-band envelope, not the pricing joint.
        assert max(snap_high.loss_quantiles_cc) >= max(snap_point.loss_quantiles_cc)
        # Independent check against the engine on the adverse STRESS matrix.
        adverse = simulate(
            model.legs,
            model.corr_tail_stress_high,
            list(model.positions),
            n_samples=self.N,
            seed=self.SEED,
        )
        assert snap_high.es_99_cc == pytest.approx(adverse.es_cc[0.99], rel=0.05)

    def test_p_night_rides_the_pricing_joint(self) -> None:
        model = self._model()
        realized = 1_000
        snap = compute_book_risk(
            model,
            n_samples=self.N,
            seed=self.SEED,
            band="high",
            realized_pnl_cc=realized,
        )
        priced = simulate(
            model.legs,
            model.corr_location_point,
            list(model.positions),
            n_samples=self.N,
            seed=self.SEED,
        )
        assert snap.p_night == pytest.approx(
            float(np.mean(priced.pnl_samples + realized > 0.0)), abs=0.01
        )
        adverse = simulate(
            model.legs,
            model.corr_tail_stress_high,
            list(model.positions),
            n_samples=self.N,
            seed=self.SEED,
        )
        adverse_p_night = float(np.mean(adverse.pnl_samples + realized > 0.0))
        assert adverse_p_night < snap.p_night - 0.02

    def test_identical_joints_collapse_to_one_shared_sample(self) -> None:
        # A book whose game carries ONE uniform rho: the two joints coincide, so
        # the split must degenerate to the pre-split single-sample path (no extra
        # MC cost, no behaviour change) and say so in the stamp.
        provider = _band_provider({frozenset(("A", "B")): (0.3, 0.3, 0.3)})
        position = _pos("p1", (LegRef("A", GAME, "yes"), LegRef("B", GAME, "yes")))
        model = build_book_model(
            [position], marginals=lambda t: 0.5, within_game_rho=provider
        )
        snap = compute_book_risk(model, n_samples=20_000, seed=3, band="high")
        assert snap.location_band == "high" == snap.band
        assert snap.location_joint == TAIL_STRESS_JOINT == snap.tail_joint

    def test_unknown_and_all_reserved_snapshots_do_not_claim_a_joint(self) -> None:
        # Nothing sampled ⇒ location_band == band and NEITHER joint is claimed.
        unpriceable = _pos("stuck", (LegRef("Z", GAME, "yes"),))
        model = build_book_model([unpriceable], marginals=lambda t: None)
        snap = compute_book_risk(model, n_samples=1_000, seed=1, band="high")
        assert snap.n_positions == 0
        assert snap.deterministic_max_loss_cc > 0.0
        assert snap.location_band == "high" == snap.band
        assert snap.tail_joint == snap.location_joint == NO_JOINT
        empty = build_book_model([], marginals=lambda t: 0.5)
        empty_snap = compute_book_risk(empty, n_samples=1_000, seed=1, band="high")
        assert empty_snap.location_band == "high" == empty_snap.band
        assert empty_snap.tail_joint == empty_snap.location_joint == NO_JOINT
