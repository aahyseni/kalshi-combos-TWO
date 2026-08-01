"""KILL-ANCHORED BOOK GATE (2026-07-29) + the RATIFIED det-max demotion (2026-07-31).

THE DEFECT. The operator ratified ``P(KILL-night) <= 2%`` at a 12%-of-bankroll
KILL line and ``portfolio_tail_prob_gate`` has been ARMED live since 2026-07-25 —
but the armed form thresholds on ``portfolio_cvar_frac x bankroll`` (0.35 live),
not on ``hard_trip_frac x bankroll`` (0.12). At the live det-max fraction 0.36
that is 97.22% of the COMONOTONE MAXIMUM loss, so it never fired: 0 occurrences
of the breach string in 104,803 live ``risk_audit`` rows.

THE RATIFICATION (operator, 2026-07-31, verbatim intent): "ratify and finish
number 2. i think based on our past fills and quoting if we raise to 2k we'd
get a very diverse and profitable book. but we need to open it up as it was on
7/29 just with more capacity."  So ARMING does two things: the KILL gate
becomes the GOVERNING constraint, and det-max DEMOTES to the model-free
ruin-anchor backstop ``cap_family.det_max_backstop_frac()`` (= 1 -
RUIN_FLOOR_FRAC = 0.70 of bankroll). The demotion applies ONLY while the
governing gate can run (tail-prob form armed + KILL line threaded) — no
governor, no demotion, at BOTH sites (quote-time cap and confirm-time gate).

THE READING RATIFICATION (operator, 2026-08-01, verbatim): "Reading b" — the
"ruin floor 30%" anchor means an EQUITY FLOOR (always keep >= 30% of
bankroll), so the backstop is (1 - 0.30) x risk_bankroll = 0.70B, NOT the
30%-drawdown reading (0.30B). Ratified with the collision stated three times
with the numbers: Reading A (30% DRAWDOWN) is the convention enforced
elsewhere in this codebase (``cap_family.RUIN_FLOOR_FRAC`` = 0.30 as drawdown
DISTANCE; ``book_risk`` p_ruin at the 0.70 surviving-equity floor with the 5%
budget — that MC gate STAYS ARMED unchanged), and the operator explicitly
chose B for the det-max backstop axis knowing it, with the copula caveat on
the table (at cross-rho 0.25 the target 64-ticket book measures P(KILL)
20.8%; the KILL gate + book diversity is the management mechanism;
det-max@0.70B is the model-free floor guaranteeing equity >= 30% under ANY
copula). CANARY NOTE: this file's canary was
``test_det_max_ceiling_is_unmoved_by_the_arming_flag`` (it refused ANY
re-landing of the demotion). It is updated — not deleted — for the
ratification: ``test_armed_ceiling_is_the_backstop_disarmed_is_todays_wall``
accepts exactly the RATIFIED reading-B form and still goes red on an
UNRATIFIED landing shape (reverting the demotion, widening the DISARMED
wall), and ``test_demotion_never_applies_without_its_governor`` (+ its
confirm-site twin) refuses an UNGUARDED landing (demotion without the
governing gate).

Every book below is built at the LIVE bankroll of the measurement day
($2,940.28 exchange equity), so the dollars in the assertions are the operator's
actual walls: KILL line $352.83, the disarmed det-max wall $1,058.50 (0.36),
and the armed backstop $2,058.19 (0.70).

Pinned here, one test per requirement:
  * the gate FIRES on a book that breaches 2% at the 12% line, and does NOT
    fire on the SAME book at the old 0.35 threshold (the defect, reproduced);
  * a 2-ticket concentrated book at P(KILL-night) 4.9% is REFUSED where the
    $1,058.50 dollar wall ADMITTED it (its premium is only $760);
  * a 64-ticket spread book at P(KILL-night) 0.6% ($1,983.64 premium,
    EV +$73.85) is ADMITTED armed — the RATIFIED capacity case, on both the
    cap site and the confirm site — and still REFUSED disarmed;
  * the armed det-max wall is the backstop and it still ENFORCES: books past
    $2,058.19 are refused however diversified (no diversifier bypass);
  * the demotion NEVER applies without its governor (half-wired states keep
    today's 0.36 wall, both sites);
  * the residual model risk the ratification ACCEPTED is measured and pinned
    (ruin-convention collision + one-factor copula sweep) so the trade the
    operator made stays visible;
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
from combomaker.risk.cap_family import RUIN_FLOOR_FRAC, det_max_backstop_frac
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
DET_MAX_FRAC = Fraction(36, 100)       # the DISARMED det-max wall
KILL_TAIL_PROB = 0.02                  # ratified probability budget
# The ARMED backstop — DERIVED from the ruin anchor, never typed here: the
# import IS the test that no consumer carries its own copy of the fraction.
BACKSTOP_FRAC = det_max_backstop_frac()
RUIN_DISTANCE_FRAC = Fraction(30, 100)  # cap_family.RUIN_FLOOR_FRAC — a DRAWDOWN

KILL_LINE_CC = threshold_cc(KILL_FRAC, BANKROLL)            # 3_528_333 = $352.83
DOLLAR_WALL_CC = threshold_cc(DET_MAX_FRAC, BANKROLL)       # $1,058.50
BACKSTOP_CC = threshold_cc(BACKSTOP_FRAC, BANKROLL)         # $2,058.19
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
# the dollar wall REFUSES it. This is the flow the defect was turning away, and
# the book the RATIFIED demotion admits ($1,983.64 < the $2,058.19 backstop).
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


class TestBackstopDerivation:
    def test_backstop_is_derived_from_the_ruin_anchor_not_typed(self) -> None:
        """The ratification's own rule: NEVER a typed fraction. The backstop
        must be exactly ``1 - RUIN_FLOOR_FRAC`` as an exact Fraction, and the
        anchor itself must still be the ratified 0.30."""
        assert RUIN_FLOOR_FRAC == 0.30
        assert BACKSTOP_FRAC == Fraction(7, 10)
        assert BACKSTOP_FRAC == 1 - Fraction(30, 100)
        # Exactness: no float round-trip residue (0.2999... would poison the
        # integer threshold arithmetic).
        assert BACKSTOP_FRAC.denominator == 10


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

    def test_spread_book_is_admitted_by_the_ratified_demotion(
        self, spread
    ) -> None:
        """THE RATIFIED CAPACITY CASE (operator 2026-07-31, "number 2"). The
        64-ticket spread book: P(KILL-night) 0.6% — under a third of the
        budget — on $1,983.64 of premium. Disarmed, the $1,058.50 dollar wall
        refuses it (the wrong-direction refusal the operator named "we need to
        open it up"). ARMED, det-max sits at the $2,058.19 ruin-anchor
        backstop and the governing KILL gate rates the book INSIDE budget, so
        it is admitted OUTRIGHT — no breach on either portfolio axis."""
        book, snap = spread
        premium = sum(x.max_loss_cc for x in book)
        assert premium > DOLLAR_WALL_CC          # the $1,058.50 wall refuses it
        assert premium < BACKSTOP_CC             # ...the backstop admits it
        assert _p_kill(snap) == pytest.approx(0.006, abs=1e-6)
        assert _p_kill(snap) < KILL_TAIL_PROB    # the governing gate: inside

        shadow = _reasons(_limits(armed=False), snap)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in shadow   # today: refused

        armed = _reasons(_limits(armed=True), snap)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX not in armed  # ratified: open
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
        """The backstop ENFORCES. A book whose comonotone worst case passes
        the ruin-anchor backstop is refused no matter how beautifully
        diversified it is or how small its modeled tail is — that is the
        operator's stated invariant ("even if the ENTIRE admitted book loses
        simultaneously") holding at the new wall."""
        # 160 INDEPENDENT games, $15 each = $2,400 of premium > $2,058.19.
        book = _book(160, 150_000, 9_500)
        snap = _snapshot(book, 0.02)
        premium = sum(x.max_loss_cc for x in book)
        assert premium > BACKSTOP_CC
        assert _p_kill(snap) < KILL_TAIL_PROB     # the MODEL says it is safe...
        armed = _reasons(_limits(armed=True), snap)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in armed
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in armed   # ...backstop refuses

    def test_armed_ceiling_is_the_backstop_disarmed_is_todays_wall(self) -> None:
        """THE DEMOTION, pinned at the cap site. A book sitting between
        today's $1,058.50 wall and the $2,058.19 backstop — the exact band the
        ratification opens — is refused DISARMED and admitted ARMED. If
        someone reverts the demotion, the armed half goes red; if someone
        widens the disarmed wall, the disarmed half goes red."""
        # $1,500 of premium: past the 0.36 wall, inside the backstop.
        book = _book(100, 150_000, 9_500)
        snap = _snapshot(book, 0.02)
        premium = sum(x.max_loss_cc for x in book)
        assert DOLLAR_WALL_CC < premium < BACKSTOP_CC
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _reasons(
            _limits(armed=False), snap
        )
        armed = _reasons(_limits(armed=True), snap)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX not in armed
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in armed

    def test_det_max_is_not_a_diversifier_bypass(self) -> None:
        """NO DOLLAR-CAP BYPASS FOR DIVERSIFIERS (operator ruling, standing).
        Certification is pairwise LOCAL; det-max is GLOBAL. A book of 300
        perfectly independent games — every pair individually 'diversifying' —
        is still refused at the backstop."""
        book = _book(300, 150_000, 9_500)         # $4,500 of premium
        snap = _snapshot(book, 0.02)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _reasons(
            _limits(armed=True), snap
        )

    def test_demotion_never_applies_without_its_governor(self) -> None:
        """FAIL-CLOSED PAIRING: the demotion is a loosening, so it must never
        outlive the gate that justifies it. Armed flag on but the
        tail-probability form OFF (half-wired deployment) ⇒ the wall does NOT
        move — the band book stays refused at today's 0.36."""
        book = _book(100, 150_000, 9_500)
        snap = _snapshot(book, 0.02)
        premium = sum(x.max_loss_cc for x in book)
        assert DOLLAR_WALL_CC < premium < BACKSTOP_CC
        reasons = _reasons(
            _limits(armed=True, portfolio_tail_prob_gate=False), snap
        )
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons

    def test_armed_breach_detail_names_the_backstop(self) -> None:
        """The live tape is audited off breach strings — the armed refusal
        must say which wall refused (the backstop), not claim the 0.36 wall
        that was not tested."""
        book = _book(160, 150_000, 9_500)
        snap = _snapshot(book, 0.02)
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
            if b.reason is ReasonCode.SKIP_PORTFOLIO_DET_MAX
        )
        assert "ruin-anchor backstop" in detail
        assert f"{BACKSTOP_CC}cc" in detail


