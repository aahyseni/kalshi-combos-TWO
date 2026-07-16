"""P0-4 — usable MC without hiding unmodeled holdings.

The three MANDATORY tests from RISK_ENGINE_AUDIT_ACTION_PLAN.txt (Unmodeled
holdings):

1. Gated holdings remain in global deterministic/gross risk.
2. Missing held marginal does not poison unrelated candidate decomposition.
3. Missing marginal never becomes an ordinary usable p=0.5 MC score.

The mechanism: an exchange-held position on a series with no subscribed leg books
(gated-off allowlist) is rehydrated as a CONSERVATIVELY-RESERVED holding
(``OpenPosition.risk_modeled=False``). Such a position:
  - STILL counts its exact premium loss / gross notional / per-game concentration
    in the exposure snapshot (deterministic + gross caps) — its whole-account risk
    never vanishes;
  - is NEVER decomposed against marginals in the exposure snapshot (its marginals
    are never even queried), so it cannot flip ``unknown_marginals`` and poison the
    decomposition of the quote-eligible (risk-modeled) candidates;
  - is held OUTSIDE the portfolio model ES in the full-book MC — its premium is a
    deterministic reserve added to the operative ES, never a leg sampled at a
    fabricated ``p=0.5``.
"""
from __future__ import annotations

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import compute_book_risk

