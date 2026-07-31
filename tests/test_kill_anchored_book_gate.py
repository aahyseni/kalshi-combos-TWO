"""KILL-ANCHORED BOOK GATE (2026-07-29) — the re-anchor + the demoted det-max.

THE DEFECT. The operator ratified ``P(KILL-night) <= 2%`` at a 12%-of-bankroll
KILL line and ``portfolio_tail_prob_gate`` has been ARMED live since 2026-07-25 —
but the armed form thresholds on ``portfolio_cvar_frac x bankroll`` (0.35 live),
not on ``hard_trip_frac x bankroll`` (0.12). At the live det-max fraction 0.36
that is 97.22% of the COMONOTONE MAXIMUM loss, so it never fired: 0 occurrences
of the breach string in 104,803 live ``risk_audit`` rows.

Every book below is built at the LIVE bankroll of the measurement day
($2,940.28 exchange equity), so the dollars in the assertions are the operator's
actual walls: KILL line $352.83, det-max wall $1,058.50 (0.36), and the
WITHDRAWN 0.70 "backstop" $2,058.19 (kept as a constant here only so the
demotion cannot quietly return — nothing enforces it).

Pinned here, one test per operator requirement:
  * the gate FIRES on a book that breaches 2% at the 12% line, and does NOT
    fire on the SAME book at the old 0.35 threshold (the defect, reproduced);
  * a 2-ticket concentrated book at P(KILL-night) 4.9% is REFUSED where the
    $1,058.50 dollar wall ADMITTED it (its premium is only $760);
  * a 64-ticket spread book at P(KILL-night) 0.6% is rated INSIDE budget by
    the re-anchored tail axis ($1,983.64 of premium, EV +$73.85) — the axis
    that misordered the two books stops misordering them — while det-max keeps
    refusing it on the withdrawn-demotion grounds (67.5% of bankroll is a
    29.5%-probability RUIN event under a correlation-model failure);
  * det-max still ENFORCES — a genuinely over-exposed book is refused by the
    demoted backstop, and there is NO diversifier bypass;
  * stale / unusable snapshots still fail CLOSED on both axes, armed or not;
  * SHADOW is byte-identical, proven against a golden captured at HEAD BEFORE
    the change (20,000 limit cases + 2,000 candidate-gate cases).
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import (
    DailyPnl,
    LimitChecker,
    ReasonCode,
    RiskLimits,
    threshold_cc,
)
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import compute_book_risk

# The LIVE risk bankroll on the measurement day (account_standing
# exchange_equity_cc, 2026-07-29 boot): $2,940.28.
BANKROLL = 29_402_780
KILL_FRAC = Fraction(12, 100)          # ratified KILL line
DET_MAX_FRAC = Fraction(36, 100)       # the live det-max wall (UNMOVED)
KILL_TAIL_PROB = 0.02                  # ratified probability budget
# The WITHDRAWN backstop fraction, kept only so the regression it would have
# caused stays pinned by a test (see TestDetMaxDemotionWithdrawn). It is NOT a
# RiskLimits field and nothing enforces it.
WITHDRAWN_BACKSTOP_FRAC = Fraction(70, 100)
RUIN_DISTANCE_FRAC = Fraction(30, 100)  # cap_family.RUIN_FLOOR_FRAC — a DRAWDOWN

KILL_LINE_CC = threshold_cc(KILL_FRAC, BANKROLL)            # 3_528_333 = $352.83
DOLLAR_WALL_CC = threshold_cc(DET_MAX_FRAC, BANKROLL)       # $1,058.50
WITHDRAWN_BACKSTOP_CC = threshold_cc(WITHDRAWN_BACKSTOP_FRAC, BANKROLL)  # $2,058.19
RUIN_CC = threshold_cc(RUIN_DISTANCE_FRAC, BANKROLL)        # $882.08

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def _pos(i: int, game: str, contracts: int, price_cc: int) -> OpenPosition:
    return OpenPosition(
        position_id=f"p{i}",
        combo_ticker=f"COMBO-{i}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=(
            LegRef(
                market_ticker=f"L{game}",
                event_ticker=f"KXG-{game}",
                side="yes",
            ),
        ),
    )


def _book(n: int, premium_each_cc: int, price_cc: int) -> list[OpenPosition]:
    """``n`` sell-only long-NO tickets, ONE PER INDEPENDENT GAME, each risking
    ``premium_each_cc``. ``price_cc`` near $1 is the real sell-only shape: we pay
    most of the dollar and collect the thin remainder, so a loser costs the whole
    premium and a winner returns only the sliver — which is exactly why net-loss
    SHAPE (how many can break together), not premium TOTAL, is what the KILL
    budget has to measure."""
    contracts = premium_each_cc * 100 // price_cc
    return [_pos(i, f"G{i}", contracts, price_cc) for i in range(n)]


def _snapshot(positions: list[OpenPosition], p_hit: float, seed: int = 11):
    model = build_book_model(
        positions, marginals=lambda _t: p_hit, within_game_rho=None
    )
    return compute_book_risk(
        model, n_samples=80_000, seed=seed, bankroll_cc=BANKROLL
    )


def _limits(*, armed: bool, **kw: object) -> RiskLimits:
    base: dict[str, object] = dict(
        caps_shadow_mode=False,
        portfolio_tail_prob_gate=True,      # ARMED live since 2026-07-25
        portfolio_kill_tail_prob=KILL_TAIL_PROB,
        portfolio_cvar_frac=Fraction(35, 100),
        portfolio_det_max_frac=DET_MAX_FRAC,
        hard_trip_frac=KILL_FRAC,
        kill_anchored_book_gate=armed,
    )
    base.update(kw)
    return RiskLimits(**base)  # type: ignore[arg-type]


def _reasons(limits: RiskLimits, snap) -> set[ReasonCode]:
    return {
        b.reason
        for b in LimitChecker(limits).check(
            ExposureBook(CONVENTIONS),
            lambda _t: 0.5,
            DailyPnl(),
            risk_bankroll_cc=BANKROLL,
            book_risk=snap,
        )
    }


def _p_kill(snap) -> float:
    """P(book loss >= the ratified KILL line) off the SAME envelope, with the
    SAME conservative round-UP point count ``risk/limits`` §(8a) uses."""
    q = snap.loss_quantiles_cc
    return sum(1 for x in q if x >= KILL_LINE_CC) / (len(q) - 1)


# --- the shapes, measured -----------------------------------------------------
# 2 tickets, $380 of premium each, hit probability 0.024 -> P(KILL-night) 4.900%
# (2.45x the ratified 2% budget) on only $760 of premium: the dollar wall at
# $1,058.50 ADMITS it.
CONCENTRATED = dict(n=2, premium_each_cc=3_800_000, price_cc=9_500, p_hit=0.024)
# 64 tickets, $31 each, hit probability 0.295 -> P(KILL-night) 0.600% (under a
# third of the budget) on $1,983.64 of premium, EV +$73.85, ES/premium 0.193:
# the dollar wall REFUSES it. This is the flow the defect was turning away.
SPREAD = dict(n=64, premium_each_cc=310_000, price_cc=6_800, p_hit=0.295)


@pytest.fixture(scope="module")
def concentrated():
    b = _book(
        CONCENTRATED["n"],  # type: ignore[arg-type]
        CONCENTRATED["premium_each_cc"],  # type: ignore[arg-type]
        CONCENTRATED["price_cc"],  # type: ignore[arg-type]
    )
    return b, _snapshot(b, CONCENTRATED["p_hit"])  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def spread():
    b = _book(
        SPREAD["n"],  # type: ignore[arg-type]
        SPREAD["premium_each_cc"],  # type: ignore[arg-type]
        SPREAD["price_cc"],  # type: ignore[arg-type]
    )
    return b, _snapshot(b, SPREAD["p_hit"])  # type: ignore[arg-type]


class TestTheDefect:
    def test_gate_never_fires_at_the_cvar_threshold_but_fires_at_the_kill_line(
        self, concentrated
    ) -> None:
        """THE DEFECT, reproduced on one book: 4.9% of scenarios reach the
        ratified KILL line and the SHADOW (today's) gate says nothing, because
        it is asking about a threshold 3x further out."""
        book, snap = concentrated
        assert _p_kill(snap) == pytest.approx(0.049, abs=1e-6)
        assert _p_kill(snap) > KILL_TAIL_PROB          # over the ratified budget

        # today: threshold = 0.35 x bankroll = $1,029.10, which the whole book
        # ($760 of premium) cannot even reach -> P = 0 -> silence.
        cvar_thr = threshold_cc(Fraction(35, 100), BANKROLL)
        assert sum(x.max_loss_cc for x in book) < cvar_thr
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in _reasons(
            _limits(armed=False), snap
        )
        # re-anchored: the gate FIRES.
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in _reasons(
            _limits(armed=True), snap
        )

    def test_breach_detail_names_the_kill_line(self, concentrated) -> None:
        _, snap = concentrated
        breaches = LimitChecker(_limits(armed=True)).check(
            ExposureBook(CONVENTIONS),
            lambda _t: 0.5,
            DailyPnl(),
            risk_bankroll_cc=BANKROLL,
            book_risk=snap,
        )
        detail = next(
            b.detail
            for b in breaches
            if b.reason is ReasonCode.SKIP_PORTFOLIO_CVAR
        )
        assert f"P(book loss >= {KILL_LINE_CC}cc)" in detail
        assert "KILL line 3/25 bankroll" in detail


class TestWrongInBothDirections:
    def test_concentrated_book_refused_where_the_dollar_wall_admitted(
        self, concentrated
    ) -> None:
        book, snap = concentrated
        premium = sum(x.max_loss_cc for x in book)
        assert premium < DOLLAR_WALL_CC          # the $1,058.50 wall admits it
        assert _p_kill(snap) == pytest.approx(0.049, abs=1e-6)

        shadow = _reasons(_limits(armed=False), snap)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in shadow
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX not in shadow

        armed = _reasons(_limits(armed=True), snap)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in armed

    def test_spread_book_stays_refused_by_the_unmoved_det_max_wall(
        self, spread
    ) -> None:
        """The 64-ticket spread book looks safe on the MODEL (P(KILL-night)
        0.6%) and the original design moved det-max out 1.94x to admit it.
        That demotion was WITHDRAWN: the book carries $1,983.64 of premium =
        67.5% of bankroll, and under a correlation-model failure it is a
        29.5%-probability RUIN event (TestDetMaxDemotionWithdrawn). So det-max
        refuses it identically armed and disarmed — the arming flag does not
        touch this axis."""
        book, snap = spread
        premium = sum(x.max_loss_cc for x in book)
        assert premium > DOLLAR_WALL_CC          # the $1,058.50 wall refuses it
        assert premium > RUIN_CC                 # ...and it is past ruin, too
        assert _p_kill(snap) == pytest.approx(0.006, abs=1e-6)
        assert _p_kill(snap) < KILL_TAIL_PROB    # the model says it is fine

        shadow = _reasons(_limits(armed=False), snap)
        armed = _reasons(_limits(armed=True), snap)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in shadow
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in armed   # UNCHANGED by arming
        # The tail axis correctly has no complaint — det-max alone refuses.
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in armed

    def test_det_max_is_shape_blind_by_construction(
        self, concentrated, spread
    ) -> None:
        """WHY the dollar cap cannot be the judge: det-max IS the premium sum,
        so it cannot distinguish these two books' 8.2x difference in
        P(KILL-night) — it only sees which one is bigger, and gets the ORDER
        backwards (it refuses the safe one)."""
        (cbook, csnap), (sbook, ssnap) = concentrated, spread
        assert csnap.deterministic_max_loss_cc == sum(
            x.max_loss_cc for x in cbook
        )
        assert ssnap.deterministic_max_loss_cc == sum(
            x.max_loss_cc for x in sbook
        )
        assert _p_kill(csnap) > 8 * _p_kill(ssnap)
        assert (
            ssnap.deterministic_max_loss_cc > csnap.deterministic_max_loss_cc
        )


class TestDetMaxStillEnforces:
    def test_genuinely_over_exposed_book_is_still_refused(self) -> None:
        """det-max ENFORCES, armed or not. A book whose comonotone worst case
        passes ``portfolio_det_max_frac x bankroll`` is refused no matter how
        beautifully diversified it is or how small its modeled tail is."""
        # 160 INDEPENDENT games, $15 each = $2,400 of premium > $1,058.50.
        book = _book(160, 150_000, 9_500)
        snap = _snapshot(book, 0.02)
        premium = sum(x.max_loss_cc for x in book)
        assert premium > DOLLAR_WALL_CC
        assert _p_kill(snap) < KILL_TAIL_PROB     # the MODEL says it is safe...
        armed = _reasons(_limits(armed=True), snap)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in armed
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in armed   # ...det-max refuses

    def test_det_max_ceiling_is_unmoved_by_the_arming_flag(self) -> None:
        """THE WITHDRAWAL, pinned. The enforced ceiling is
        ``portfolio_det_max_frac x bankroll`` whether or not the re-anchor is
        armed — a book sitting between the real wall and the withdrawn 0.70
        backstop is refused in BOTH states. If someone re-lands the demotion,
        this test goes red."""
        # $1,500 of premium: past the $1,058.50 wall, inside the withdrawn
        # $2,058.19 "backstop" — the exact band the demotion would have opened.
        book = _book(100, 150_000, 9_500)
        snap = _snapshot(book, 0.02)
        premium = sum(x.max_loss_cc for x in book)
        assert DOLLAR_WALL_CC < premium < WITHDRAWN_BACKSTOP_CC
        for armed in (False, True):
            assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _reasons(
                _limits(armed=armed), snap
            )

    def test_det_max_is_not_a_diversifier_bypass(self) -> None:
        """NO DOLLAR-CAP BYPASS FOR DIVERSIFIERS. Certification is pairwise
        LOCAL; det-max is GLOBAL. A book of 300 perfectly independent games —
        every pair individually 'diversifying' — is still refused."""
        book = _book(300, 150_000, 9_500)         # $4,500 of premium
        snap = _snapshot(book, 0.02)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _reasons(
            _limits(armed=True), snap
        )


class TestDetMaxDemotionWithdrawn:
    """WHY the 0.70 "model-failure backstop" is not shipped (2026-07-31).

    The inherited design demoted det-max from ``portfolio_det_max_frac`` (0.36)
    to ``ruin_floor_frac`` (0.70) — 1.94x — calling it "ruin-derived". These
    tests pin the three reasons that is wrong, so the demotion cannot quietly
    return."""

    def test_the_derivation_was_self_cancelling(self) -> None:
        """``(1 - (1 - 0.70)) = 0.70`` — the "derived" ceiling is the input
        restated. And in THIS codebase ruin is a 30% DRAWDOWN, so a 0.70 x
        bankroll comonotone loss is 2.33x the ratified ruin distance and 5.83x
        the KILL line. No ratified anchor produces 0.70."""
        from combomaker.risk.cap_family import RUIN_FLOOR_FRAC as RUIN_DISTANCE

        assert RUIN_DISTANCE == 0.30          # a DISTANCE, not an equity floor
        assert 1 - (1 - WITHDRAWN_BACKSTOP_FRAC) == WITHDRAWN_BACKSTOP_FRAC
        assert WITHDRAWN_BACKSTOP_CC / RUIN_CC == pytest.approx(2.333, abs=0.01)
        assert WITHDRAWN_BACKSTOP_CC / KILL_LINE_CC == pytest.approx(
            5.833, abs=0.01
        )

    def test_the_admitted_book_is_a_ruin_event_under_correlated_model_error(
        self, spread
    ) -> None:
        """The 64-ticket book the demotion existed to admit, re-measured with a
        one-factor copula on the LOSS indicator. It is safe only at rho = 0 —
        the very assumption the governing MC makes and det-max exists to
        backstop. rho = 0.25 is the sensitivity already on record for this book
        (ES99 $221.64 -> $280.65, EV +$10.84 -> -$4.25)."""
        import numpy as np
        from scipy.stats import norm

        book, _ = spread
        n = len(book)
        prem = book[0].max_loss_cc
        win = float(book[0].contracts) * (10_000 - 6_800) / 100.0
        rng = np.random.default_rng(7)
        measured = {}
        for rho in (0.0, 0.25, 1.0):
            z = rng.standard_normal((200_000, n)) * np.sqrt(1 - rho) + (
                rng.standard_normal((200_000, 1)) * np.sqrt(rho)
            )
            lost = z < norm.ppf(0.295)
            loss = -np.where(lost, -float(prem), win).sum(axis=1)
            measured[rho] = (
                (loss >= KILL_LINE_CC).mean(),
                (loss >= RUIN_CC).mean(),
            )
        # Safe ONLY under ratified independence...
        assert measured[0.0][0] < KILL_TAIL_PROB
        assert measured[0.0][1] < 0.001
        # ...at the ALREADY-MEASURED rho = 0.25 it blows the 2% budget by >8x...
        assert measured[0.25][0] > 8 * KILL_TAIL_PROB
        # ...and total correlation failure makes it a ~29.5% RUIN event.
        assert measured[1.0][1] == pytest.approx(0.295, abs=0.02)

    def test_det_max_is_the_only_copula_free_bound(self, spread) -> None:
        """The reason the two axes must not both depend on the joint model: the
        tail gate is computed FROM the copula, det-max is not. ``Loss(w) <= D``
        for EVERY outcome, so the premium sum bounds even the rho = 1 collapse
        that the modeled tail rates at ~0."""
        book, snap = spread
        premium = sum(x.max_loss_cc for x in book)
        assert snap.deterministic_max_loss_cc == premium
        # The comonotone loss the model assigns ~0 probability is exactly D.
        assert premium > RUIN_CC          # ...and D alone is past ruin
        assert _p_kill(snap) < KILL_TAIL_PROB


class _Unusable:
    """A ``PortfolioRisk`` whose marginals came back UNKNOWN (``usable`` False)
    — the exact shape the live snapshot takes when the book cannot be measured.
    Every other field carries the real snapshot's value, so a gate that reads
    them anyway is caught rather than accidentally passing on zeros."""

    def __init__(self, real) -> None:
        self._real = real

    usable = False

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class TestFailClosed:
    @pytest.mark.parametrize("armed", [False, True])
    def test_unusable_snapshot_fails_closed_on_both_axes(
        self, concentrated, armed: bool
    ) -> None:
        """The 1,579 stale ``skip_portfolio_cvar`` declines on the live tape are
        CORRECT behaviour and must survive the re-anchor untouched. An UNKNOWN
        marginal makes ``usable`` False; the re-anchor must not reach the
        threshold arithmetic at all in that state."""
        _, real = concentrated
        unusable = _Unusable(real)
        reasons = _reasons(_limits(armed=armed), unusable)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in reasons
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons

    @pytest.mark.parametrize("armed", [False, True])
    def test_legacy_snapshot_without_envelope_falls_back_to_es(
        self, armed: bool
    ) -> None:
        """No envelope ⇒ the ES_0.99 form governs, on ``portfolio_cvar_frac``,
        armed or not: an ES magnitude is not a KILL-distance probability, so the
        fallback is deliberately NOT re-anchored (never a free pass either)."""
        book = _book(64, 700_000, 9_500)
        snap = _snapshot(book, 0.45)
        object.__setattr__(snap, "loss_quantiles_cc", ())
        assert snap.governing_model_es_99_cc > threshold_cc(
            Fraction(35, 100), BANKROLL
        )
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in _reasons(
            _limits(armed=armed), snap
        )


class TestConfirmPathMovesWithTheCap:
    """A gate LOOSER than the cap is the renege zone (won auctions declined at
    confirm); a gate STRICTER than the cap refuses flow the cap admitted. The
    re-anchor therefore has to land on BOTH sites off the SAME flag."""

    def _gate(self, book: list[OpenPosition], p_hit: float, **kw):
        from combomaker.sim.book_risk import evaluate_candidate_book_risk

        return evaluate_candidate_book_risk(
            book[:-1],
            book[-1],
            marginals=lambda _t: p_hit,
            n_samples=40_000,
            seed=11,
            bankroll_cc=BANKROLL,
            portfolio_cvar_frac=float(Fraction(35, 100)),
            portfolio_det_max_frac=float(DET_MAX_FRAC),
            # ``book_risk`` takes the SURVIVING-EQUITY floor (lifecycle default
            # 0.70) = the complement of the 30% ruin DISTANCE
            # ``cap_family.RUIN_FLOOR_FRAC`` — the exact name collision the
            # withdrawn demotion's self-cancelling derivation tripped over
            # (TestDetMaxDemotionWithdrawn). Inert here (no equity / no ruin
            # budget supplied); passed so the signature matches the live call.
            ruin_floor_frac=float(1 - RUIN_DISTANCE_FRAC),
            tail_prob_gate=True,
            kill_tail_prob=KILL_TAIL_PROB,
            hard_trip_frac=float(KILL_FRAC),
            **kw,
        )

    def test_concentrated_candidate_declines_only_when_re_anchored(
        self,
    ) -> None:
        book = _book(
            CONCENTRATED["n"],  # type: ignore[arg-type]
            CONCENTRATED["premium_each_cc"],  # type: ignore[arg-type]
            CONCENTRATED["price_cc"],  # type: ignore[arg-type]
        )
        p_hit = CONCENTRATED["p_hit"]
        shadow = self._gate(book, p_hit)  # type: ignore[arg-type]
        armed = self._gate(
            book, p_hit, kill_anchored_book_gate=True  # type: ignore[arg-type]
        )
        assert shadow.confirm, shadow.decline_reason
        assert not armed.confirm
        assert armed.decline_reason == "post_kill_tail_prob_over_budget"

    def test_spread_candidate_stays_refused_by_the_unmoved_det_max_wall(
        self,
    ) -> None:
        """THE WITHDRAWAL, pinned on the CONFIRM site (the cap-side twin is
        ``test_spread_book_stays_refused_by_the_unmoved_det_max_wall``). The
        original design armed this candidate to CONFIRM by demoting det-max to
        0.70 x bankroll; that demotion was withdrawn, so the verdict is
        IDENTICAL armed and disarmed — the arming flag touches only the tail
        axis. The tail gate runs BEFORE det-max in ``_candidate_gate``, so the
        armed decline reason being ``post_deterministic_max_over_budget`` (not
        ``post_kill_tail_prob_over_budget``) is itself the proof that the
        re-anchored tail axis rated this book INSIDE the 2% budget at the
        12% KILL line."""
        book = _book(
            SPREAD["n"],  # type: ignore[arg-type]
            SPREAD["premium_each_cc"],  # type: ignore[arg-type]
            SPREAD["price_cc"],  # type: ignore[arg-type]
        )
        p_hit = SPREAD["p_hit"]
        shadow = self._gate(book, p_hit)  # type: ignore[arg-type]
        armed = self._gate(
            book, p_hit, kill_anchored_book_gate=True  # type: ignore[arg-type]
        )
        assert not shadow.confirm
        assert shadow.decline_reason == "post_deterministic_max_over_budget"
        assert not armed.confirm                       # UNCHANGED by arming
        assert armed.decline_reason == "post_deterministic_max_over_budget"

    def test_confirm_gate_det_max_backstop_still_refuses(self) -> None:
        # $2,400 of premium > the UNMOVED $1,058.50 det-max wall (0.36 x
        # bankroll) — armed, and with a modeled tail the KILL-anchored axis
        # rates as safe, the copula-free wall still refuses.
        book = _book(160, 150_000, 9_500)
        armed = self._gate(book, 0.02, kill_anchored_book_gate=True)
        assert not armed.confirm
        assert armed.decline_reason == "post_deterministic_max_over_budget"

    def test_missing_anchor_degrades_to_todays_behaviour(self) -> None:
        """Fail closed on a half-wired deployment: armed but with no KILL line
        supplied must NOT silently gate on nothing — it falls back to the
        threshold in force today."""
        book = _book(
            CONCENTRATED["n"],  # type: ignore[arg-type]
            CONCENTRATED["premium_each_cc"],  # type: ignore[arg-type]
            CONCENTRATED["price_cc"],  # type: ignore[arg-type]
        )
        from combomaker.sim.book_risk import evaluate_candidate_book_risk

        v = evaluate_candidate_book_risk(
            book[:-1],
            book[-1],
            marginals=lambda _t: 0.024,
            n_samples=40_000,
            seed=11,
            bankroll_cc=BANKROLL,
            portfolio_cvar_frac=float(Fraction(35, 100)),
            portfolio_det_max_frac=float(DET_MAX_FRAC),
            tail_prob_gate=True,
            kill_tail_prob=KILL_TAIL_PROB,
            kill_anchored_book_gate=True,
            hard_trip_frac=None,
        )
        assert v.confirm  # == today's verdict, not a crash and not a free pass


class TestShadowByteIdentical:
    def test_golden_captured_at_head_still_reproduces(self) -> None:
        """The load-bearing claim: with the flag OFF nothing moved. The golden
        was generated by ``tools/diagnostics/kill_anchor_shadow_golden.py``
        BEFORE the change, so this compares the new code to the OLD code's
        actual output — 20,000 randomized limit cases (every axis, every flag
        combination, usable and unusable snapshots) and 2,000 randomized
        candidate-gate books — not to itself."""
        from tools.diagnostics import kill_anchor_shadow_golden as g

        path = Path(__file__).parent / "fixtures" / "kill_anchor_shadow_golden.json"
        gold = json.loads(path.read_text())
        got = g.build(gold["limits_cases"], gold["gate_cases"])
        assert g.digest(got) == g.digest(gold)
        assert got == gold
