"""P1 Stage-1 (operator 2026-08-13): the two variance levers.

(3d) STRUCTURE bound â€” accumulated committed+reserved+candidate premium on ONE
combo MARKET vs the per-combo ANCHOR fraction; candidate-key-scoped, NOT
waivable, flag-gated dark (None/off = byte-identical).
(4b) GAME-DIRECTION NET bound â€” accumulated one-direction net per game
(mutex-aware branch-max fold over committed+extra only, never resting quotes)
judged against the SUNK committed baseline; waivable; flag-gated dark.

Fixtures mirror tests/test_limits_caps.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.limits import Breach, DailyPnl, LimitChecker, RiskLimits

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

MARGINALS = {"A": 0.5, "B": 0.5}
MARG: Callable[[str], float | None] = lambda t: MARGINALS.get(t)  # noqa: E731

LEG_A = (LegRef("A", "SER-GAME1", "yes"),)
LEG_B = (LegRef("B", "SER-GAME2", "yes"),)

BANKROLL_2K = 20_000_000  # $2,000 in cc; 1% anchor = 200_000cc = $20


def make_position(
    pid: str,
    legs: tuple[LegRef, ...] = LEG_A,
    *,
    combo_ticker: str | None = None,
    our_side: Side = Side.YES,
    contracts: int = 100,
    entry_price: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=combo_ticker or f"COMBO-{pid}",
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def make_quote(qid: str, legs: tuple[LegRef, ...] = LEG_A) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=qid,
        rfq_id=f"rfq-{qid}",
        combo_ticker=f"COMBO-{qid}",
        collection=None,
        yes_bid_cc=CC(2_000),
        no_bid_cc=CC(2_000),
        contracts=Q(1_000),
        legs=legs,
    )


def empty_book() -> ExposureBook:
    return ExposureBook(CONVENTIONS)


@dataclass(frozen=True)
class Cert:
    """Minimal WaiverCertificate stand-in (structural protocol)."""

    worst_case_cc: int
    certified: bool = True


LOOSE: dict[str, object] = {
    "caps_shadow_mode": False,  # enforced mode: new breaches must be real
    "game_loss_frac": Fraction(99, 100),
    "per_combo_loss_frac": Fraction(99, 100),
    "directional_frac": Fraction(99, 100),
    "slate_loss_frac": Fraction(99, 100),
    "daily_loss_frac": Fraction(99, 100),
    "drawdown_frac": Fraction(99, 100),
    "hard_trip_frac": Fraction(99, 100),
    "portfolio_cvar_frac": Fraction(99, 100),
    "portfolio_det_max_frac": Fraction(99, 100),
    "absolute_notional_multiple": 999,
    "max_gross_notional_dollars": 1e12,
    "max_event_worst_case_loss_dollars": 1e12,
    "max_market_delta_contracts": 1e12,
    "max_event_delta_contracts": 1e12,
    "max_contracts_per_quote": 1e12,
    "max_notional_per_quote_dollars": 1e12,
}


def checker(**overrides: object) -> LimitChecker:
    merged = {**LOOSE, **overrides}
    return LimitChecker(RiskLimits(**merged))  # type: ignore[arg-type]


def reasons(breaches: list[Breach]) -> list[ReasonCode]:
    return [b.reason for b in breaches]


# --- (3d) STRUCTURE bound --------------------------------------------------------


class TestStructureBound:
    def test_lone_whale_candidate_fires_when_armed(self) -> None:
        # The 8/13 shape: a single 271-contract near-coin ticket at ~3.7% of
        # bankroll â€” no accumulation needed, the candidate alone crosses the
        # 1% anchor ($20 here). max_loss = 100ct x 50c = $50 = 500_000cc.
        cand = make_position("w", combo_ticker="KXMVE-WHALE", contracts=10_000)
        breaches = checker(
            structure_loss_frac=Fraction(1, 100), structure_bound_armed=True
        ).check(
            empty_book(), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
        )
        hits = [b for b in breaches if b.reason is ReasonCode.SKIP_STRUCTURE_LOSS_CAP]
        assert len(hits) == 1
        assert "KXMVE-WHALE" in hits[0].detail
        assert hits[0].game is None  # never waivable â‡’ no game attribution

    def test_accumulation_same_structure_fires_other_structure_does_not(self) -> None:
        book = empty_book()
        book.add_position(
            make_position("p1", combo_ticker="KXMVE-S1", contracts=3_000)  # $15
        )
        # candidate adds $10 on the SAME structure: accumulated $25 > $20 anchor
        cand_same = make_position(
            "c1", combo_ticker="KXMVE-S1", contracts=2_000, entry_price=5_000
        )
        armed = checker(
            structure_loss_frac=Fraction(1, 100), structure_bound_armed=True
        )
        hits = [
            b
            for b in armed.check(
                book, MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_2K,
                candidate_positions=[cand_same],
            )
            if b.reason is ReasonCode.SKIP_STRUCTURE_LOSS_CAP
        ]
        assert len(hits) == 1
        # the same $10 candidate on a DIFFERENT structure: $10 < $20 â€” clean
        cand_other = make_position(
            "c2", combo_ticker="KXMVE-S2", contracts=2_000, entry_price=5_000
        )
        assert [
            b
            for b in armed.check(
                book, MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_2K,
                candidate_positions=[cand_other],
            )
            if b.reason is ReasonCode.SKIP_STRUCTURE_LOSS_CAP
        ] == []

    def test_at_threshold_does_not_fire(self) -> None:
        # exactly the anchor (strict >): 40ct x 50c = $20 = 200_000cc
        cand = make_position("t", combo_ticker="KXMVE-T", contracts=4_000)
        breaches = checker(
            structure_loss_frac=Fraction(1, 100), structure_bound_armed=True
        ).check(
            empty_book(), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
        )
        assert ReasonCode.SKIP_STRUCTURE_LOSS_CAP not in reasons(breaches)

    def test_axis_off_is_byte_identical_and_observer_silent(self) -> None:
        cand = make_position("w", combo_ticker="KXMVE-WHALE", contracts=10_000)
        calls: list[tuple] = []
        for flags in (
            {},  # frac None (default) â€” axis not evaluated
            {"structure_loss_frac": Fraction(1, 100)},  # frac set, both flags off
        ):
            breaches = checker(**flags).check(
                empty_book(), MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_2K,
                candidate_positions=[cand],
                structure_bound_observer=lambda *a: calls.append(a),
            )
            assert ReasonCode.SKIP_STRUCTURE_LOSS_CAP not in reasons(breaches)
        assert calls == []

    def test_enabled_only_observes_and_never_refuses(self) -> None:
        cand = make_position("w", combo_ticker="KXMVE-WHALE", contracts=10_000)
        calls: list[tuple] = []
        breaches = checker(
            structure_loss_frac=Fraction(1, 100), structure_bound_enabled=True
        ).check(
            empty_book(), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
            structure_bound_observer=lambda *a: calls.append(a),
        )
        assert ReasonCode.SKIP_STRUCTURE_LOSS_CAP not in reasons(breaches)
        assert len(calls) == 1
        ticker, total, thr, armed = calls[0]
        assert ticker == "KXMVE-WHALE" and total > thr and armed is False

    def test_not_waivable_even_with_valid_game_certificate(self) -> None:
        cand = make_position("w", combo_ticker="KXMVE-WHALE", contracts=10_000)
        breaches = checker(
            structure_loss_frac=Fraction(1, 100), structure_bound_armed=True
        ).check(
            empty_book(), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
            waived_games={"SER-GAME1": Cert(worst_case_cc=0)},
        )
        assert ReasonCode.SKIP_STRUCTURE_LOSS_CAP in reasons(breaches)

    def test_risk_unmodeled_reserved_holding_counts(self) -> None:
        # A reserved holding with no priceable marginals still carries its
        # max_loss into loss_by_combo_cc (the accumulated convention).
        book = empty_book()
        book.add_position(
            make_position(
                "r1",
                legs=(LegRef("ZZZ-UNPRICED", "SER-GAME9", "yes"),),
                combo_ticker="KXMVE-S1",
                contracts=3_000,
            )
        )
        cand = make_position("c", combo_ticker="KXMVE-S1", contracts=2_000)
        breaches = checker(
            structure_loss_frac=Fraction(1, 100), structure_bound_armed=True
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
        )
        assert ReasonCode.SKIP_STRUCTURE_LOSS_CAP in reasons(breaches)


# --- (4b) GAME-DIRECTION accumulated net -----------------------------------------


DIR_FRAC = Fraction(5, 100)  # $100 of 2K â€” test-local isolation value


class TestGameDirectionNet:
    def test_aligned_accumulation_fires_with_game_key(self) -> None:
        # committed 150ct YES on A (game GAME1) + candidate 150ct more, same
        # direction: net-with â‰ˆ 300ct-equivalent > $100 threshold; committed
        # baseline â‰ˆ 150ct â€” the candidate raises the fold â‡’ fires, game-keyed.
        book = empty_book()
        book.add_position(make_position("p1", contracts=15_000))
        cand = make_position("c1", contracts=15_000)
        breaches = checker(
            game_direction_net_frac=DIR_FRAC, game_direction_net_armed=True
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
        )
        hits = [
            b
            for b in breaches
            if b.reason is ReasonCode.SKIP_GAME_DIRECTION_NET_CAP
        ]
        assert len(hits) == 1
        assert hits[0].game == "GAME1"

    def test_sunk_committed_book_never_blocks_other_games(self) -> None:
        # committed book alone over the line on GAME1; candidate on GAME2:
        # candidate-game scoping means GAME1 is never even examined.
        book = empty_book()
        book.add_position(make_position("p1", contracts=40_000))  # way over
        cand = make_position("c1", legs=LEG_B, contracts=1_000)
        breaches = checker(
            game_direction_net_frac=DIR_FRAC, game_direction_net_armed=True
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
        )
        assert ReasonCode.SKIP_GAME_DIRECTION_NET_CAP not in reasons(breaches)

    def test_zero_contribution_candidate_on_hot_game_passes(self) -> None:
        # Candidate whose legs touch the hot game but add ZERO directional
        # magnitude (unpriced marginal â‡’ no directional sensitivity â€” the
        # P0-9 convention): net_with == committed baseline â‡’ marginal
        # judgment admits (sunk book never blocks the future).
        book = empty_book()
        book.add_position(make_position("p1", contracts=40_000))
        cand = make_position(
            "c1", legs=(LegRef("UNPRICED-X", "SER-GAME1", "yes"),), contracts=1_000
        )
        breaches = checker(
            game_direction_net_frac=DIR_FRAC, game_direction_net_armed=True
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
        )
        assert ReasonCode.SKIP_GAME_DIRECTION_NET_CAP not in reasons(breaches)

    def test_valid_waiver_skips_the_direction_net_cap(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", contracts=15_000))
        cand = make_position("c1", contracts=15_000)
        breaches = checker(
            game_direction_net_frac=DIR_FRAC, game_direction_net_armed=True
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
            waived_games={"GAME1": Cert(worst_case_cc=0)},
        )
        assert ReasonCode.SKIP_GAME_DIRECTION_NET_CAP not in reasons(breaches)

    def test_axis_off_is_byte_identical(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", contracts=15_000))
        cand = make_position("c1", contracts=15_000)
        for flags in ({}, {"game_direction_net_frac": DIR_FRAC}):
            breaches = checker(**flags).check(
                book, MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_2K,
                candidate_positions=[cand],
            )
            assert ReasonCode.SKIP_GAME_DIRECTION_NET_CAP not in reasons(breaches)

    def test_enabled_only_observes_and_never_refuses(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", contracts=15_000))
        cand = make_position("c1", contracts=15_000)
        calls: list[tuple] = []
        breaches = checker(
            game_direction_net_frac=DIR_FRAC, game_direction_net_enabled=True
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            candidate_positions=[cand],
            game_direction_observer=lambda *a: calls.append(a),
        )
        assert ReasonCode.SKIP_GAME_DIRECTION_NET_CAP not in reasons(breaches)
        assert len(calls) == 1
        game, net_with, baseline, thr, armed = calls[0]
        assert game == "GAME1" and net_with > thr and net_with > baseline
        assert armed is False


# --- snapshot-level invariants ----------------------------------------------------


class TestDirectionalNetSnapshot:
    def test_resting_quotes_never_enter_the_net_census(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", contracts=10_000))
        book.upsert_quote(make_quote("q1"))
        snap = book.snapshot(MARG, mass_acceptance=True, want_directional_net=True)
        assert snap.directional_net_built is True
        # the mass-acceptance directional bound sees the quote...
        assert snap.directional_by_game_cc.get("GAME1", 0) > snap.directional_net_by_game_cc.get("GAME1", 0)
        # ...the accumulated net census does not.

    def test_not_wanted_means_not_built(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", contracts=10_000))
        snap = book.snapshot(MARG, mass_acceptance=True)
        assert snap.directional_net_built is False
        assert snap.directional_net_by_game_cc == {}

    def test_candidate_monotonicity(self) -> None:
        # E2: adding an extra position never lowers the accumulated net.
        book = empty_book()
        book.add_position(make_position("p1", contracts=10_000))
        base = book.snapshot(
            MARG, mass_acceptance=True, want_directional_net=True
        ).directional_net_by_game_cc.get("GAME1", 0)
        with_extra = book.snapshot(
            MARG,
            mass_acceptance=True,
            want_directional_net=True,
            extra_positions=[make_position("c1", contracts=5_000)],
        ).directional_net_by_game_cc.get("GAME1", 0)
        assert with_extra >= base