CONV = Conventions(
    verified=True, source="test",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def _wc_position() -> OpenPosition:
    """A quote-eligible (risk-modeled) WC position — legs have available marginals."""
    return OpenPosition(
        position_id="rehydrate:KXMVE-WC",
        combo_ticker="KXMVE-WC",
        collection="KXMVESPORTS",
        our_side=Side.NO,
        contracts=CentiContracts(5000),          # 50 contracts
        entry_price_cc=CentiCents(6000),          # $0.60
        legs=(
            LegRef("KXWCADVANCE-26JUL15ENGARG-ARG", "KXWCADVANCE-26JUL15ENGARG", "yes"),
            LegRef("KXWCGOAL-26JUL15ENGARG-ARGP-1", "KXWCGOAL-26JUL15ENGARG-ARGP", "yes"),
        ),
    )


def _mlb_reserved_position() -> OpenPosition:
    """A GATED-OFF (MLB) holding: no subscribed leg books ⇒ marginals unavailable.
    Rehydrated as a CONSERVATIVELY-RESERVED holding (risk_modeled=False)."""
    return OpenPosition(
        position_id="reserve:KXMVE-MLB",
        combo_ticker="KXMVE-MLB",
        collection="KXMVESPORTS",
        our_side=Side.NO,
        contracts=CentiContracts(3000),           # 30 contracts
        entry_price_cc=CentiCents(7000),           # $0.70
        legs=(
            LegRef("KXMLBGAME-26JUL16NYMPHI-PHI", "KXMLBGAME-26JUL16NYMPHI", "yes"),
            LegRef("KXMLBTOTAL-26JUL16NYMPHI-9", "KXMLBTOTAL-26JUL16NYMPHI", "yes"),
        ),
        risk_modeled=False,
    )


# The MLB legs have NO marginal (gated series, not subscribed); the WC legs do.
def _mixed_marginals(ticker: str) -> float | None:
    if ticker.startswith("KXMLB"):
        return None                    # gated-off leg book: unavailable
    return 0.6


# --- MANDATORY TEST 1: gated holdings remain in global deterministic/gross risk ---


def test_gated_holding_remains_in_global_deterministic_and_gross_risk() -> None:
    """A reserved (gated) holding's EXACT premium loss, gross settlement notional,
    and per-game concentration must all remain in the exposure snapshot — the
    deterministic + gross caps — exactly as if it were fully modeled."""
    book = ExposureBook(CONV)
    reserved = _mlb_reserved_position()
    book.add_position(reserved)

    snap = book.snapshot(_mixed_marginals, mass_acceptance=False)

    # Exact premium loss (LOSS axis): 3000 centi-ct * 7000 cc // 100 = 210_000 cc.
    assert reserved.max_loss_cc == 210_000
    assert snap.gross_notional_cc == 210_000

    # Gross settlement notional (CAPITAL-UTILIZATION axis): 3000 * $1 // 100.
    game = "26JUL16NYMPHI"
    assert snap.gross_settlement_notional_by_game_cc[game] == 300_000

    # Known per-game concentration (the game cluster is present with the full loss).
    assert snap.worst_case_loss_by_game_cc[game] == 210_000

    # And it did NOT flip unknown_marginals (a reserved holding is not a candidate
    # we failed to decompose — its marginals were never even queried).
    assert snap.unknown_marginals is False


# --- MANDATORY TEST 2: missing held marginal does not poison unrelated decomposition ---


def test_missing_held_marginal_does_not_poison_unrelated_candidate_decomposition() -> None:
    """A gated holding whose marginals are MISSING sits in the same book as a
    quote-eligible WC candidate whose marginals are AVAILABLE. The missing MLB data
    must not poison the WC decomposition: the snapshot stays usable
    (``unknown_marginals`` False) and the WC legs get real, non-zero deltas."""
    book = ExposureBook(CONV)
    book.add_position(_wc_position())              # marginals available
    book.add_position(_mlb_reserved_position())    # marginals MISSING (reserved)

    snap = book.snapshot(_mixed_marginals, mass_acceptance=False)

    # NOT poisoned: the whole-book check is still usable.
    assert snap.unknown_marginals is False

    # The WC (unrelated, quote-eligible) legs are decomposed with real deltas.
    assert "KXWCADVANCE-26JUL15ENGARG-ARG" in snap.delta_by_market
    assert "KXWCGOAL-26JUL15ENGARG-ARGP-1" in snap.delta_by_market
    assert snap.delta_by_market["KXWCADVANCE-26JUL15ENGARG-ARG"] != 0.0
    wc_game = "26JUL15ENGARG"
    assert wc_game in snap.delta_by_game

    # The MLB legs (missing marginals) contribute NO delta (they were reserved, not
    # scored) — so they neither zero-out nor contaminate the WC decomposition.
    assert "KXMLBGAME-26JUL16NYMPHI-PHI" not in snap.delta_by_market

    # Both positions' premium is still counted in the global loss axis.
    # WC: 5000*6000//100 = 300_000; MLB: 3000*7000//100 = 210_000.
    assert snap.gross_notional_cc == 300_000 + 210_000


def test_reserved_holding_does_not_force_whole_book_mc_unknown() -> None:
    """The full-book MC (build_book_model) must NOT go UNKNOWN because a RESERVED
    holding's marginal is missing. The old defect: any missing held marginal flipped
    the whole model UNKNOWN → the CVaR cap failed closed → ALL quoting stopped."""
    positions = [_wc_position(), _mlb_reserved_position()]
    model = build_book_model(positions, marginals=_mixed_marginals)

    # The reserved MLB marginals are missing, but the model is NOT unknown: only the
    # risk-modeled WC position is sampled; the MLB premium is a deterministic reserve.
    assert model.unknown is False
    # Only the WC legs entered the sampled leg universe (2 legs, not 4).
    assert len(model.legs) == 2
    assert "KXWCADVANCE-26JUL15ENGARG-ARG" in model.leg_index
    assert "KXMLBGAME-26JUL16NYMPHI-PHI" not in model.leg_index
    # The reserved premium is carried outside the sampled model.
    assert model.reserved_loss_cc == 210_000.0
    assert len(model.positions) == 1  # one sampled ComboPosition (the WC one)


def test_reserved_premium_lands_in_deterministic_max_outside_model() -> None:
    """The reserved holding's premium must appear in the DETERMINISTIC max-loss
    axis as a reserve, added OUTSIDE the sampled model ES — so a reserved holding's
    whole-account risk is never hidden from the deterministic-max cap. P0-3: it is
    NOT folded into the model-ES number."""
    positions = [_wc_position(), _mlb_reserved_position()]
    model = build_book_model(positions, marginals=_mixed_marginals)
    snap = compute_book_risk(model, n_samples=4000, seed=1, band="high")

    assert snap.usable  # a risk-modeled position exists ⇒ the snapshot gates

    # The deterministic maximum carries BOTH the sampled all-hit worst case AND the
    # reserved premium. Rebuild the model with the reserved holding REMOVED and the
    # deterministic maximum must drop by exactly the reserved premium (210_000 cc).
    modeled_only = build_book_model([_wc_position()], marginals=_mixed_marginals)
    snap_no_reserve = compute_book_risk(modeled_only, n_samples=4000, seed=1, band="high")

    assert (
        snap.deterministic_max_loss_cc
        == snap_no_reserve.deterministic_max_loss_cc + 210_000.0
    )
    # The deterministic maximum is an unconditional upper bound the sampled model
    # ES never exceeds (P0-3: separate axes, not max'd together).
    assert snap.deterministic_max_loss_cc >= snap.governing_model_es_99_cc


# --- MANDATORY TEST 3: missing marginal never becomes an ordinary usable p=0.5 ----


def test_missing_marginal_never_becomes_ordinary_usable_half() -> None:
    """The missing MLB marginals must NEVER be scored as an ordinary usable p=0.5.

    Two proofs:
    (a) In the exposure snapshot, the reserved position's marginals are never even
        queried — a marginal provider that would RAISE on the MLB tickers is never
        called for them (so no fabricated 0.5 leaks in).
    (b) In the full-book MC, the reserved legs never enter the sampled leg universe,
        so the p=0.5 matrix placeholder (used only to keep the matrix valid for
        risk-modeled missing legs) is never produced for them — the reserved premium
        is a deterministic reserve, not a sampled p=0.5 leg.
    """
    # (a) exposure snapshot: a provider that raises on the reserved (MLB) tickers.
    def _raise_on_reserved(ticker: str) -> float | None:
        if ticker.startswith("KXMLB"):
            raise AssertionError(
                f"reserved marginal {ticker} was queried — a missing held marginal "
                "must NEVER be scored (would risk a fabricated p=0.5)"
            )
        return 0.6

    book = ExposureBook(CONV)
    book.add_position(_wc_position())
    book.add_position(_mlb_reserved_position())
    # Does not raise ⇒ the reserved marginals were never queried.
    snap = book.snapshot(_raise_on_reserved, mass_acceptance=False)
    assert snap.unknown_marginals is False

    # (b) full-book MC: the reserved legs never become sampled p=0.5 legs.
    model = build_book_model(
        [_wc_position(), _mlb_reserved_position()], marginals=_mixed_marginals
    )
    # No sampled leg carries the fabricated 0.5 placeholder (the WC legs are 0.6).
    assert all(leg.p == 0.6 for leg in model.legs)
    assert model.reserved_loss_cc == 210_000.0


def test_all_reserved_book_is_still_deterministic_reserve_not_nogo() -> None:
    """A book that is ALL reserved (no risk-modeled position) still has a real
    deterministic reserve to account for — the MC returns that reserve on the
    deterministic max-loss axis rather than a no-go zero, and the snapshot is
    USABLE: the sampled tail is exactly 0 (nothing to sample) and the reserve
    gates on the deterministic axis. Grading it unusable fail-closed EVERY quote
    on a 100%-reserved book (live 2026-07-16: the one rehydrated gated-series
    position blocked all quoting via SKIP_PORTFOLIO_CVAR)."""
    model = build_book_model([_mlb_reserved_position()], marginals=_mixed_marginals)
    assert model.unknown is False
    assert len(model.positions) == 0
    assert model.reserved_loss_cc == 210_000.0

    snap = compute_book_risk(model, n_samples=4000, seed=1, band="high")
    assert snap.deterministic_max_loss_cc == 210_000.0
    # No sampled positions ⇒ the model-ES axis is zero (nothing sampled).
    assert snap.governing_model_es_99_cc == 0.0
    # P0-4 documented intent: "still USABLE ... not a no-go".
    assert snap.usable is True


def test_truly_empty_and_unknown_snapshots_stay_unusable() -> None:
    """The usable widening is EXACTLY the all-reserved case: a truly-empty book
    (no positions, no reserve) and an UNKNOWN model (missing risk-modeled
    marginal) both still fail closed."""
    empty = compute_book_risk(
        build_book_model([], marginals=_mixed_marginals), n_samples=100, seed=1
    )
    assert empty.usable is False

    unknown_snap = compute_book_risk(
        build_book_model([_wc_position()], marginals=lambda _t: None),
        n_samples=100,
        seed=1,
    )
    assert unknown_snap.usable is False