class TestDemotionResidualRiskOnRecord:
    """The measured caveats the operator ratified OVER (2026-07-31). These are
    not refusal grounds any more — they are the RECORD of the trade, pinned so
    the accepted residual risk stays visible and so nobody later calls the
    demotion a free lunch."""

    def test_the_ruin_convention_collision_is_pinned(self) -> None:
        """The backstop reads "ruin floor 30%" as an EQUITY floor at 0.30 x
        bankroll (⇒ max simultaneous loss 0.70). The ENFORCED p_ruin axis
        reads ruin as a 30% DRAWDOWN (equity < 0.70 x bankroll;
        ``cap_family.RUIN_FLOOR_FRAC = 0.30`` is that distance and
        ``book_risk``'s default floor is the 0.70 complement). Under the
        enforced convention the backstop permits a comonotone collapse of
        2.33x the ruin distance and 5.83x the KILL line. RATIFIED with this
        collision on the table — pinned so the numbers never drift silently."""
        assert RUIN_FLOOR_FRAC == 0.30
        assert BACKSTOP_CC / RUIN_CC == pytest.approx(2.333, abs=0.01)
        assert BACKSTOP_CC / KILL_LINE_CC == pytest.approx(5.833, abs=0.01)
        # The enforced MC floor is the 0.70 complement (the OTHER convention).
        from inspect import signature

        from combomaker.sim.book_risk import compute_book_risk as cbr

        assert signature(cbr).parameters["ruin_floor_frac"].default == 0.70

    def test_the_admitted_book_under_correlated_model_error(
        self, spread
    ) -> None:
        """The 64-ticket book the demotion admits, re-measured with a
        one-factor copula on the LOSS indicator. It is inside the 2% KILL
        budget only at rho = 0 — the very assumption the governing MC makes.
        rho = 0.25 is the sensitivity already on record for this book (ES99
        $221.64 -> $280.65, EV +$10.84 -> -$4.25). This is the residual risk
        the ratification ACCEPTED; what still guards it live is the p_ruin
        <= budget candidate gate and the KILL gate itself — both computed
        FROM the joint model."""
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
        # Inside budget under ratified independence...
        assert measured[0.0][0] < KILL_TAIL_PROB
        assert measured[0.0][1] < 0.001
        # ...at the ALREADY-MEASURED rho = 0.25 it blows the 2% budget by >8x...
        assert measured[0.25][0] > 8 * KILL_TAIL_PROB
        # ...and total correlation failure makes the 30%-drawdown event ~29.5%.
        assert measured[1.0][1] == pytest.approx(0.295, abs=0.02)

    def test_the_backstop_is_still_the_only_copula_free_bound(
        self, spread
    ) -> None:
        """What the backstop still guarantees, copula-free: ``Loss(w) <= D``
        for EVERY outcome, and armed D = $2,058.19, so even the rho = 1
        collapse the model rates at ~0 cannot take more than 70% of bankroll —
        the operator's stated floor holds by construction. The admitted spread
        book sits under it."""
        book, snap = spread
        premium = sum(x.max_loss_cc for x in book)
        assert snap.deterministic_max_loss_cc == premium
        assert premium < BACKSTOP_CC      # the stated invariant, enforced
        assert premium > RUIN_CC          # ...and past the ENFORCED 30%-drawdown
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
        marginal makes ``usable`` False; neither the re-anchor nor the demotion
        may reach the threshold arithmetic at all in that state."""
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
    re-anchor AND the demotion therefore land on BOTH sites off the SAME flag
    with the SAME governor guard."""

    def _gate(self, book: list[OpenPosition], p_hit: float, **kw):
        from combomaker.sim.book_risk import evaluate_candidate_book_risk

        # Defaults mirror the LIVE armed wiring; individual tests override
        # (e.g. ``tail_prob_gate=False`` to prove the governor guard), so
        # these are setdefault, not hard kwargs.
        kw.setdefault("tail_prob_gate", True)
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
            # ``cap_family.RUIN_FLOOR_FRAC`` — the exact name collision pinned
            # in TestDemotionResidualRiskOnRecord. Inert here (no equity / no
            # ruin budget supplied); passed so the signature matches the live
            # call.
            ruin_floor_frac=float(1 - RUIN_DISTANCE_FRAC),
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

    def test_spread_candidate_confirms_under_the_ratified_demotion(
        self,
    ) -> None:
        """THE RATIFIED CAPACITY CASE, confirm site (the cap-side twin is
        ``test_spread_book_is_admitted_by_the_ratified_demotion``). Disarmed:
        declined on today's det-max wall. ARMED: the governing tail axis rates
        the book inside the 2% budget at the 12% line AND det-max sits at the
        backstop, so the candidate CONFIRMS — the exact flow the operator said
        to stop turning away."""
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
        assert armed.confirm, armed.decline_reason      # RATIFIED: admitted

    def test_confirm_gate_backstop_still_refuses(self) -> None:
        # $2,400 of premium > the $2,058.19 ruin-anchor backstop — armed, and
        # with a modeled tail the KILL-anchored axis rates as safe, the
        # copula-free backstop still refuses.
        book = _book(160, 150_000, 9_500)
        armed = self._gate(book, 0.02, kill_anchored_book_gate=True)
        assert not armed.confirm
        assert armed.decline_reason == "post_deterministic_max_over_budget"

    def test_confirm_demotion_never_applies_without_its_governor(self) -> None:
        """Half-wired states keep today's wall at the CONFIRM site too: armed
        flag on but tail-prob form off ⇒ a band book ($1,500, inside the
        backstop, past today's wall) still declines on det-max."""
        book = _book(100, 150_000, 9_500)
        armed_no_gov = self._gate(
            book, 0.02, kill_anchored_book_gate=True, tail_prob_gate=False
        )
        assert not armed_no_gov.confirm
        assert armed_no_gov.decline_reason == "post_deterministic_max_over_budget"

    def test_missing_anchor_degrades_to_todays_behaviour(self) -> None:
        """Fail closed on a half-wired deployment: armed but with no KILL line
        supplied must NOT silently gate on nothing — the tail axis falls back
        to the threshold in force today AND the det-max wall does not move (a
        band book still declines)."""
        from combomaker.sim.book_risk import evaluate_candidate_book_risk

        book = _book(
            CONCENTRATED["n"],  # type: ignore[arg-type]
            CONCENTRATED["premium_each_cc"],  # type: ignore[arg-type]
            CONCENTRATED["price_cc"],  # type: ignore[arg-type]
        )
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
        band = _book(100, 150_000, 9_500)
        v2 = evaluate_candidate_book_risk(
            band[:-1],
            band[-1],
            marginals=lambda _t: 0.02,
            n_samples=40_000,
            seed=11,
            bankroll_cc=BANKROLL,
            portfolio_cvar_frac=float(Fraction(35, 100)),
            portfolio_det_max_frac=float(DET_MAX_FRAC),
            tail_prob_gate=True,
            kill_tail_prob=KILL_TAIL_PROB,
            kill_anchored_book_gate=True,
            hard_trip_frac=None,          # no KILL line ⇒ no demotion either
        )
        assert not v2.confirm
        assert v2.decline_reason == "post_deterministic_max_over_budget"


class TestShadowByteIdentical:
    def test_golden_captured_at_head_still_reproduces(self) -> None:
        """The load-bearing claim: with the flag OFF nothing moved — including
        after the demotion (the demotion is inside the armed branch and can
        never touch a disarmed threshold). The golden was generated by
        ``tools/diagnostics/kill_anchor_shadow_golden.py`` BEFORE the change,
        so this compares the new code to the OLD code's actual output — 20,000
        randomized limit cases (every axis, every flag combination, usable and
        unusable snapshots) and 2,000 randomized candidate-gate books — not to
        itself."""
        from tools.diagnostics import kill_anchor_shadow_golden as g

        path = Path(__file__).parent / "fixtures" / "kill_anchor_shadow_golden.json"
        gold = json.loads(path.read_text())
        got = g.build(gold["limits_cases"], gold["gate_cases"])
        assert g.digest(got) == g.digest(gold)
        assert got == gold
