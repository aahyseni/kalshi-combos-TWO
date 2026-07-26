"""RESIDUAL-RESERVE RECONCILE — the two fail-OPEN regressions of the B1 rebuild.

Both were reproduced with numbers by the 2026-07-26 adversarial review of
``position_reconcile_unmodeled_once`` and are pinned here with the reviewer's
exact figures.

F1 — RESIDUAL COST BASIS PRO-RATED AT THE EXCHANGE AVERAGE PRICE. The rebuild
     split the exchange's aggregate ``market_exposure`` by CONTRACTS, which is
     only correct if every contract on the ticker was bought at the same price.
     On a price-skewed ticker it understated (or overstated) the reserve's
     premium — straight into ``deterministic_max_loss_cc``, gross, per-game, and
     every cap that scales off det-max. The residual basis is now the exchange
     figure MINUS the premium the modeled records already book: no price
     assumption at all, and booked premium identically EQUALS the exchange
     figure rather than approximately.

F2 — A LAGGING PAYLOAD SHRANK A RESERVE SILENTLY. The positions payload is
     fetched at the TOP of the reconcile (a paged loop of up to 25 sequential
     REST calls); the modeled book is read from live memory many round trips
     later. A fill confirmed inside that window is in the book but not yet in
     the exchange row, so ``exchange − modeled`` subtracted the new fill FROM
     the reserve — and the divergence net stayed silent because the shrunken
     book then matched the stale row exactly. The reserve is now only ever
     SHRUNK by a payload that accounts for the whole book on that ticker.

Helpers (the orphan ticker, the exchange-row builder, the book totals) are the
ones the B1 PoCs already use, imported so the two suites can never drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import structlog

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.ops.metrics import Metrics
from combomaker.ops.quote_app import (
    position_reconcile_unmodeled_once,
    reserve_from_exchange_figures,
)
from combomaker.risk.exposure import ExposureBook, OpenPosition
from combomaker.sim.book_model import build_book_model
from tests.test_book_completeness import (
    ORPHAN,
    _book_totals,
    _fill_position,
    _MutableRest,
    _positions_payload,
    _row,
    _store_with_orphan_fill,
)
from tests.test_rehydrate_positions import CONV, IS_ME


async def test_f1_residual_basis_exact_expensive_reserve_cheap_fill(
    tmp_path: Path,
) -> None:
    """PRICE SKEW, reserve dear / fill cheap. Reserve bought at 90c (500 centi /
    45,000cc) plus a modeled fill at 20c (200 centi / 4,000cc); the exchange
    reports the aggregate 700 centi / 49,000cc.

    Pro-rating by CONTRACTS assumed one uniform price: ceil(49,000x500/700) =
    35,000cc for the residual, so the book carried 35,000 + 4,000 = 39,000cc — a
    20.4% UNDERSTATEMENT (and WITHOUT the B1 rebuild at all the same scenario
    booked the full 49,000cc, so this was a REGRESSION). Subtracting what the
    modeled records already book needs no price assumption: booked premium is
    identically EQUAL to the exchange figure."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 45_000),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 2_000))
        assert _book_totals(exposure, ORPHAN) == (700, 49_000)

        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 49_000)))
        metrics = Metrics(FakeClock())
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, metrics, subaccount=0
        )

        # EXACT, not approximate: reserve premium = exchange - modeled.
        assert _book_totals(exposure, ORPHAN) == (700, 49_000)
        reserve = exposure.positions[f"reserve:{ORPHAN}"]
        assert int(reserve.contracts) == 500
        assert reserve.max_loss_cc == 45_000
        assert int(reserve.entry_price_cc) == 9_000  # 90c, the price actually paid
        # The defeated arithmetic, for the record.
        prorated = -(-49_000 * 500 // 700)
        assert prorated == 35_000
        assert reserve.max_loss_cc != prorated
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
        model = build_book_model(exposure.positions.values(), marginals=lambda _t: 0.5)
        assert model.reserved_loss_cc == float(45_000)
    finally:
        await store.close()


async def test_f1_residual_basis_exact_cheap_reserve_expensive_fill(
    tmp_path: Path,
) -> None:
    """REVERSE SKEW, reserve cheap / fill dear. Reserve at 10c (500 centi /
    5,000cc) plus a modeled fill at 90c (200 centi / 18,000cc); exchange
    aggregate 700 centi / 23,000cc. Pro-rating gave the residual
    ceil(23,000x500/700) = 16,429cc, entry ceil to 32.86c, booked 16,430 +
    18,000 = 34,430cc — a 49.7% OVERSTATEMENT. The exact basis books 23,000cc,
    the exchange figure."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 5_000),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 9_000))
        assert _book_totals(exposure, ORPHAN) == (700, 23_000)

        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 23_000)))
        metrics = Metrics(FakeClock())
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, metrics, subaccount=0
        )

        assert _book_totals(exposure, ORPHAN) == (700, 23_000)
        reserve = exposure.positions[f"reserve:{ORPHAN}"]
        assert int(reserve.contracts) == 500
        assert reserve.max_loss_cc == 5_000
        assert int(reserve.entry_price_cc) == 1_000  # 10c
        # The defeated arithmetic, for the record: 34,430cc booked on 23,000cc.
        prorated_basis = -(-23_000 * 500 // 700)
        prorated_entry = -(-prorated_basis * 100 // 500)
        prorated_book = 500 * prorated_entry // 100 + 18_000
        assert (prorated_basis, prorated_book) == (16_429, 34_430)
        assert _book_totals(exposure, ORPHAN)[1] != prorated_book
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
    finally:
        await store.close()


async def test_f1_no_provable_remainder_holds_the_reserve(tmp_path: Path) -> None:
    """No PROVABLE remainder (the exchange's cost basis lags the modeled
    premium) ⇒ nothing is invented and nothing is silently zeroed: the existing
    reserve is HELD unchanged on the pre-existing unpriced branch (fail-safe
    LARGER, hard rule 6)."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 31_000),
            )
        )
        # Modeled premium (18,000) alone exceeds the exchange basis (17,000).
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 9_000))
        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 17_000)))
        metrics = Metrics(FakeClock())
        with structlog.testing.capture_logs() as cap:
            await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )
        reserve = exposure.positions[f"reserve:{ORPHAN}"]
        assert int(reserve.contracts) == 500
        assert reserve.max_loss_cc == 31_000
        assert [
            e
            for e in cap
            if e["event"] == "position_reconcile_reserve_residual_unpriced"
        ]
    finally:
        await store.close()


