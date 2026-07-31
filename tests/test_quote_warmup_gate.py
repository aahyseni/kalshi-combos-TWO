"""BOOT-WARMUP QUOTE GATE (2026-07-31).

Quote SENDING is held at startup until the FIRST evaluation on which the
confirm gate's book-risk usability predicate (``_book_risk_for_check`` —
REUSED, never duplicated) could pass. Proof obligations (the build spec's
four, plus throttle):

  1. BOOT HOLD: a non-empty (rehydrated) book with NO usable snapshot sends
     ZERO quotes — the skip is the dedicated SKIP_WARMUP_BOOK_RISK (held
     BEFORE pricing), not a post-pricing portfolio-cap decline.
  2. AUTO-OPEN: the first usable snapshot opens quoting with no operator
     action; the latch then stays open.
  3. NEVER-USABLE: a book whose snapshot never becomes usable stays silent
     (fail-closed) with a loud PERIODIC warning throttled to the snapshot
     freshness window (``book_risk_stale_after_s`` — an existing horizon,
     not a new knob).
  4. MID-RUN UNCHANGED: once open, a generation-stale snapshot declines via
     the portfolio caps exactly as before (skip_portfolio_cvar), NEVER by
     re-holding the warmup gate.
  5. EMPTY BOOK: a flat boot opens instantly (an empty book has no joint
     tail to measure — it must still quote).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.within_game_rho import sgp_within_game_rho_provider
from tests.test_book_risk_wiring import _build, _no_position, _sgp_params
from tests.test_filters import Harness
from tests.test_lifecycle import CROSS_EVENT_LEGS, rfq
from tests.test_pricing_engine import combo, seed_event


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _prov() -> object:
    return sgp_within_game_rho_provider(_sgp_params())


async def test_boot_hold_zero_quotes_before_first_usable_snapshot(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # Rehydrated non-empty book, NO snapshot ever computed: the boot state the
    # 2026-07-31 gate exists for. Huge bankroll so no OTHER cap can be the
    # reason nothing goes out.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=_prov(),
    )
    _no_position(exposure, contracts=100, price_cc=5_000)
    assert lifecycle.quote_warmup_open() is False
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []  # zero quote_sent before the first usable snapshot
    # Held by the WARMUP gate (pre-pricing), counted on its own metric.
    assert lifecycle._metrics.counter("quote.warmup_held") == 1  # noqa: SLF001


async def test_quoting_opens_automatically_on_first_usable_snapshot(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=_prov(),
    )
    _no_position(exposure, contracts=100, price_cc=5_000)
    # Warmup holds first...
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []
    # ...then the first USABLE snapshot publishes (the maintenance path) and
    # quoting opens with no operator action.
    lifecycle.recompute_book_risk()
    assert lifecycle.quote_warmup_open() is True
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1


async def test_never_usable_book_stays_silent_with_throttled_warning(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=_prov(), book_risk_stale_after_s=30.0,
    )
    _no_position(exposure, contracts=100, price_cc=5_000)
    # Never any snapshot: every evaluation holds (fail-closed, forever).
    assert lifecycle.quote_warmup_open() is False
    first_warn = lifecycle._warmup_last_warn_mono_ns  # noqa: SLF001
    assert first_warn is not None  # warned loudly on the first hold
    # Within the freshness window: throttled (no new warning stamp).
    h.clock.advance(1.0)
    assert lifecycle.quote_warmup_open() is False
    assert lifecycle._warmup_last_warn_mono_ns == first_warn  # noqa: SLF001
    # Past the freshness window: warns again (periodic, loud).
    h.clock.advance(31.0)
    assert lifecycle.quote_warmup_open() is False
    assert lifecycle._warmup_last_warn_mono_ns != first_warn  # noqa: SLF001
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []  # still silent


async def test_midrun_generation_staleness_behaviour_unchanged(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=_prov(), book_risk_stale_after_s=1_000_000.0,
    )
    _no_position(exposure, contracts=100, price_cc=5_000)
    lifecycle.recompute_book_risk()
    assert lifecycle.quote_warmup_open() is True  # boot window over
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1
    # MID-RUN: a fill mutates the book → the snapshot is generation-stale →
    # the portfolio caps fail closed per check, EXACTLY as before the gate.
    exposure.add_position(
        OpenPosition(
            position_id="fill2",
            combo_ticker="COMBO-2",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(5_000),
            legs=(LegRef("M1", "E1", "yes"),),
        )
    )
    await lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_2"))
    assert len(sender.created) == 1  # declined — but by the caps, not warmup
    # The one-way latch NEVER re-holds: warmup stays open and its skip metric
    # does not move (the decline was the caps' fail-closed, unchanged).
    assert lifecycle.quote_warmup_open() is True
    assert lifecycle._metrics.counter("quote.warmup_held") == 0  # noqa: SLF001


async def test_empty_book_boot_opens_instantly(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, cvar_frac="0.15",
        within_game_rho=_prov(), book_risk_stale_after_s=0.0,
    )
    assert not exposure.positions
    # Flat boot: nothing to measure ⇒ the gate opens on the first evaluation
    # and the first RFQ quotes normally (no warmup penalty on an empty book).
    assert lifecycle.quote_warmup_open() is True
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1
