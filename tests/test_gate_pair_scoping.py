"""B1 — the candidate gate resolves EXACTLY the rho pairs the MC consumes.

THE DEFECT (2026-07-27). ``_build_candidate_gate_inputs`` resolved a band for
EVERY unordered pair of every leg ticker in the merged book, on the theory —
written into the code as a comment — that "resolving all is a harmless
superset". ``build_book_model`` iterates only ``game_members``, i.e. SAME-GAME
pairs, so every cross-game pair was computed and thrown away. Measured on the
live 100-position book: 22,155 all-pairs against 880 same-game = 96.03% waste,
4,150 ms against 167 ms. At 3x the book: 200,028 against 2,637 (98.68%),
38,273 ms against 488 ms. That waste is what burnt the exchange's 3.0 s confirm
window, and 23 of 24 lost auctions in one live session never reached a single
Monte Carlo sample.

THE OTHER DIRECTION IS WORSE, AND SILENT. A same-game pair MISSING from the
dict makes the worker's ``_DictWithinGameRho`` return None, ``build_book_model``
substitute ``flat_band``, and the book model carry LESS correlation than the
pricer measured — it UNDERSTATES the joint tail and therefore UNDERSTATES RISK,
with nothing in the verdict to say so.

So the pair set is not re-derived at the call site. ``within_game_pair_tickers``
and ``build_book_model`` share three primitives (``select_modeled_positions`` /
``build_leg_universe`` / ``within_game_index_members``) and there is no second
implementation to drift. These tests prove the equality holds — including on
randomly generated books with the shapes that break naive re-implementations:
ungamed legs, unpriceable legs, non-risk-modeled positions, tickers repeated
across positions under DIFFERENT event tickers, period/derived markets that key
onto the full game, and pricing event aliases.
"""

from __future__ import annotations

import random

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import (
    build_book_model,
    within_game_pair_tickers,
)

# --------------------------------------------------------------------------- #
# The ORACLE: what pairs does build_book_model ACTUALLY ask for?               #
# --------------------------------------------------------------------------- #


class _RecordingRho:
    """A ``WithinGameRhoProvider`` that records every pair it is asked about.

    This is the ground truth for "the set ``build_book_model`` consumes" — read
    off the real function, never predicted from a second implementation."""

    def __init__(self, band: tuple[float, float, float] | None = (0.1, 0.2, 0.3)):
        self.asked: list[tuple[str, str]] = []
        self._band = band

    def __call__(self, a: str, b: str) -> tuple[float, float, float] | None:
        self.asked.append((a, b))
        return self._band

    @property
    def asked_set(self) -> set[frozenset[str]]:
        return {frozenset(p) for p in self.asked}


def _pos(
    pid: str,
    legs: list[tuple[str, str | None, str]],
    *,
    risk_modeled: bool = True,
    contracts: int = 100,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.YES,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(3_000),
        legs=tuple(
            LegRef(market_ticker=m, event_ticker=e, side=s) for m, e, s in legs
        ),
        risk_modeled=risk_modeled,
    )


def _assert_identical(
    positions: list[OpenPosition], priced: set[str], label: str = ""
) -> int:
    """THE INVARIANT, both directions, on one book. Returns the pair count."""
    rho = _RecordingRho()
    build_book_model(
        positions,
        marginals=lambda t: 0.45 if t in priced else None,
        within_game_rho=rho,
    )
    scoped = within_game_pair_tickers(positions, lambda t: t in priced)
    scoped_set = {frozenset(p) for p in scoped}

    missing = rho.asked_set - scoped_set
    extra = scoped_set - rho.asked_set
    assert not missing, (
        f"{label}: {len(missing)} same-game pair(s) the MC consumes are MISSING "
        f"from the scoped set — each one silently drops to flat_band and "
        f"UNDERSTATES correlation: {sorted(map(sorted, missing))[:5]}"
    )
    assert not extra, (
        f"{label}: {len(extra)} pair(s) resolved that the MC never asks for — "
        f"dead work in the confirm window: {sorted(map(sorted, extra))[:5]}"
    )
    # No duplicates either: the scoped list is walked once per pair, so a
    # duplicate would be a second provider call inside the confirm window.
    assert len(scoped) == len(scoped_set), f"{label}: duplicate pairs emitted"
    # And the ORDER matches build_book_model's own nested-loop order.
    assert [frozenset(p) for p in scoped] == [
        frozenset(p) for p in rho.asked
    ], f"{label}: pair ORDER diverged from build_book_model's"
    return len(scoped)