async def test_f2_lagging_payload_never_shrinks_the_reserve_silently(
    tmp_path: Path,
) -> None:
    """THE REVIEWER'S EXACT F2 PoC. True holding 700 centi / 43,400cc; the
    payload still shows the pre-fill 500 centi / 31,000cc because the fill
    confirmed after the paged positions fetch. ``exchange - modeled`` subtracted
    the new fill FROM the reserve, collapsing the book to 500 centi / 31,000cc
    (a 28.6% undercount) — and quantity_divergence fired ZERO times, because the
    shrunken book matched the stale row exactly. Now the reserve is HELD at full
    size and the condition ALARMS."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 31_000),
            )
        )
        # The fill that confirmed AFTER the payload snapshot.
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 6_200))
        assert _book_totals(exposure, ORPHAN) == (700, 43_400)

        rest = _MutableRest(_positions_payload(_row(ORPHAN, 500, 31_000)))
        metrics = Metrics(FakeClock())
        with structlog.testing.capture_logs() as cap:
            await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )

        # HELD at full size — the book still equals the TRUE holding.
        assert _book_totals(exposure, ORPHAN) == (700, 43_400)
        reserve = exposure.positions[f"reserve:{ORPHAN}"]
        assert int(reserve.contracts) == 500
        assert reserve.max_loss_cc == 31_000
        # ...and the condition is OBSERVABLE, not silent.
        assert metrics.counter("position_reconcile.quantity_divergence") >= 1
        assert metrics.counter("position_reconcile.reserve_hold_lagging_payload") == 1
        assert [
            e
            for e in cap
            if e["event"] == "position_reconcile_reserve_hold_lagging_payload"
        ]
        assert [
            e for e in cap if e["event"] == "position_reconcile_quantity_divergence"
        ]

        # Next pass, payload caught up: the book still equals exchange truth,
        # the reserve is already exact, and nothing churns.
        rest.set_payload(_positions_payload(_row(ORPHAN, 700, 43_400)))
        gen_before = exposure.position_generation
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, metrics, subaccount=0
        )
        assert _book_totals(exposure, ORPHAN) == (700, 43_400)
        assert int(exposure.positions[f"reserve:{ORPHAN}"].contracts) == 500
        assert exposure.position_generation == gen_before
        assert metrics.counter("position_reconcile.reserve_hold_lagging_payload") == 1
    finally:
        await store.close()


async def test_f2_payload_that_accounts_for_the_whole_book_still_shrinks(
    tmp_path: Path,
) -> None:
    """The guard is not a freeze: once the exchange row accounts for the whole
    book (a genuinely SETTLED-DOWN holding — here part of the reserve was closed
    on the app), the reserve shrinks to the true residual on the next pass."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 31_000),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 6_200))
        # Exchange >= book total (700): the row accounts for everything the book
        # holds and then some, so the residual is trustworthy.
        rest = _MutableRest(_positions_payload(_row(ORPHAN, 900, 55_800)))
        metrics = Metrics(FakeClock())
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, metrics, subaccount=0
        )
        assert _book_totals(exposure, ORPHAN) == (900, 55_800)
        assert int(exposure.positions[f"reserve:{ORPHAN}"].contracts) == 700
        assert exposure.positions[f"reserve:{ORPHAN}"].max_loss_cc == 43_400
        assert metrics.counter("position_reconcile.reserve_hold_lagging_payload") == 0
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
    finally:
        await store.close()


async def test_no_reserve_churn_across_repeated_passes(tmp_path: Path) -> None:
    """A steady payload must leave the book bit-identical pass after pass: no
    position-generation bumps (they invalidate in-flight MC snapshots), no
    premium drift from repeated rounding."""
    store = await _store_with_orphan_fill(tmp_path)
    try:
        exposure = ExposureBook(CONV, is_me_event=IS_ME)
        exposure.add_position(
            cast(
                OpenPosition,
                reserve_from_exchange_figures(ORPHAN, Side.NO, 500, 45_000),
            )
        )
        exposure.add_position(_fill_position(ORPHAN, f"fill:{ORPHAN}", 200, 2_000))
        rest = _MutableRest(_positions_payload(_row(ORPHAN, 700, 49_000)))
        metrics = Metrics(FakeClock())
        await position_reconcile_unmodeled_once(
            cast(Any, rest), exposure, store, metrics, subaccount=0
        )
        gen = exposure.position_generation
        entry = int(exposure.positions[f"reserve:{ORPHAN}"].entry_price_cc)
        for _ in range(5):
            await position_reconcile_unmodeled_once(
                cast(Any, rest), exposure, store, metrics, subaccount=0
            )
            assert _book_totals(exposure, ORPHAN) == (700, 49_000)
            assert int(exposure.positions[f"reserve:{ORPHAN}"].entry_price_cc) == entry
            assert exposure.position_generation == gen
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
        assert metrics.counter("position_reconcile.reserve_hold_lagging_payload") == 0
    finally:
        await store.close()
