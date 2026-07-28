"""FIX 1 — SLATE AGGREGATION BY PARTITION: soundness + the fail-closed contract.

Operator 2026-07-28, ratifying the repair:

    "We need to fix the slate count it shouldn't be over counting, with 12 games
     in a day we should always be filling the $1500 cap we have."

and the governing principle this whole build answers to:

    "Risk engine = protection not limitation. Those risk caps should be
     protecting us as intended, right now they're limiting us."

THE DEFECT. ``LimitChecker._slate_rollup`` sums ``worst_case_loss_by_game_cc``,
so an AND-BOUND ticket spanning G games contributes its FULL ``max_loss_cc``
**G times** to one slate's number. Measured on the live book (116 open
positions, $1,449.22 of real premium): the reconstructed naive sum-per-game read
$2,366.08 against a once-counted joint worst case of $940.95 — **2.51x**. The
book therefore sat at ~96-98% of the slate wall before any candidate was even
considered, and a candidate touching G games consumed L x G of the remaining
headroom.

THE REPAIR IS THE MEASURE, NOT THE NUMBER. ``slate_loss_frac`` (0.65, ratified)
does not move. What moves is that the slate now binds on the ENUMERATED JOINT
WORST CASE with every loss event counted EXACTLY ONCE, folded through the SAME
Stage-B mutex machinery the per-game axis already uses
(``exposure.partitioned_worst_case_cc`` -> ``_mutex_game_worst_cc``), clamped
into ``[largest single loss, once-counted comonotone sum]``.

WHAT THIS FILE PINS, in the order the brief asked for them:

  1. the enumerated aggregate NEVER UNDERSTATES the true joint worst case —
     including on books where the partition's own enumeration is wrong or
     unavailable (brute-forced against an explicit outcome space);
  2. mutex-offsetting positions stop double-counting;
  3. a genuinely over-limit slate is STILL REFUSED;
  4. ``want_loss_units=False`` CAN NEVER YIELD A PERMISSIVE ANSWER (the latent
     fail-open: an un-built census folds to zero, and zero admits everything);
  5. SHADOW mode is byte-identical to today, on the exact breach detail strings.

It lives in its own module rather than in ``tests/test_admission_fixes.py``
because that file is owned by the concurrent entity-axis workstream; the slate
axis is a separate lever with a separate flag and keeps a separate suite.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable
from datetime import UTC, datetime
from fractions import Fraction

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    LossUnit,
    OpenPosition,
    partitioned_worst_case_cc,
)
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

BANKROLL = 20_000_000                     # $2,000.00
MARG: Callable[[str], float | None] = lambda t: 0.5  # noqa: E731

# Two games on ONE slate, each an explicitly MUTUALLY-EXCLUSIVE result event
# (exactly one of A/B happens) — the shape the Stage-B fold is built for.
G1 = "KXMLBGAME-26JUL281845TORWSH"
G2 = "KXMLBGAME-26JUL281905NYYBOS"
G3 = "KXMLBGAME-26JUL281910LADSFG"
START = datetime(2026, 7, 28, 22, 45, tzinfo=UTC)      # 6:45pm ET -> 2026-07-28


def _leg(event: str, outcome: str) -> LegRef:
    return LegRef(f"{event}-{outcome}", event, "yes")


def _pos(pid: str, legs: tuple[LegRef, ...], loss_cc: int) -> OpenPosition:
    """A long-NO (AND-bound) ticket whose max_loss is exactly ``loss_cc``."""
    assert loss_cc % 100 == 0
    return OpenPosition(
        position_id=pid, combo_ticker=f"COMBO-{pid}", collection=None,
        our_side=Side.NO, contracts=Q(10_000), entry_price_cc=CC(loss_cc // 100),
        legs=legs,
    )


def _me(*events: str) -> Callable[[str], bool | None]:
    known = set(events)

    def is_me(event: str) -> bool | None:
        return True if event in known else False

    return is_me


def _unit(
    legs_by_game: tuple[tuple[str, tuple[LegRef, ...]], ...],
    loss_cc: int,
    *,
    requires_all: bool = True,
    resting: bool = False,
) -> LossUnit:
    return LossUnit(
        legs_by_game=legs_by_game, loss_cc=loss_cc,
        requires_all=requires_all, resting=resting,
    )


def _limits(**over: object) -> RiskLimits:
    """Every axis but the SLATE loosened, so only the cap under test can fire."""
    base: dict[str, object] = dict(
        caps_shadow_mode=False,
        slate_loss_frac=Fraction(65, 100),
        per_combo_loss_frac=Fraction(99, 100),
        game_loss_frac=Fraction(99, 100),
        directional_frac=Fraction(999, 100),
        daily_loss_frac=Fraction(99, 100),
        drawdown_frac=Fraction(99, 100),
        hard_trip_frac=Fraction(99, 100),
        portfolio_det_max_frac=Fraction(999, 100),
        portfolio_cvar_frac=Fraction(999, 100),
        absolute_notional_multiple=999,
        max_market_delta_contracts=1e9,
        max_event_delta_contracts=1e9,
        max_gross_notional_dollars=1e9,
        max_event_worst_case_loss_dollars=1e9,
    )
    base.update(over)
    return RiskLimits(**base)  # type: ignore[arg-type]


def _starts(_ticker: str) -> datetime | None:
    return START


def _breaches(
    book: ExposureBook,
    *,
    armed: bool,
    enabled: bool = False,
    candidates: list[OpenPosition] | None = None,
    **over: object,
) -> list[object]:
    checker = LimitChecker(
        _limits(
            slate_partition_armed=armed,
            slate_partition_enabled=enabled,
            **over,
        )
    )
    return checker.check(
        book, MARG, DailyPnl(),
        candidate_positions=candidates or [],
        risk_bankroll_cc=BANKROLL,
        start_time_provider=_starts,
    )


def _slate_details(breaches: list[object]) -> list[str]:
    return [
        b.detail  # type: ignore[attr-defined]
        for b in breaches
        if b.reason is ReasonCode.SKIP_SLATE_CAP  # type: ignore[attr-defined]
    ]


# =============================================================================
# 1. THE AGGREGATE NEVER UNDERSTATES THE TRUE JOINT WORST CASE
# =============================================================================


class TestNeverUnderstatesTheTrueWorstCase:
    """The one property that matters: for EVERY realizable joint outcome, the
    realized total loss must be <= the number the cap binds on. Everything else
    in this build is a throughput argument; this is the safety argument."""

    def _brute_force_worst(
        self,
        tickets: list[tuple[list[tuple[str, str]], int]],
        me_games: set[str],
        outcomes: dict[str, list[str]],
    ) -> int:
        """The TRUE joint worst case, by enumerating the whole outcome space.

        A ticket is a list of ``(game, outcome)`` requirements plus its premium;
        it loses iff EVERY requirement is satisfied. For a MUTUALLY-EXCLUSIVE
        game exactly ONE outcome realizes; for a non-ME game every outcome may
        realize together (independent binaries). No approximation, no bound —
        the actual maximum over the actual state space.
        """
        games = sorted(outcomes)
        # Per game, the set of REALIZABLE outcome-sets.
        per_game: list[list[frozenset[str]]] = []
        for g in games:
            if g in me_games:
                per_game.append([frozenset((o,)) for o in outcomes[g]])
            else:
                per_game.append(
                    [
                        frozenset(sub)
                        for r in range(len(outcomes[g]) + 1)
                        for sub in itertools.combinations(outcomes[g], r)
                    ]
                )
        worst = 0
        for combo in itertools.product(*per_game):
            realized = dict(zip(games, combo, strict=True))
            total = 0
            for reqs, loss in tickets:
                if all(o in realized.get(g, frozenset()) for g, o in reqs):
                    total += loss
            worst = max(worst, total)
        return worst

    def test_honest_enumeration_dominates_the_true_worst_case(self) -> None:
        """REQUIRED. Randomised books, brute-forced. The partition's answer is
        never below the true maximum realizable loss."""
        rng = random.Random(20260728)
        games = [G1, G2, G3]
        outcomes = {g: ["A", "B"] for g in games}
        for trial in range(120):
            me_games = {g for g in games if rng.random() < 0.6}
            tickets: list[tuple[list[tuple[str, str]], int]] = []
            units: list[LossUnit] = []
            for _ in range(rng.randint(1, 6)):
                touched = rng.sample(games, rng.randint(1, 3))
                reqs = [(g, rng.choice(outcomes[g])) for g in touched]
                loss = rng.randrange(100, 900_000, 100)
                tickets.append((reqs, loss))
                units.append(
                    _unit(
                        tuple(
                            (g, (_leg(g, o),)) for g, o in reqs
                        ),
                        loss,
                    )
                )
            truth = self._brute_force_worst(tickets, me_games, outcomes)
            bound = partitioned_worst_case_cc(units, _me(*me_games))
            assert bound >= truth, (
                f"trial {trial}: partition {bound} < true worst case {truth}"
            )

    def test_a_multi_game_ticket_is_never_netted_away(self) -> None:
        """The partition's own enumeration cannot decide a multi-game ticket's
        exclusivity against the rest, so it goes to the COMONOTONE residual at
        FULL loss — the larger treatment — never into a game bucket where a
        max-over-branches fold could hide it."""
        spanning = _unit(((G1, (_leg(G1, "A"),)), (G2, (_leg(G2, "A"),))), 500_000)
        same_branch = _unit(((G1, (_leg(G1, "A"),)),), 300_000)
        got = partitioned_worst_case_cc([spanning, same_branch], _me(G1, G2))
        assert got == 800_000

    def test_unknown_me_metadata_folds_comonotone(self) -> None:
        """UNKNOWN is never the convenient default (quiet-failure defense #2):
        a game whose ME fact is None gets the comonotone (larger) treatment."""
        a = _unit(((G1, (_leg(G1, "A"),)),), 400_000)
        b = _unit(((G1, (_leg(G1, "B"),)),), 300_000)

        def unknown(_e: str) -> bool | None:
            return None

        assert partitioned_worst_case_cc([a, b], unknown) == 700_000
        assert partitioned_worst_case_cc([a, b], None) == 700_000
        # …and with the ME fact actually KNOWN, the same two net.
        assert partitioned_worst_case_cc([a, b], _me(G1)) == 400_000

    def test_an_ungamed_loss_event_is_pooled_never_dropped(self) -> None:
        """A ticket with no identifiable game is invisible to today's roll-up
        entirely. Here it lands in the residual at full loss."""
        ungamed = _unit((), 250_000)
        gamed = _unit(((G1, (_leg(G1, "A"),)),), 400_000)
        assert partitioned_worst_case_cc([ungamed, gamed], _me(G1)) == 650_000

    def test_an_exception_in_the_me_lookup_folds_comonotone(self) -> None:
        def boom(_e: str) -> bool | None:
            raise RuntimeError("metadata cache is gone")

        units = [
            _unit(((G1, (_leg(G1, "A"),)),), 400_000),
            _unit(((G1, (_leg(G1, "B"),)),), 300_000),
        ]
        assert partitioned_worst_case_cc(units, boom) == 700_000

    def test_the_clamp_holds_on_randomised_books(self) -> None:
        """Invariants 3 + 4: always within [largest single loss, once-counted
        sum], and adding a unit never LOWERS the answer (E2 dominance)."""
        rng = random.Random(7281)
        for _ in range(200):
            units = [
                _unit(
                    tuple(
                        (g, (_leg(g, rng.choice("AB")),))
                        for g in rng.sample([G1, G2, G3], rng.randint(1, 3))
                    ),
                    rng.randrange(100, 500_000, 100),
                )
                for _ in range(rng.randint(1, 7))
            ]
            is_me = _me(*rng.sample([G1, G2, G3], rng.randint(0, 3)))
            got = partitioned_worst_case_cc(units, is_me)
            assert max(u.loss_cc for u in units) <= got
            assert got <= sum(u.loss_cc for u in units)
            extra = _unit(((G2, (_leg(G2, "A"),)),), 123_400)
            assert partitioned_worst_case_cc([*units, extra], is_me) >= got

    def test_a_game_certificate_can_only_tighten(self) -> None:
        units = [
            _unit(((G1, (_leg(G1, "A"),)),), 400_000),
            _unit(((G1, (_leg(G1, "A"),)),), 300_000),
        ]
        base = partitioned_worst_case_cc(units, _me(G1))
        assert base == 700_000
        assert partitioned_worst_case_cc(units, _me(G1), {G1: 500_000}) == 500_000
        # A LOOSER certificate never raises the fold.
        assert partitioned_worst_case_cc(units, _me(G1), {G1: 9_000_000}) == base


# =============================================================================
# 2. MUTEX-OFFSETTING POSITIONS STOP DOUBLE-COUNTING
# =============================================================================


class TestDoubleCountingStops:
    def test_the_same_parlay_is_counted_once_per_slate_not_once_per_game(
        self,
    ) -> None:
        """REQUIRED, and the whole defect in one assertion. One $500 ticket
        across three games: the naive roll-up charges the slate $1,500."""
        book = ExposureBook(CONVENTIONS)
        book.add_position(
            _pos(
                "p1",
                (_leg(G1, "A"), _leg(G2, "A"), _leg(G3, "A")),
                500_000,
            )
        )
        snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        naive = sum(snap.worst_case_loss_by_game_cc.values())
        assert naive == 1_500_000                       # 3 games x $50
        checker = LimitChecker(_limits(slate_partition_armed=True))
        got = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates={"2026-07-28"},
        )
        assert got == {"2026-07-28": 500_000}           # the real premium

    def test_within_game_mutually_exclusive_arms_net(self) -> None:
        """Two tickets short OPPOSITE arms of one exclusive result cannot both
        lose. The bucket takes the max, not the sum."""
        book = ExposureBook(CONVENTIONS, is_me_event=_me(G1))
        book.add_position(_pos("a", (_leg(G1, "A"),), 400_000))
        book.add_position(_pos("b", (_leg(G1, "B"),), 300_000))
        snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        checker = LimitChecker(_limits(slate_partition_armed=True))
        got = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates={"2026-07-28"},
        )
        assert got == {"2026-07-28": 400_000}

    def test_the_live_shape_naive_vs_partitioned(self) -> None:
        """The measured live shape in miniature: a book that is 55% multi-game
        reads ~2x its real premium under the roll-up."""
        book = ExposureBook(CONVENTIONS)
        for i in range(5):
            book.add_position(
                _pos(f"m{i}", (_leg(G1, "A"), _leg(G2, "A")), 100_000)
            )
        for i in range(4):
            book.add_position(_pos(f"s{i}", (_leg(G3, "A"),), 100_000))
        snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        naive = sum(snap.worst_case_loss_by_game_cc.values())
        checker = LimitChecker(_limits(slate_partition_armed=True))
        part = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates={"2026-07-28"},
        )["2026-07-28"]
        assert naive == 1_400_000        # 5x$10 on G1 + 5x$10 on G2 + 4x$10
        assert part == 900_000           # 9 tickets x $10, each counted once
        assert naive / part > 1.5


# =============================================================================
# 3. A GENUINELY OVER-LIMIT SLATE IS STILL REFUSED
# =============================================================================


class TestGenuineBreachesStillRefuse:
    def test_over_the_wall_on_the_honest_measure_still_breaches(self) -> None:
        """REQUIRED. 65% of $2,000 = $1,300. A book of 15 SINGLE-game tickets at
        $100 each carries $1,500 of real, once-counted premium — no double
        counting to remove — so the armed cap refuses exactly as the naive one
        does, and says so with the corrected number."""
        book = ExposureBook(CONVENTIONS)
        for i in range(15):
            book.add_position(_pos(f"g{i}", (_leg(G1, f"O{i}"),), 1_000_000))
        armed = _breaches(book, armed=True)
        assert ReasonCode.SKIP_SLATE_CAP in [
            b.reason for b in armed  # type: ignore[attr-defined]
        ]
        detail = _slate_details(armed)[0]
        assert "15000000cc" in detail
        assert "once-counted joint worst case" in detail

    def test_the_armed_number_is_never_above_the_naive_one(self) -> None:
        rng = random.Random(99)
        for _ in range(40):
            book = ExposureBook(CONVENTIONS)
            for i in range(rng.randint(1, 8)):
                games = rng.sample([G1, G2, G3], rng.randint(1, 3))
                book.add_position(
                    _pos(
                        f"p{i}",
                        tuple(_leg(g, rng.choice("AB")) for g in games),
                        rng.randrange(100, 400_000, 100),
                    )
                )
            snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
            checker = LimitChecker(_limits(slate_partition_armed=True))
            naive = checker._slate_rollup(book, snap, [], _starts)
            part = checker._slate_partition(
                book, snap, [], _starts,
                limits=checker.limits, only_slates=set(naive),
                naive_by_slate=naive,
            )
            for slate, value in part.items():
                assert value <= naive[slate]

    def test_a_candidate_that_genuinely_breaches_is_refused_armed(self) -> None:
        book = ExposureBook(CONVENTIONS)
        for i in range(12):
            book.add_position(_pos(f"g{i}", (_leg(G1, f"O{i}"),), 1_000_000))
        cand = _pos("cand", (_leg(G2, "A"),), 2_000_000)
        armed = _breaches(book, armed=True, candidates=[cand])
        assert ReasonCode.SKIP_SLATE_CAP in [
            b.reason for b in armed  # type: ignore[attr-defined]
        ]


# =============================================================================
# 4. THE LATENT FAIL-OPEN: want_loss_units=False MUST NEVER ADMIT
# =============================================================================


class TestUnbuiltCensusCanNeverAdmit:
    """THE HOLE THIS CLOSES. ``_slate_partition`` builds its answer from
    ``snapshot.loss_units``; an EMPTY census folds to ZERO for every slate, and
    zero is the PERMISSIVE answer on a loss cap. A snapshot built with
    ``want_loss_units=False`` would therefore make EVERY slate breach vanish.

    Before this build the only thing preventing it was that one caller happened
    to derive ``want_loss_units`` from the same two booleans that arm the
    partition — no assertion, and the permissive answer as the default. That is
    exactly the species hard rule 6 and quiet-failure defense #2 exist for."""

    def _over_limit_book(self) -> ExposureBook:
        book = ExposureBook(CONVENTIONS)
        for i in range(15):
            book.add_position(_pos(f"g{i}", (_leg(G1, f"O{i}"),), 1_000_000))
        return book

    def test_the_poc_the_fix_kills(self) -> None:
        """The exploit, executed: an over-limit book, the partition ARMED, and a
        snapshot whose census was never taken. Before the fix this returned
        ``{'2026-07-28': 0}`` and the breach disappeared."""
        book = self._over_limit_book()
        snap = book.snapshot(MARG, mass_acceptance=True)     # want_loss_units OFF
        assert snap.loss_units == ()
        assert snap.loss_units_built is False
        checker = LimitChecker(_limits(slate_partition_armed=True))
        got = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates={"2026-07-28"},
        )
        assert got == {}, "an un-built census must never produce a number"

    def test_an_unbuilt_census_leaves_the_naive_number_enforcing(self) -> None:
        """End to end: with the corrected number unavailable, the slate is still
        refused — on the naive number, i.e. the LARGER one."""
        book = self._over_limit_book()
        snap = book.snapshot(MARG, mass_acceptance=True)
        checker = LimitChecker(_limits(slate_partition_armed=True))
        naive = checker._slate_rollup(book, snap, [], _starts)
        part = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates=set(naive), naive_by_slate=naive,
        )
        for slate, value in naive.items():
            assert part.get(slate, value) == value

    def test_an_empty_book_is_distinguishable_from_an_unbuilt_census(
        self,
    ) -> None:
        """The two states that both present as ``loss_units == ()``. Only the
        flag tells them apart, which is why the flag exists."""
        empty = ExposureBook(CONVENTIONS)
        built = empty.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        assert built.loss_units == () and built.loss_units_built is True
        unbuilt = empty.snapshot(MARG, mass_acceptance=True)
        assert unbuilt.loss_units == () and unbuilt.loss_units_built is False

    def test_a_slate_the_census_cannot_see_is_omitted(self) -> None:
        """FAIL-OPEN #2. The census was taken, but it sees nothing in a slate the
        roll-up says is over the wall — the two views disagree about the book, so
        the naive number keeps enforcing."""
        book = self._over_limit_book()
        snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        checker = LimitChecker(_limits(slate_partition_armed=True))
        got = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates={"1999-01-01"},
        )
        assert got == {}

    def test_the_corrected_number_can_never_exceed_the_naive_one(self) -> None:
        """FAIL-OPEN #3. A partition ABOVE the roll-up is impossible by
        construction, so if it ever happens the census is not the book the
        roll-up measured — clamp to the naive term rather than raise the cap."""
        book = self._over_limit_book()
        snap = book.snapshot(MARG, mass_acceptance=True, want_loss_units=True)
        checker = LimitChecker(_limits(slate_partition_armed=True))
        got = checker._slate_partition(
            book, snap, [], _starts,
            limits=checker.limits, only_slates={"2026-07-28"},
            naive_by_slate={"2026-07-28": 7},
        )
        assert got == {"2026-07-28": 7}

    def test_the_readout_reports_the_enforced_number_not_zero(self) -> None:
        """The shadow read-out is what the operator ARMS FROM, so it fails
        closed too: a slate the partition refused to correct must not print a
        would-admit line."""
        book = self._over_limit_book()
        seen: list[tuple[str, int, int, int]] = []
        checker = LimitChecker(
            _limits(slate_partition_enabled=True, slate_partition_armed=False)
        )
        # Force the un-built state the read-out must survive.
        real = book.snapshot

        def spy(*a: object, **kw: object) -> object:
            kw["want_loss_units"] = False
            return real(*a, **kw)  # type: ignore[arg-type]

        book.snapshot = spy  # type: ignore[method-assign,assignment]
        checker.check(
            book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL,
            start_time_provider=_starts,
            slate_partition_observer=lambda s, n, p, t: seen.append((s, n, p, t)),
        )
        assert seen, "the read-out must still fire on a would-be refusal"
        for _slate, naive, partitioned, _thr in seen:
            assert partitioned == naive, "0 would have read as would_admit=True"