# --------------------------------------------------------------------------- #
# 1. THE HEADLINE: cross-game pairs are gone, same-game pairs are all there.   #
# --------------------------------------------------------------------------- #


def test_cross_game_pairs_are_not_resolved_and_same_game_pairs_all_are() -> None:
    positions = [
        _pos(
            "p1",
            [
                ("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("KXWCTOTAL-26JUL05MEXENG-T2", "KXWCTOTAL-26JUL05MEXENG", "yes"),
            ],
        ),
        _pos(
            "p2",
            [
                ("KXMLBGAME-26JUL05ATLNYM-ATL", "KXMLBGAME-26JUL05ATLNYM", "yes"),
                ("KXMLBTOTAL-26JUL05ATLNYM-T8", "KXMLBTOTAL-26JUL05ATLNYM", "no"),
            ],
        ),
    ]
    priced = {leg.market_ticker for p in positions for leg in p.legs}
    n = _assert_identical(positions, priced, "two disjoint games")
    # 4 tickers ⇒ 6 all-pairs; only the 2 SAME-GAME pairs survive.
    assert n == 2
    all_pairs = len(priced) * (len(priced) - 1) // 2
    assert all_pairs == 6


def test_a_dropped_same_game_pair_would_understate_correlation() -> None:
    """The failure this guards is SILENT, so pin its consequence explicitly: a
    pair the caller omits comes back as ``flat_band``, i.e. LESS correlation than
    the pricer measured, and the tail matrix is materially different."""
    positions = [
        _pos(
            "p1",
            [
                ("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("KXWCTOTAL-26JUL05MEXENG-T2", "KXWCTOTAL-26JUL05MEXENG", "yes"),
            ],
        ),
    ]
    priced = {leg.market_ticker for p in positions for leg in p.legs}
    pairs = within_game_pair_tickers(positions, lambda t: t in priced)
    assert len(pairs) == 1

    strong = {frozenset(pairs[0]): (0.80, 0.85, 0.90)}
    complete = build_book_model(
        positions,
        marginals=lambda t: 0.45,
        within_game_rho=lambda a, b: strong.get(frozenset((a, b))),
    )
    # The SAME book with that one pair omitted from the caller's dict.
    dropped = build_book_model(
        positions,
        marginals=lambda t: 0.45,
        within_game_rho=lambda a, b: None,
    )
    assert complete.corr_tail_stress_high[0][1] > 0.8
    assert dropped.corr_tail_stress_high[0][1] < 0.5
    assert (
        dropped.corr_tail_stress_high[0][1] < complete.corr_tail_stress_high[0][1]
    ), "a dropped pair must be shown to UNDERSTATE, not overstate, correlation"


# --------------------------------------------------------------------------- #
# 2. THE SHAPES THAT BREAK A NAIVE RE-IMPLEMENTATION.                          #
# --------------------------------------------------------------------------- #


def test_unpriceable_leg_drops_its_whole_position_from_both_sets() -> None:
    positions = [
        _pos(
            "priced",
            [
                ("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("KXWCTOTAL-26JUL05MEXENG-T2", "KXWCTOTAL-26JUL05MEXENG", "yes"),
            ],
        ),
        _pos(
            "unpriced",
            [
                ("KXWCBTTS-26JUL05MEXENG-Y", "KXWCBTTS-26JUL05MEXENG", "yes"),
                ("GONE", "KXWCGAME-26JUL05MEXENG", "yes"),
            ],
        ),
    ]
    priced = {
        "KXWCGAME-26JUL05MEXENG-MEX",
        "KXWCTOTAL-26JUL05MEXENG-T2",
        "KXWCBTTS-26JUL05MEXENG-Y",
    }
    # The BTTS leg shares the game, but its position carries an unpriceable leg,
    # so the WHOLE position is reserved rather than sampled — and neither its
    # legs nor any pair involving them may be resolved.
    assert _assert_identical(positions, priced, "unpriceable leg") == 1


def test_non_risk_modeled_position_is_excluded_from_both_sets() -> None:
    positions = [
        _pos(
            "modeled",
            [
                ("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("KXWCTOTAL-26JUL05MEXENG-T2", "KXWCTOTAL-26JUL05MEXENG", "yes"),
            ],
        ),
        _pos(
            "gated",
            [("KXWCBTTS-26JUL05MEXENG-Y", "KXWCBTTS-26JUL05MEXENG", "yes")],
            risk_modeled=False,
        ),
    ]
    priced = {leg.market_ticker for p in positions for leg in p.legs}
    assert _assert_identical(positions, priced, "gated holding") == 1


def test_ungamed_leg_never_pairs_with_anything() -> None:
    positions = [
        _pos(
            "p1",
            [
                ("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("NOEVENT", None, "yes"),
            ],
        ),
    ]
    priced = {"KXWCGAME-26JUL05MEXENG-MEX", "NOEVENT"}
    assert _assert_identical(positions, priced, "ungamed leg") == 0


def test_ticker_repeated_under_two_event_tickers_uses_first_occurrence() -> None:
    """A ticker seen twice with DIFFERENT events is placed by its FIRST
    occurrence — the one property a re-implementation that walks a SET of
    tickers (as the old all-pairs code did) cannot reproduce."""
    positions = [
        _pos(
            "first",
            [
                ("SHARED", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("KXWCTOTAL-26JUL05MEXENG-T2", "KXWCTOTAL-26JUL05MEXENG", "yes"),
            ],
        ),
        _pos(
            "second",
            [
                ("SHARED", "KXMLBGAME-26JUL05ATLNYM", "yes"),
                ("KXMLBTOTAL-26JUL05ATLNYM-T8", "KXMLBTOTAL-26JUL05ATLNYM", "no"),
            ],
        ),
    ]
    priced = {
        "SHARED",
        "KXWCTOTAL-26JUL05MEXENG-T2",
        "KXMLBTOTAL-26JUL05ATLNYM-T8",
    }
    # SHARED belongs to the WC game (first occurrence), so it pairs with the WC
    # total and NOT with the MLB total, which is then alone in its game.
    pairs = within_game_pair_tickers(positions, lambda t: t in priced)
    assert {frozenset(p) for p in pairs} == {
        frozenset(("SHARED", "KXWCTOTAL-26JUL05MEXENG-T2"))
    }
    _assert_identical(positions, priced, "ticker under two events")


def test_period_market_rejoins_its_full_game_block() -> None:
    """A first-half series keys on the GAME code and must land in the same block
    as its full-time siblings (``pricing.grouping.game_key``'s documented
    behaviour). Re-deriving the grouping by SERIES would silently split it."""
    positions = [
        _pos(
            "p1",
            [
                ("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
                ("KXWC1HTOTAL-26JUL05MEXENG-T1", "KXWC1HTOTAL-26JUL05MEXENG", "yes"),
            ],
        ),
    ]
    priced = {leg.market_ticker for p in positions for leg in p.legs}
    assert _assert_identical(positions, priced, "1H x FT") == 1


def test_pricing_event_alias_moves_both_sets_together() -> None:
    """An aliased champion event joins the final's game EVERYWHERE at once —
    because both sets call the same ``game_key``, which is where aliases
    resolve."""
    from combomaker.pricing.legtypes import set_pricing_aliases

    # Aliases are MARKET->MARKET; the event alias is derived from them at
    # install time (legtypes._event_of), which is precisely why both sets must
    # go through ``game_key`` rather than parsing an event ticker themselves.
    alias = {"KXMENWORLDCUP-26-ESP": "KXWCADVANCE-26JUL19ESPARG-ESP"}
    positions = [
        _pos(
            "p1",
            [
                ("KXMENWORLDCUP-26-ESP", "KXMENWORLDCUP-26", "yes"),
                ("KXWCGAME-26JUL19ESPARG-ESP", "KXWCGAME-26JUL19ESPARG", "yes"),
            ],
        ),
    ]
    priced = {leg.market_ticker for p in positions for leg in p.legs}
    try:
        set_pricing_aliases({})
        assert _assert_identical(positions, priced, "unaliased") == 0
        set_pricing_aliases(alias)
        assert _assert_identical(positions, priced, "aliased") == 1
    finally:
        set_pricing_aliases({})


# --------------------------------------------------------------------------- #
# 3. THE PROPERTY TEST: divergence is impossible on RANDOM books.              #
# --------------------------------------------------------------------------- #


_GAMES = [
    "KXWCGAME-26JUL05MEXENG",
    "KXWCTOTAL-26JUL05MEXENG",
    "KXWC1HTOTAL-26JUL05MEXENG",
    "KXMLBGAME-26JUL05ATLNYM",
    "KXMLBTOTAL-26JUL05ATLNYM",
    "KXMLBKS-26JUL05ATLNYM",
    "KXNFLGAME-26SEP05KCBUF",
]


def test_property_scoped_set_equals_consumed_set_on_generated_books() -> None:
    """1,000 randomly shaped books. Every one must satisfy the invariant in BOTH
    directions — this is the "divergence is impossible" claim, tested rather than
    assumed, on top of the by-construction sharing."""
    rng = random.Random(20260727)
    tickers = [f"M{i}" for i in range(24)]
    total_scoped = 0
    total_all_pairs = 0
    saw_pairs = 0
    for case in range(1_000):
        n_pos = rng.randint(0, 8)
        positions: list[OpenPosition] = []
        for pi in range(n_pos):
            n_legs = rng.randint(1, 4)
            legs = []
            for _ in range(n_legs):
                m = rng.choice(tickers)
                # ~10% of legs are UNGAMED (no event ticker at all).
                e = None if rng.random() < 0.10 else rng.choice(_GAMES)
                legs.append((m, e, rng.choice(["yes", "no"])))
            positions.append(
                _pos(
                    f"p{pi}",
                    legs,
                    # ~15% of holdings are gated (not risk-modeled).
                    risk_modeled=rng.random() > 0.15,
                )
            )
        # ~15% of tickers are UNPRICEABLE.
        priced = {t for t in tickers if rng.random() > 0.15}
        n = _assert_identical(positions, priced, f"random case {case}")
        total_scoped += n
        if n:
            saw_pairs += 1
        distinct = {
            leg.market_ticker
            for p in positions
            if p.risk_modeled and all(leg.market_ticker in priced for leg in p.legs)
            for leg in p.legs
        }
        total_all_pairs += len(distinct) * (len(distinct) - 1) // 2

    # The generator must actually have produced pairs, or the property is vacuous.
    assert saw_pairs > 500, f"only {saw_pairs}/1000 books had any same-game pair"
    # And it must have produced real WASTE to eliminate, or the fix is untested.
    assert total_all_pairs > total_scoped, "no cross-game pairs in the corpus"


def test_property_holds_when_every_leg_shares_one_game() -> None:
    """The degenerate maximum: one game, N legs ⇒ N(N-1)/2 pairs and the scoped
    set must equal ALL pairs (the fix must never DROP work that is real)."""
    rng = random.Random(7)
    for n_legs in (2, 5, 12):
        legs = [(f"T{i}", "KXWCGAME-26JUL05MEXENG", "yes") for i in range(n_legs)]
        rng.shuffle(legs)
        positions = [_pos("solo", legs)]
        priced = {m for m, _e, _s in legs}
        assert _assert_identical(positions, priced, f"one game n={n_legs}") == (
            n_legs * (n_legs - 1) // 2
        )


# --------------------------------------------------------------------------- #
# 4. THE SEAM ITSELF: the LIFECYCLE's dict == what the WORKER's model consumes. #
# --------------------------------------------------------------------------- #


async def test_lifecycle_gate_dict_equals_the_set_the_worker_model_consumes(
    tmp_path,
) -> None:
    """End to end through the real ``_build_candidate_gate_inputs``.

    The property test above pins ``within_game_pair_tickers`` against
    ``build_book_model``. This pins the CALL SITE: that the lifecycle feeds it
    the same ordered positions (committed, reservations, candidate — the order
    ``evaluate_candidate_book_risk`` itself assembles) and the same priced
    predicate the worker's ``_DictMarginals`` implements. Both halves are needed:
    a correct helper called with the wrong arguments understates risk exactly as
    silently as a wrong helper."""
    from combomaker.ops.pricing_pool import _DictMarginals
    from tests.test_candidate_gate_atomic import ScriptedPool, _make, _verdict
    from tests.test_confirm_window_budget import _same_game_holding, _warm_latency
    from tests.test_lifecycle import accepted_msg, rfq

    pool = ScriptedPool([_verdict(confirm=True)])
    lifecycle, _sender, exposure, _reservation = await _make(
        tmp_path, pool=pool, db="pair_seam.sqlite3"
    )
    _warm_latency(lifecycle)
    # A book with BOTH a same-game pair and cross-game legs, so the two sets can
    # actually differ if anything drifts.
    exposure.add_position(_same_game_holding("sg"))
    recorder = _RecordingRho()
    lifecycle._within_game_rho = recorder  # noqa: SLF001

    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))

    assert len(pool.calls) == 1, "the candidate MC did not run"
    inputs = pool.calls[0]
    resolved = set(inputs.within_game_rho_pairs)
    assert resolved, "the gate resolved NO rho pairs on a book that has one"

    # Now ask the REAL model, with the REAL dict-backed marginal provider, what
    # it consumes — in the worker's own position order.
    consumed = _RecordingRho()
    build_book_model(
        [*inputs.committed, *inputs.reservations, inputs.candidate],
        marginals=_DictMarginals(inputs.marginals),
        within_game_rho=consumed,
    )
    assert consumed.asked_set == resolved, (
        f"gate dict != model demand.  missing (understates risk): "
        f"{sorted(map(sorted, consumed.asked_set - resolved))}   "
        f"extra (dead work in the confirm window): "
        f"{sorted(map(sorted, resolved - consumed.asked_set))}"
    )

    # And the waste is genuinely gone: the SAME rig without the same-game
    # holding is a purely CROSS-EVENT book (M1 in E1, M2 in E2). The old
    # all-pairs build resolved a band for that pair; the scoped build resolves
    # NOTHING, because the model never asks.
    pool2 = ScriptedPool([_verdict(confirm=True)])
    lifecycle2, _s2, _e2, _r2 = await _make(
        tmp_path, pool=pool2, db="pair_seam_cross.sqlite3"
    )
    _warm_latency(lifecycle2)
    cross_recorder = _RecordingRho()
    lifecycle2._within_game_rho = cross_recorder  # noqa: SLF001
    await lifecycle2.handle_rfq(rfq())
    await lifecycle2.on_quote_accepted(accepted_msg("q1", "yes"))
    assert len(pool2.calls) == 1
    assert len(pool2.calls[0].marginals) == 2  # one all-pairs pair existed...
    assert cross_recorder.asked == []  # ...and it was never resolved
    assert pool2.calls[0].within_game_rho_pairs == {}
