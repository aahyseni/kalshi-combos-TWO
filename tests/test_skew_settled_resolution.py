"""SKEW settled-leg fact resolution (2026-08-01) — property tests.

The defect: at boot, exposure rehydration fed ALREADY-FINISHED games'
positions into the inventory-skew concentration input as if live (7/29 05:50Z
boot: 8 positions + 2 reserved on 7 finished 7/28 games; finished games never
un-concentrate). The fix: ``ExposureBook.snapshot(settled_facts=...)``
fact-resolves exchange-DETERMINED legs out of every concentration aggregate,
with the det-max FIX 2 semantics — a graded leg is a FACT, not concentration;
an UNRESOLVABLE leg stays fully counted (fail-closed).

Properties pinned here (the build's stated contract):
  - settled leg          => ZERO concentration contribution
  - unresolved leg       => FULL contribution (byte-identical to no provider)
  - mixed combo          => PARTIAL: live legs only (== the leg-stripped book)
  - determined position  => nothing, on every axis
  - leg-less reserve     => untouched, fully counted
  - boot == intraday     => identical (positions, facts) state gives identical
                            resolved snapshots regardless of construction order
  - flag off / no provider => byte-identical to today (the caps' view)
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    ExposureBook,
    ExposureSnapshot,
    LegRef,
    OpenPosition,
    concentration_live_legs,
)
from combomaker.risk.skew import SkewParams

CONV = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

G_DONE = "26JUL281840TEXTB"       # finished game
G_LIVE = "26JUL291910ATLNYM"      # live game
G_LIVE2 = "26JUL292140BOSATH"     # live game 2
E_DONE = f"KXMLBGAME-{G_DONE}"
E_LIVE = f"KXMLBGAME-{G_LIVE}"
E_LIVE2 = f"KXMLBGAME-{G_LIVE2}"

# The fields the skew path consumes (compute_inventory_skew + widen shadow +
# the leg-axis / conc profiles).
SKEW_FIELDS = (
    "delta_by_game",
    "worst_case_loss_by_game_cc",
    "gross_settlement_notional_by_game_cc",
    "committed_loss_by_family_cc",
    "committed_loss_by_entity_cc",
    "loss_by_entity_cc",
    "dir_entries_by_game",
    "committed_dir_entries_by_game",
)


def leg(t: str, ev: str | None, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=t, event_ticker=ev, side=side)


def pos(pid: str, legs: list[LegRef], contracts: int = 1500, price: int = 3000) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"combo-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(price),
        legs=tuple(legs),
    )


def book_of(*positions: OpenPosition) -> ExposureBook:
    book = ExposureBook(CONV)
    for p in positions:
        book.add_position(p)
    return book


MIDS: dict[str, float] = {
    f"KXMLBGAME-{G_DONE}-TEX": 0.97,   # settled market still printing (trap)
    f"KXMLBTOTAL-{G_DONE}-7": 0.02,
    f"KXMLBGAME-{G_LIVE}-ATL": 0.55,
    f"KXMLBTOTAL-{G_LIVE}-8": 0.6,
    f"KXMLBGAME-{G_LIVE2}-BOS": 0.5,
    f"KXMLBKS-{G_LIVE2}-BOSX-5": 0.45,
}


def marginals(t: str) -> float | None:
    return MIDS.get(t)


def facts_from(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda t: mapping.get(t)


def fields(s: ExposureSnapshot) -> dict[str, object]:
    return {f: getattr(s, f) for f in SKEW_FIELDS}


# --- the resolution rule itself -------------------------------------------


def test_live_legs_rule_side_aware() -> None:
    a = leg("M1", E_DONE, "yes")
    b = leg("M2", E_LIVE, "yes")
    n = leg("M3", E_DONE, "no")
    f = facts_from({"M1": 0.0})
    assert concentration_live_legs((a, b), f) is None      # selected yes LOST
    f = facts_from({"M1": 1.0})
    assert concentration_live_legs((a, b), f) == (b,)       # selected yes WON
    f = facts_from({"M3": 1.0})
    assert concentration_live_legs((n, b), f) is None       # selected no LOST
    f = facts_from({"M3": 0.0})
    assert concentration_live_legs((n, b), f) == (b,)       # selected no WON
    # every leg selected-WON => determined (combo hit) => None
    f = facts_from({"M1": 1.0, "M2": 1.0})
    assert concentration_live_legs((a, b), f) is None


def test_live_legs_rule_fail_closed() -> None:
    a = leg("M1", E_DONE)
    b = leg("M2", E_LIVE)
    # no facts => identity (the SAME tuple object — no allocation)
    none_facts = facts_from({})
    assert concentration_live_legs((a, b), none_facts) == (a, b)
    # a non-binary "fact" is NOT a settlement: fully counted
    for garbage in (0.5, -1.0, 2.0, 0.999999):
        f = facts_from({"M1": garbage})
        assert concentration_live_legs((a, b), f) == (a, b), garbage
    # leg-less reserve: untouched
    assert concentration_live_legs((), facts_from({"M1": 0.0})) == ()


# --- snapshot-level properties --------------------------------------------


def _settled_book() -> tuple[OpenPosition, OpenPosition, OpenPosition, dict[str, float]]:
    """dead: settled-lost leg; part: settled-won leg + live leg; live: untouched."""
    dead = pos(
        "dead",
        [leg(f"KXMLBGAME-{G_DONE}-TEX", E_DONE), leg(f"KXMLBTOTAL-{G_LIVE}-8", E_LIVE)],
    )
    part = pos(
        "part",
        [leg(f"KXMLBTOTAL-{G_DONE}-7", E_DONE, "no"), leg(f"KXMLBGAME-{G_LIVE}-ATL", E_LIVE)],
    )
    live = pos(
        "live",
        [leg(f"KXMLBGAME-{G_LIVE2}-BOS", E_LIVE2), leg(f"KXMLBKS-{G_LIVE2}-BOSX-5", E_LIVE2)],
    )
    facts = {
        f"KXMLBGAME-{G_DONE}-TEX": 0.0,  # dead's selected yes LOST
        f"KXMLBTOTAL-{G_DONE}-7": 0.0,   # part's selected no WON -> drops out
    }
    return dead, part, live, facts


def test_settled_leg_zero_contribution_and_dead_position_gone() -> None:
    dead, part, live, facts = _settled_book()
    snap = book_of(dead, part, live).snapshot(
        marginals, mass_acceptance=True, settled_facts=facts_from(facts)
    )
    # The finished game vanishes from EVERY per-game aggregate.
    assert G_DONE not in snap.delta_by_game
    assert G_DONE not in snap.worst_case_loss_by_game_cc
    assert G_DONE not in snap.gross_settlement_notional_by_game_cc
    assert G_DONE not in snap.dir_entries_by_game
    # The dead position contributes nothing anywhere — its live-game leg too.
    assert snap.worst_case_loss_by_game_cc[G_LIVE] == part.max_loss_cc
    # Settled legs' family/entity keys carry no premium.
    assert "KXMLBTOTAL:no" not in snap.committed_loss_by_family_cc
    assert "KXMLBGAME:yes" in snap.committed_loss_by_family_cc


def test_unresolved_is_byte_identical_to_no_provider() -> None:
    dead, part, live, _facts = _settled_book()
    book = book_of(dead, part, live)
    raw = book.snapshot(marginals, mass_acceptance=True)
    none_kw = book.snapshot(marginals, mass_acceptance=True, settled_facts=None)
    unresolved = book.snapshot(
        marginals, mass_acceptance=True, settled_facts=facts_from({})
    )
    assert fields(raw) == fields(none_kw) == fields(unresolved)
    assert raw.gross_notional_cc == unresolved.gross_notional_cc
    assert raw.loss_by_combo_cc == unresolved.loss_by_combo_cc


def test_mixed_combo_partial_equals_leg_stripped_book() -> None:
    dead, part, live, facts = _settled_book()
    resolved = book_of(dead, part, live).snapshot(
        marginals, mass_acceptance=True, settled_facts=facts_from(facts)
    )
    # Reference: the book with dead dropped and part's settled leg stripped.
    stripped_part = dataclasses.replace(part, legs=(part.legs[1],))
    reference = book_of(stripped_part, live).snapshot(marginals, mass_acceptance=True)
    assert fields(resolved) == fields(reference)
    assert resolved.gross_notional_cc == reference.gross_notional_cc


def test_delta_uses_live_legs_only_even_when_settled_market_still_prints() -> None:
    # The trap: the settled market's book still prints 0.97 through the
    # settlement timer. The live leg's delta must be the STRIPPED product
    # (exclude the graded leg), not x0.97.
    dead, part, live, facts = _settled_book()
    resolved = book_of(part).snapshot(
        marginals, mass_acceptance=True, settled_facts=facts_from(facts)
    )
    stripped = dataclasses.replace(part, legs=(part.legs[1],))
    reference = book_of(stripped).snapshot(marginals, mass_acceptance=True)
    assert resolved.delta_by_game == reference.delta_by_game
    assert resolved.delta_by_market == reference.delta_by_market


def test_all_legs_selected_won_is_determined() -> None:
    hit = pos(
        "hit",
        [leg(f"KXMLBGAME-{G_DONE}-TEX", E_DONE), leg(f"KXMLBTOTAL-{G_DONE}-7", E_DONE)],
    )
    facts = {f"KXMLBGAME-{G_DONE}-TEX": 1.0, f"KXMLBTOTAL-{G_DONE}-7": 1.0}
    snap = book_of(hit).snapshot(
        marginals, mass_acceptance=True, settled_facts=facts_from(facts)
    )
    for f in SKEW_FIELDS:
        assert not getattr(snap, f), (f, getattr(snap, f))
    assert snap.gross_notional_cc == 0


def test_legless_reserve_untouched() -> None:
    reserve = OpenPosition(
        position_id="reserve", combo_ticker="combo-r", collection=None,
        our_side=Side.NO, contracts=CentiContracts(1000),
        entry_price_cc=CentiCents(5000), legs=(), risk_modeled=False,
    )
    facts = facts_from({"anything": 0.0})
    raw = book_of(reserve).snapshot(marginals, mass_acceptance=True)
    resolved = book_of(reserve).snapshot(
        marginals, mass_acceptance=True, settled_facts=facts
    )
    assert fields(raw) == fields(resolved)
    assert resolved.gross_notional_cc == reserve.max_loss_cc


def test_boot_equals_intraday_on_identical_state() -> None:
    # Boot: positions rehydrated all at once, facts already cached.
    # Intraday: positions added over time, facts landing later.
    # Identical final (positions, facts) => identical resolved snapshots —
    # the one-input-resolution-path guarantee.
    dead, part, live, facts = _settled_book()
    f = facts_from(facts)
    boot = book_of(dead, part, live).snapshot(
        marginals, mass_acceptance=True, settled_facts=f
    )
    intraday_book = ExposureBook(CONV)
    for p in (live, dead):
        intraday_book.add_position(p)
    # facts "land" between adds — irrelevant: resolution reads at snapshot time
    intraday_book.add_position(part)
    intraday = intraday_book.snapshot(marginals, mass_acceptance=True, settled_facts=f)
    assert fields(boot) == fields(intraday)


def test_caps_view_is_never_resolved() -> None:
    # The same book, snapshotted WITHOUT the provider (every limit-check /
    # confirm-path call site): settled positions still count in full — the
    # refusal layer's view is untouched by this fix.
    dead, part, live, facts = _settled_book()
    book = book_of(dead, part, live)
    caps_view = book.snapshot(marginals, mass_acceptance=True)
    assert G_DONE in caps_view.worst_case_loss_by_game_cc
    assert caps_view.worst_case_loss_by_game_cc[G_LIVE] == (
        dead.max_loss_cc + part.max_loss_cc
    )
    total_loss = sum(p.max_loss_cc for p in (dead, part, live))
    assert caps_view.gross_notional_cc == total_loss


def test_flag_defaults_off() -> None:
    # Ship dark: the SkewParams flag (and therefore the lifecycle's provider
    # gate) defaults to today's behaviour.
    assert SkewParams().settled_fact_resolution is False
    from combomaker.ops.config import SkewConfig

    assert SkewConfig().settled_fact_resolution is False
