"""Mutex/scenario-aware deterministic max-loss (operator directive 2026-07-18).

The comonotone all-hit det-max charged mutually exclusive parlays (opposing
moneylines of one game; two champion outcomes) as if they could all hit
SIMULTANEOUSLY, taxing diversifying flow at the two det-max cap checks
(candidate-gate "post_deterministic_max_over_budget" + quote-time
SKIP_PORTFOLIO_DET_MAX). The fix (``sim.book_risk.mutex_aware_det_max_from_
units`` + the ``portfolio_det_max_mutex_aware`` knob) co-aggregates the SAME
counted losses soundly: within a game, max over provably-exclusive outcome
branches (state-exact via the DC scoreline enumeration where a plan builds;
explicit-ME-event branch-max where metadata is supplied); across games, sum;
comonotone for every unproven slice.

Mandated coverage (task items 1-9):
  1. same-game mutually-exclusive moneyline pair nets to max; the candidate
     gate admits the second parlay when the budget fits max but not sum;
  2. two different (independent) games sum, unchanged vs comonotone;
  3. mixed book = max(mutex pair) + independent third;
  4. champion ME-event legs net max-over-branches; unresolvable structure
     falls back comonotone for that slice only;
  5. per-scenario soundness: realized loss <= bound on every enumerable joint
     outcome (state source AND ME source);
  6. bound <= comonotone, always (randomized fixed-seed books);
  7. knob False -> old comonotone behaviour byte-identical on the cap decision
     (candidate gate, quote-time cap, config plumbing);
  8. E2 subset-monotonicity: resting quotes are never counted (unchanged WHAT
     counts) and removing any counted unit never increases the bound;
  9. cap-check switch: candidate gate + quote-time SKIP both gate on the new
     bound when the knob is True (public APIs), both numbers logged.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass
from fractions import Fraction

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import RiskConfig
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits, threshold_cc
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import (
    DetMaxUnit,
    _det_units_from_positions,
    compute_book_risk,
    evaluate_candidate_book_risk,
    mutex_aware_det_max_from_units,
)
from combomaker.sim.structural_book import StructuralConfigView
from tests.test_limits import CONVENTIONS

CFG = StructuralConfigView()

# --- fixture game 1: FRA vs ENG (WC knockout; KXWCGAME = regulation ML) ------
GAME1 = "26JUL18FRAENG"
ML1_EV = f"KXWCGAME-{GAME1}"
TOT1_EV = f"KXWCTOTAL-{GAME1}"
FRA_ML = f"KXWCGAME-{GAME1}-FRA"
ENG_ML = f"KXWCGAME-{GAME1}-ENG"
TOT1 = f"KXWCTOTAL-{GAME1}-3"  # over 2.5 (>= 3 goals in 90')

# --- fixture game 2: GER vs ESP (independent game) ---------------------------
GAME2 = "26JUL19GERESP"
ML2_EV = f"KXWCGAME-{GAME2}"
GER_ML = f"KXWCGAME-{GAME2}-GER"
TOT2 = f"KXWCTOTAL-{GAME2}-3"
TOT2_EV = f"KXWCTOTAL-{GAME2}"

# --- fixture game 3: GROUP-format MLS (state-exact scenario enumeration) -----
GAME3 = "26JUL20LAFCSEA"
ML3_EV = f"KXMLSGAME-{GAME3}"
TOT3_EV = f"KXMLSTOTAL-{GAME3}"
LAFC_ML = f"KXMLSGAME-{GAME3}-LAFC"
SEA_ML = f"KXMLSGAME-{GAME3}-SEA"
TOT3 = f"KXMLSTOTAL-{GAME3}-1"  # over 0.5 (>= 1 goal in 90')

# --- champion-style ME event (no pricing alias installed -> game "99" never
# parses as a match, so ONLY the explicit-ME metadata can net it) -------------
CH_EV = "KXMENWORLDCUP-99"
CH_ARG = f"{CH_EV}-ARG"
CH_FRA = f"{CH_EV}-FRA"
CH_ESP = f"{CH_EV}-ESP"
# A second ME event sharing the same game key "99" (>= 2 ME events in one
# bucket must fail closed comonotone).
CH2_EV = "KXWOMWORLDCUP-99"
CH2_USA = f"{CH2_EV}-USA"

MARGINALS: dict[str, float] = {
    FRA_ML: 0.45, ENG_ML: 0.35, TOT1: 0.48,
    GER_ML: 0.40, TOT2: 0.55,
    LAFC_ML: 0.45, SEA_ML: 0.30, TOT3: 0.85,
    CH_ARG: 0.30, CH_FRA: 0.25, CH_ESP: 0.20, CH2_USA: 0.40,
}


def marg(ticker: str) -> float | None:
    return MARGINALS.get(ticker)


def champ_me(event: str) -> bool | None:
    return True if event == CH_EV else None


def _leg(ticker: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


def _pos(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    price_cc: int = 3_000,
    risk_modeled: bool = True,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=our_side,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
        risk_modeled=risk_modeled,
    )


def _unit(
    uid: str,
    legs: tuple[LegRef, ...],
    *,
    loss: float = 3_000.0,
    side: Side = Side.NO,
    contracts: int = 100,
    modeled: bool = True,
) -> DetMaxUnit:
    # price consistent with loss for the default 1.00-contract size.
    price = int(round(loss / (contracts / 100)))
    return DetMaxUnit(
        unit_id=uid,
        our_side=side,
        contracts_centi=contracts,
        entry_price_cc=price,
        legs=legs,
        loss_cc=loss,
        risk_modeled=modeled,
    )


def bound(
    units: list[DetMaxUnit],
    *,
    reserved: float = 0.0,
    me=None,
    cfg: StructuralConfigView | None = CFG,
) -> float:
    return mutex_aware_det_max_from_units(
        units,
        reserved_loss_cc=reserved,
        marginals=marg,
        structural_cfg=cfg,
        is_me_event=me,
    )


def comono(units: list[DetMaxUnit], reserved: float = 0.0) -> float:
    return sum(u.loss_cc for u in units) + reserved


# --- (1) same-game mutually-exclusive moneyline pair -------------------------


class TestSameGameMutexPair:
    def test_state_source_nets_to_max(self) -> None:
        a = _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0)
        b = _unit("b", (_leg(ENG_ML, ML1_EV),), loss=2_000.0)
        # FRA-wins and ENG-wins cannot co-occur: max, not sum.
        assert bound([a, b]) == 3_000.0
        assert comono([a, b]) == 5_000.0

    def test_cap_admits_second_parlay_when_budget_fits_max_not_sum(self) -> None:
        # Budget thr = 0.025 x 200_000cc = 5_000cc: fits max (3_000) not sum
        # (6_000). Knob ON (default) admits the diversifying second parlay.
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        p2 = _pos("p2", (_leg(ENG_ML, ML1_EV),))
        r = evaluate_candidate_book_risk(
            [p1], p2, marginals=marg, structural_cfg=CFG,
            n_samples=4_000, seed=1,
            bankroll_cc=200_000, portfolio_det_max_frac=0.025,
        )
        assert r.candidate_ev_cc > 0.0  # NO at 3000 on a 35%-hit parlay is +EV
        assert r.confirm
        assert r.decline_reason == ""
        # Both bounds ride the verdict for decline logging / monitoring.
        assert r.post.deterministic_max_loss_cc == 6_000.0
        assert r.post.mutex_aware_det_max_cc == 3_000.0


# --- (2) two different (independent) games sum -------------------------------


class TestDifferentGamesSum:
    def test_independent_games_sum_unchanged(self) -> None:
        a = _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0)
        b = _unit("b", (_leg(GER_ML, ML2_EV),), loss=2_500.0)
        # Different games CAN co-lose (independent events): sum is required.
        assert bound([a, b]) == comono([a, b]) == 5_500.0


# --- (3) mixed book ----------------------------------------------------------


class TestMixedBook:
    def test_mutex_pair_plus_independent_third(self) -> None:
        a = _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0)
        b = _unit("b", (_leg(ENG_ML, ML1_EV),), loss=2_000.0)
        c = _unit("c", (_leg(GER_ML, ML2_EV),), loss=1_500.0)
        assert bound([a, b, c]) == 3_000.0 + 1_500.0
        assert comono([a, b, c]) == 6_500.0

    def test_multi_game_combo_rides_comonotone_residual(self) -> None:
        # A parlay spanning two games is never netted (fail-closed slice): its
        # full loss adds on top of the netted pair.
        a = _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0)
        b = _unit("b", (_leg(ENG_ML, ML1_EV),), loss=2_000.0)
        x = _unit(
            "x", (_leg(FRA_ML, ML1_EV), _leg(GER_ML, ML2_EV)), loss=1_000.0
        )
        assert bound([a, b, x]) == 3_000.0 + 1_000.0

    def test_reserved_holdings_stay_comonotone(self) -> None:
        a = _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0)
        b = _unit("b", (_leg(ENG_ML, ML1_EV),), loss=2_000.0)
        assert bound([a, b], reserved=4_000.0) == 3_000.0 + 4_000.0


# --- (4) champion ME-event legs ----------------------------------------------


class TestChampionMeEvent:
    def test_me_metadata_nets_max_over_branches(self) -> None:
        # Game "99" never parses as a match (no alias installed), so ONLY the
        # explicit-True ME metadata can prove exclusivity here.
        a = _unit("a", (_leg(CH_ARG, CH_EV),), loss=3_000.0)
        b = _unit("b", (_leg(CH_FRA, CH_EV),), loss=2_000.0)
        c = _unit("c", (_leg(CH_ESP, CH_EV),), loss=1_000.0)
        assert bound([a, b, c], me=champ_me) == 3_000.0
        # Without the metadata the slice is unproven -> comonotone.
        assert bound([a, b, c]) == 6_000.0

    def test_unresolvable_metadata_falls_back_comonotone(self) -> None:
        a = _unit("a", (_leg(CH_ARG, CH_EV),), loss=3_000.0)
        b = _unit("b", (_leg(CH_FRA, CH_EV),), loss=2_000.0)
        # is_me_event returns None (UNKNOWN) for the event -> comonotone.
        assert bound([a, b], me=lambda e: None) == 5_000.0

    def test_two_me_events_in_one_bucket_fail_closed(self) -> None:
        # Two distinct explicit-ME events sharing game key "99": the census is
        # >= 2, the branch fold fails closed to the comonotone sum.
        a = _unit("a", (_leg(CH_ARG, CH_EV),), loss=3_000.0)
        b = _unit("b", (_leg(CH2_USA, CH2_EV),), loss=2_000.0)
        me = lambda e: True if e in (CH_EV, CH2_EV) else None  # noqa: E731
        assert bound([a, b], me=me) == 5_000.0

    def test_champion_slice_fails_closed_alone_others_still_net(self) -> None:
        # Comonotone fallback is PER SLICE: the unproven champion bucket sums
        # while the structural moneyline pair still nets.
        a = _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0)
        b = _unit("b", (_leg(ENG_ML, ML1_EV),), loss=2_000.0)
        c = _unit("c", (_leg(CH_ARG, CH_EV),), loss=1_000.0)
        d = _unit("d", (_leg(CH_FRA, CH_EV),), loss=500.0)
        assert bound([a, b, c, d]) == 3_000.0 + 1_500.0


# --- (5) per-scenario soundness ----------------------------------------------


class TestPerScenarioSoundness:
    def _game3_book(self) -> list[DetMaxUnit]:
        return [
            _unit("h", (_leg(LAFC_ML, ML3_EV),), loss=3_000.0),
            _unit("z", (_leg(SEA_ML, ML3_EV),), loss=2_500.0),
            _unit(
                "ho", (_leg(LAFC_ML, ML3_EV), _leg(TOT3, TOT3_EV)), loss=1_200.0
            ),
            _unit("o", (_leg(TOT3, TOT3_EV),), loss=900.0),
            _unit("g2", (_leg(GER_ML, ML2_EV),), loss=700.0),
        ]

    def test_every_enumerable_scoreline_realizes_at_most_the_bound(self) -> None:
        units = self._game3_book()
        b = bound(units)
        assert b < comono(units)  # netting actually engaged
        # Enumerate every joint outcome: game-3 90' scorelines x game-2
        # moneyline outcomes (independent game — its parlay may co-lose).
        for h in range(7):
            for z in range(7):
                for ger_wins in (False, True):
                    hit = {
                        "h": h > z,
                        "z": z > h,
                        "ho": (h > z) and (h + z >= 1),
                        "o": h + z >= 1,
                        "g2": ger_wins,
                    }
                    realized = sum(
                        u.loss_cc for u in units if hit[u.unit_id]
                    )
                    assert realized <= b + 1e-6, (h, z, ger_wins, realized, b)

    def test_every_champion_outcome_realizes_at_most_the_bound(self) -> None:
        units = [
            _unit("a", (_leg(CH_ARG, CH_EV),), loss=3_000.0),
            _unit("b", (_leg(CH_FRA, CH_EV),), loss=2_000.0),
            _unit("c", (_leg(CH_ESP, CH_EV),), loss=1_000.0),
        ]
        b = bound(units, me=champ_me)
        # ME event: exactly one outcome (or none of the listed) settles YES.
        for winner in (CH_ARG, CH_FRA, CH_ESP, "OTHER"):
            realized = sum(
                u.loss_cc
                for u in units
                if u.legs[0].market_ticker == winner
            )
            assert realized <= b + 1e-6


# --- (6) bound <= comonotone, randomized -------------------------------------


class TestBoundNeverExceedsComonotone:
    def test_randomized_books_fixed_seed(self) -> None:
        rng = random.Random(20260718)
        legs_pool = [
            (FRA_ML, ML1_EV), (ENG_ML, ML1_EV), (TOT1, TOT1_EV),
            (GER_ML, ML2_EV), (TOT2, TOT2_EV),
            (LAFC_ML, ML3_EV), (SEA_ML, ML3_EV), (TOT3, TOT3_EV),
            (CH_ARG, CH_EV), (CH_FRA, CH_EV), (CH2_USA, CH2_EV),
        ]
        me_maps = [None, champ_me, lambda e: True]
        for trial in range(40):
            n = rng.randint(1, 7)
            units = []
            for i in range(n):
                k = rng.randint(1, 3)
                legs = tuple(
                    _leg(t, e, rng.choice(["yes", "no"]))
                    for t, e in rng.sample(legs_pool, k)
                )
                units.append(
                    DetMaxUnit(
                        unit_id=f"u{trial}:{i}",
                        our_side=rng.choice([Side.NO, Side.NO, Side.NO, Side.YES]),
                        contracts_centi=rng.choice([40, 100, 250]),
                        entry_price_cc=rng.randint(100, 9_900),
                        legs=legs,
                        loss_cc=float(rng.randint(100, 9_900)),
                        risk_modeled=rng.random() > 0.1,
                    )
                )
            reserved = float(rng.choice([0, 0, 2_500]))
            me = rng.choice(me_maps)
            b = bound(units, reserved=reserved, me=me)
            c = comono(units, reserved)
            assert b <= c + 1e-6, (trial, b, c)
            # Never below any single counted unit's loss (a one-unit scenario).
            if units:
                assert b >= max(u.loss_cc for u in units) - 1e-6


# --- (7) knob False -> old behaviour byte-identical --------------------------


@dataclass(frozen=True, slots=True)
class FakeSnap:
    """PortfolioRisk stand-in WITH the new field."""

    usable: bool
    governing_model_es_99_cc: float
    deterministic_max_loss_cc: float
    p_ruin: float = 0.0
    p_ruin_upper: float = 0.0
    mutex_aware_det_max_cc: float | None = None


@dataclass(frozen=True, slots=True)
class LegacySnap:
    """PortfolioRisk stand-in WITHOUT the new field (a pre-fix snapshot)."""

    usable: bool
    governing_model_es_99_cc: float
    deterministic_max_loss_cc: float
    p_ruin: float = 0.0


BANKROLL = 20_000_000  # $2,000 in cc


def _loose_limits(**over: object) -> RiskLimits:
    base: dict[str, object] = {
        "caps_shadow_mode": False,
        "game_loss_frac": Fraction(99, 100),
        "per_combo_loss_frac": Fraction(99, 100),
        "directional_frac": Fraction(99, 100),
        "slate_loss_frac": Fraction(99, 100),
        "daily_loss_frac": Fraction(99, 100),
        "drawdown_frac": Fraction(99, 100),
        "hard_trip_frac": Fraction(99, 100),
        "portfolio_cvar_frac": Fraction(99, 100),
        "portfolio_det_max_frac": Fraction(15, 100),
        "portfolio_ruin_prob_budget": Fraction(99, 100),
        "absolute_notional_multiple": 999,
    }
    base.update(over)
    return RiskLimits(**base)  # type: ignore[arg-type]


def _det_reasons(risk: object, **limit_over: object) -> list[ReasonCode]:
    breaches = LimitChecker(_loose_limits(**limit_over)).check(
        ExposureBook(CONVENTIONS),
        lambda t: MARGINALS.get(t),
        DailyPnl(),
        risk_bankroll_cc=BANKROLL,
        book_risk=risk,  # type: ignore[arg-type]
    )
    return [b.reason for b in breaches]


class TestKnobFalseOldBehaviour:
    def test_candidate_gate_knob_false_declines_on_comonotone(self) -> None:
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        p2 = _pos("p2", (_leg(ENG_ML, ML1_EV),))
        kwargs: dict[str, object] = dict(
            marginals=marg, structural_cfg=CFG, n_samples=4_000, seed=1,
            bankroll_cc=200_000, portfolio_det_max_frac=0.025,
        )
        on = evaluate_candidate_book_risk([p1], p2, **kwargs)  # type: ignore[arg-type]
        off = evaluate_candidate_book_risk(
            [p1], p2, det_max_mutex_aware=False, **kwargs  # type: ignore[arg-type]
        )
        assert on.confirm
        assert not off.confirm
        assert off.decline_reason == "post_deterministic_max_over_budget"
        # Knob False leaves the mutex fields uncomputed (None), so the gate
        # read exactly the comonotone number — the pre-fix decision.
        assert off.pre.mutex_aware_det_max_cc is None
        assert off.post.mutex_aware_det_max_cc is None
        # Everything except the det-max axis is byte-identical (same seed,
        # same books, same substreams).
        assert on.candidate_ev_cc == off.candidate_ev_cc
        assert on.post.governing_model_es_99_cc == off.post.governing_model_es_99_cc
        assert on.post.deterministic_max_loss_cc == off.post.deterministic_max_loss_cc

    def test_quote_time_knob_false_gates_comonotone(self) -> None:
        thr = threshold_cc(Fraction(15, 100), BANKROLL)
        snap = FakeSnap(
            usable=True,
            governing_model_es_99_cc=0.0,
            deterministic_max_loss_cc=float(thr + 1),
            mutex_aware_det_max_cc=float(thr - 1),
        )
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX not in _det_reasons(snap)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _det_reasons(
            snap, portfolio_det_max_mutex_aware=False
        )

    def test_config_knob_plumbs_to_risk_limits(self) -> None:
        assert RiskConfig().to_risk_limits().portfolio_det_max_mutex_aware is True
        assert (
            RiskConfig(portfolio_det_max_mutex_aware=False)
            .to_risk_limits()
            .portfolio_det_max_mutex_aware
            is False
        )
        # The default RiskLimits arms the knob too.
        assert RiskLimits().portfolio_det_max_mutex_aware is True


# --- (8) E2 subset-monotonicity ----------------------------------------------


class TestSubsetMonotonicity:
    def test_resting_quotes_are_never_counted_units(self) -> None:
        # WHAT counts is unchanged: the det-max folds positions (+ reserved
        # holdings) only — an ExposureBook's resting quotes contribute nothing,
        # so removing any resting quote trivially never increases the bound.
        book = ExposureBook(CONVENTIONS)
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        book.add_position(p1)
        units_before, res_before = _det_units_from_positions(
            list(book.positions.values())
        )
        from combomaker.risk.exposure import OpenQuoteRisk

        book.upsert_quote(
            OpenQuoteRisk(
                quote_id="q1", rfq_id="r1", combo_ticker="C-q1", collection=None,
                yes_bid_cc=1_000,  # type: ignore[arg-type]
                no_bid_cc=2_000,  # type: ignore[arg-type]
                contracts=CentiContracts(100),
                legs=(_leg(ENG_ML, ML1_EV),),
            )
        )
        units_after, res_after = _det_units_from_positions(
            list(book.positions.values())
        )
        assert units_after == units_before
        assert res_after == res_before

    def test_removing_any_counted_unit_never_increases_the_bound(self) -> None:
        units = [
            _unit("a", (_leg(FRA_ML, ML1_EV),), loss=3_000.0),
            _unit("b", (_leg(ENG_ML, ML1_EV),), loss=2_000.0),
            _unit(
                "ab", (_leg(FRA_ML, ML1_EV), _leg(TOT1, TOT1_EV)), loss=1_200.0
            ),
            _unit("g2", (_leg(GER_ML, ML2_EV),), loss=700.0),
            _unit("ch", (_leg(CH_ARG, CH_EV),), loss=600.0),
            _unit(
                "xg", (_leg(FRA_ML, ML1_EV), _leg(GER_ML, ML2_EV)), loss=500.0
            ),
        ]
        full = bound(units, me=champ_me)
        for i in range(len(units)):
            subset = units[:i] + units[i + 1 :]
            assert bound(subset, me=champ_me) <= full + 1e-6, units[i].unit_id


# --- (9) cap-check switch through the public APIs ----------------------------


class TestCapCheckSwitch:
    def test_snapshot_carries_both_bounds(self) -> None:
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        p2 = _pos("p2", (_leg(ENG_ML, ML1_EV),))
        m = build_book_model([p1, p2], marginals=marg)
        snap = compute_book_risk(m, n_samples=2_000, seed=0, structural_cfg=CFG)
        assert snap.deterministic_max_loss_cc == 6_000.0  # comonotone unchanged
        assert snap.mutex_aware_det_max_cc == 3_000.0  # the new sound bound

    def test_quote_time_skip_uses_the_new_bound_end_to_end(self) -> None:
        # Real snapshot -> LimitChecker: bankroll st. mutex (3_000) < thr
        # (4_500) < comonotone (6_000). Knob True: no det-max breach; knob
        # False: the old comonotone SKIP fires, with BOTH numbers logged.
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        p2 = _pos("p2", (_leg(ENG_ML, ML1_EV),))
        m = build_book_model([p1, p2], marginals=marg)
        snap = compute_book_risk(m, n_samples=2_000, seed=0, structural_cfg=CFG)
        bankroll = 30_000  # 15% -> thr 4_500cc

        def breaches(**over: object):
            return LimitChecker(_loose_limits(**over)).check(
                ExposureBook(CONVENTIONS),
                marg,
                DailyPnl(),
                risk_bankroll_cc=bankroll,
                book_risk=snap,
            )

        on = [b for b in breaches() if b.reason is ReasonCode.SKIP_PORTFOLIO_DET_MAX]
        assert on == []
        off = [
            b
            for b in breaches(portfolio_det_max_mutex_aware=False)
            if b.reason is ReasonCode.SKIP_PORTFOLIO_DET_MAX
        ]
        assert len(off) == 1
        # Both bounds are in the decline detail for live monitoring.
        assert "comonotone 6000cc" in off[0].detail
        assert "mutex-aware 3000cc" in off[0].detail

    def test_quote_time_breach_when_even_mutex_bound_over(self) -> None:
        thr = threshold_cc(Fraction(15, 100), BANKROLL)
        snap = FakeSnap(
            usable=True,
            governing_model_es_99_cc=0.0,
            deterministic_max_loss_cc=float(thr + 500),
            mutex_aware_det_max_cc=float(thr + 100),
        )
        reasons = _det_reasons(snap)
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in reasons

    def test_pre_fix_snapshot_without_field_fails_closed_comonotone(self) -> None:
        thr = threshold_cc(Fraction(15, 100), BANKROLL)
        snap = LegacySnap(
            usable=True,
            governing_model_es_99_cc=0.0,
            deterministic_max_loss_cc=float(thr + 1),
        )
        # No mutex field -> gate on the comonotone number (never fail open).
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _det_reasons(snap)

    def test_unusable_snapshot_still_fails_closed(self) -> None:
        snap = FakeSnap(
            usable=False,
            governing_model_es_99_cc=0.0,
            deterministic_max_loss_cc=0.0,
            mutex_aware_det_max_cc=0.0,
        )
        assert ReasonCode.SKIP_PORTFOLIO_DET_MAX in _det_reasons(snap)


# --- invariants on the snapshot path -----------------------------------------


class TestSnapshotInvariants:
    def test_no_mutex_structure_means_exact_equality(self) -> None:
        # Two independent games: the new field equals the comonotone number
        # exactly (equality when no mutex structure exists — task invariant).
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        p2 = _pos("p2", (_leg(GER_ML, ML2_EV),))
        m = build_book_model([p1, p2], marginals=marg)
        snap = compute_book_risk(m, n_samples=2_000, seed=0, structural_cfg=CFG)
        assert snap.mutex_aware_det_max_cc == snap.deterministic_max_loss_cc

    def test_no_structural_cfg_means_exact_equality(self) -> None:
        p1 = _pos("p1", (_leg(FRA_ML, ML1_EV),))
        p2 = _pos("p2", (_leg(ENG_ML, ML1_EV),))
        m = build_book_model([p1, p2], marginals=marg)
        snap = compute_book_risk(m, n_samples=2_000, seed=0)  # no structural cfg
        assert snap.mutex_aware_det_max_cc == snap.deterministic_max_loss_cc

    def test_all_reserved_book_field_equals_reserve(self) -> None:
        held = _pos(
            "held", (_leg("KXNBA-XX-YY", "KXNBA-XX"),), risk_modeled=False
        )
        m = build_book_model([held], marginals=marg)
        snap = compute_book_risk(m, n_samples=2_000, seed=0, structural_cfg=CFG)
        assert snap.usable
        assert snap.mutex_aware_det_max_cc == snap.deterministic_max_loss_cc
        assert snap.deterministic_max_loss_cc == float(held.max_loss_cc)

    def test_unknown_snapshot_leaves_field_none(self) -> None:
        # A missing marginal now RESERVES the held position (usable), so force a
        # genuinely-unknown model to pin the invariant: unknown ⇒ no usable field.
        p1 = _pos("p1", (_leg("NOMARG", "KXWCGAME-G9"),))
        m = dataclasses.replace(
            build_book_model([p1], marginals=lambda t: 0.5), unknown=True
        )
        snap = compute_book_risk(m, n_samples=2_000, seed=0, structural_cfg=CFG)
        assert not snap.usable
        assert snap.mutex_aware_det_max_cc is None