# =============================================================================
# 5. SHADOW IS BYTE-IDENTICAL TO TODAY
# =============================================================================


class TestShadowIsByteIdentical:
    def _book(self) -> ExposureBook:
        book = ExposureBook(CONVENTIONS)
        for i in range(6):
            book.add_position(
                _pos(f"m{i}", (_leg(G1, "A"), _leg(G2, "A"), _leg(G3, "A")),
                     1_000_000)
            )
        return book

    def test_disarmed_and_enabled_produce_identical_breaches(self) -> None:
        """REQUIRED. ``slate_partition_enabled`` computes the corrected number
        and hands it to the read-out; it must NOT touch the breach list, detail
        strings included."""
        off = _breaches(self._book(), armed=False, enabled=False)
        shadow = _breaches(self._book(), armed=False, enabled=True)
        assert [b.reason for b in off] == [  # type: ignore[attr-defined]
            b.reason for b in shadow  # type: ignore[attr-defined]
        ]
        assert [b.detail for b in off] == [  # type: ignore[attr-defined]
            b.detail for b in shadow  # type: ignore[attr-defined]
        ]

    def test_an_observer_can_never_change_a_decision(self) -> None:
        book = self._book()
        quiet = _breaches(book, armed=False, enabled=True)
        checker = LimitChecker(
            _limits(slate_partition_enabled=True, slate_partition_armed=False)
        )
        noisy = checker.check(
            book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL,
            start_time_provider=_starts,
            slate_partition_observer=lambda *_a: (_ for _ in ()).throw(
                RuntimeError("telemetry exploded")
            ),
        )
        assert [b.detail for b in quiet] == [  # type: ignore[attr-defined]
            b.detail for b in noisy  # type: ignore[attr-defined]
        ]

    def test_arming_changes_exactly_the_slate_axis(self) -> None:
        """The corrected measure releases the slate refusal on a book whose whole
        breach was the multi-game double count — and touches nothing else."""
        book = self._book()
        naive_reasons = [
            b.reason for b in _breaches(book, armed=False)  # type: ignore[attr-defined]
        ]
        armed_reasons = [
            b.reason for b in _breaches(book, armed=True)  # type: ignore[attr-defined]
        ]
        assert ReasonCode.SKIP_SLATE_CAP in naive_reasons
        assert ReasonCode.SKIP_SLATE_CAP not in armed_reasons
        assert [r for r in naive_reasons if r is not ReasonCode.SKIP_SLATE_CAP] == [
            r for r in armed_reasons if r is not ReasonCode.SKIP_SLATE_CAP
        ]
