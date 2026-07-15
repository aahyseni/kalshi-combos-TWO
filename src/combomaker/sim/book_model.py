"""Bridge: the live risk book → the Monte-Carlo engine's ``(legs, corr, positions)``
triple, built the PRICER's way (RISK_BUILD_PLAN Phase 4 / research doc M1).

The MC engine (``sim/engine.py``) is already a portfolio engine — ``_book_pnl``
sums every position's P&L on the SAME sampled leg-value matrix, so cross-combo
correlation through shared legs is already captured. What was missing, and what
this module supplies, is:

1. **The real book, not a hand-built one.** ``build_book_model`` turns the live
   ``ExposureBook`` positions (+ optional open-quote hypotheticals for a
   mass-acceptance run) into the engine triple.
2. **The pricer's joint, not ``np.eye``.** The correlation matrix is
   **block-diagonal by GAME** — cross-game pairs sit at ``cross_event_rho`` (≈0,
   measured), within-game pairs carry the typed prior. This is the "the risk unit
   is the GAME" fact (the P&L-sweep finding, B2) encoded directly, and it is the
   exact structure the pricer's ``build_sgp_correlation`` produces per combo. The
   old standing report MC used ``corr = np.eye`` (pure independence) — blind to
   the one thing that can rupture a NO-seller's book (many shared games breaking
   together). That bug (``ops/report.py`` F8) is what this closes.
3. **NO-side by SIGN FLIP, not a complement pseudo-leg.** A NO-selected leg keeps
   its within-game correlation: it contributes ``1 − value`` inside the payout
   product (``ComboPosition.leg_sides``), never an independent ``~ticker`` leg
   that would destroy the NO leg's correlation with the rest of its game. For a
   sell-only parlay seller EVERY position is NO, so this is not a corner case —
   it is every position. The old report MC invented ``~ticker`` pseudo-legs at
   independence; that is the core inconsistency M1 removes.
4. **Point / low / high bands.** Three global matrices from the three within-game
   rho bands, so book risk can be reported at the correlation-uncertainty band —
   CVaR at ``high`` is the conservative number that gates (the risk analogue of
   the pricer widening on the rho band).

**Reuse, not reimplementation (hard rule 8).** The block matrix is assembled by
the pricer's own ``copula.build_block_corr`` (the exact function the pricer calls
to build a combo's correlation), and the game grouping is the pricer's public
``pricing.grouping.game_key`` (the same key the copula correlates on and the
exposure book aggregates on, B2). The within-game pairwise rho is supplied by an
injected provider so the app can wire it to the SHIPPED ``SgpParams`` / config
rho table — this module never invents a correlation number.

**Fail-closed (hard rule 6 / quiet-failure defense #2).** A missing marginal for
any leg makes the whole model UNKNOWN (``unknown=True``); the caller must treat
an UNKNOWN book model as no-go (widen-or-no-quote), never feed a ``p=0.5``
placeholder into the stats — the exact violation the old report MC committed.

Money stays int centi-cents at the interface; the copula math is in probability
space (floats OK, hard rule 5). This module builds ONLY the inputs to
``sim/engine.py`` — it runs no MC itself, so it is pure and cheap to test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from combomaker.core.conventions import Side
from combomaker.pricing.copula import build_block_corr
from combomaker.pricing.grouping import game_key
from combomaker.risk.exposure import (
    MarginalProvider,
    OpenPosition,
)
from combomaker.sim.engine import ComboPosition, LegModel

# The within-game pairwise correlation for a pair of leg tickers, at the
# (low, point, high) band. Injected so the SHIPPED pricer config / SgpParams is
# the single source of the rho — this module never hardcodes a correlation.
# Returns None when the pair has no calibrated prior (the caller falls back to
# the flat default band).
WithinGameRhoProvider = Callable[
    [str, str], tuple[float, float, float] | None
]

# Cross-game off-diagonal rho: the pricer's ``cross_event_rho`` (config default
# 0.0). Different games are independent by construction (the measured fact); this
# is the ONE off-block value the assembled matrix carries.
DEFAULT_CROSS_EVENT_RHO = 0.0

# Flat within-game band used when the provider has no calibrated prior for a pair
# (a leg types UNKNOWN, or an untyped pair). Mirrors the pricer's fail-safe: a
# positive point mean with a band wide enough to span into the negative regime,
# so correlation UNCERTAINTY widens risk rather than hiding it. The point is a
# mild positive (same-game legs lean together); low reaches ≤0 (could be
# uncorrelated or negative), high is a conservative same-game ceiling.
DEFAULT_FLAT_BAND: tuple[float, float, float] = (-0.20, 0.10, 0.40)


@dataclass(frozen=True, slots=True)
class BookModel:
    """The engine triple for one book, at all three correlation bands.

    ``legs`` is the global leg universe (one ``LegModel`` per distinct
    ``market_ticker``, on its YES marginal). ``positions`` are the engine
    ``ComboPosition``s (leg_indices into ``legs``, per-leg NO handled via
    ``leg_sides``). ``corr_point`` / ``corr_low`` / ``corr_high`` are the three
    global block-diagonal correlation matrices (identical shape ``(n, n)``); risk
    gates on ``corr_high``. ``leg_index`` maps a ticker to its latent index (for
    tail attribution). ``event_by_index`` maps a latent index to that leg's
    ``event_ticker`` (for per-game tail attribution). ``unknown`` is True iff a
    marginal for a RISK-MODELED position was missing — the whole model is then
    no-go (fail-closed).

    ``reserved_loss_cc`` (P0-4) is the exact total premium of the
    CONSERVATIVELY-RESERVED holdings (``OpenPosition.risk_modeled=False`` — e.g.
    gated-off series with no subscribed leg books). Those positions are NOT
    sampled (their marginals are unavailable, so they never enter the leg
    universe or the correlation matrix, and a missing reserved marginal never
    forces the whole model UNKNOWN), but their premium is carried here as a
    DETERMINISTIC reserve the risk MC adds OUTSIDE model ES — so their
    whole-account risk never vanishes from the tail number while never being
    scored against a fabricated p=0.5."""

    legs: tuple[LegModel, ...]
    positions: tuple[ComboPosition, ...]
    corr_point: NDArray[np.float64]
    corr_low: NDArray[np.float64]
    corr_high: NDArray[np.float64]
    leg_index: dict[str, int]
    event_by_index: dict[int, str | None]
    unknown: bool
    reserved_loss_cc: float = 0.0

    def corr_for_band(self, band: str) -> NDArray[np.float64]:
        """The correlation matrix for a band name ("point"|"low"|"high")."""
        if band == "point":
            return self.corr_point
        if band == "low":
            return self.corr_low
        if band == "high":
            return self.corr_high
        raise ValueError(f"band must be point|low|high, got {band!r}")


def _position_to_combo(
    position: OpenPosition, leg_index: dict[str, int]
) -> ComboPosition:
    """Map an ``OpenPosition`` to the engine's ``ComboPosition``.

    - ``leg_indices`` = each leg's global latent index (its YES-marginal leg).
    - ``leg_sides`` = each leg's selected side, so a NO-selected leg contributes
      ``1 − value`` inside the payout product (correlation preserved; the M1 fix)
      instead of an independent complement pseudo-leg.
    - ``side`` = the POSITION side we hold (from ``our_side``): a long NO pays
      ``$1 − payout`` per contract (``combo_no_pays_complement``, promoted).
    - ``contracts`` = centi-contracts converted EXACTLY to fractional contracts
      (``centi_contracts / 100``): 3727 → 37.27, 40 → 0.40 (P0-6). NO forced
      one-contract minimum — a fractional position is scored at its true size, so
      the simulated per-contract·contracts P&L matches the analytic
      ``contracts·entry_price//100`` max loss to the cent. (The old
      ``max(1, centi//100)`` floored 37.27→37 and inflated 0.40→1.)
    - ``price_cc`` = the premium PAID per contract; fee 0 here (reconciled
      elsewhere).
    """
    indices = tuple(leg_index[leg.market_ticker] for leg in position.legs)
    leg_sides = tuple(
        ("yes" if leg.side == "yes" else "no") for leg in position.legs
    )
    return ComboPosition(
        leg_indices=indices,
        side="yes" if position.our_side is Side.YES else "no",
        contracts=int(position.contracts) / 100,
        price_cc=int(position.entry_price_cc),
        leg_sides=leg_sides,  # type: ignore[arg-type]  # narrowed to yes|no above
    )


def build_book_model(
    positions: Iterable[OpenPosition],
    *,
    marginals: MarginalProvider,
    within_game_rho: WithinGameRhoProvider | None = None,
    cross_event_rho: float = DEFAULT_CROSS_EVENT_RHO,
    flat_band: tuple[float, float, float] = DEFAULT_FLAT_BAND,
) -> BookModel:
    """Assemble the MC engine triple for the live book, the pricer's way.

    The global correlation is **block-diagonal by game**: cross-game pairs sit at
    ``cross_event_rho``; within-game pairs get the typed prior from
    ``within_game_rho`` (or the flat band when the pair has none). NO-selected
    legs are handled per position via ``leg_sides`` (no complement pseudo-leg).

    Fail-closed: a missing marginal on a RISK-MODELED position ⇒ ``unknown=True``;
    the model is still assembled (so the caller can inspect it) but ``unknown``
    marks it no-go. A leg whose marginal is missing gets ``p=0.5`` ONLY so the
    matrix has a valid entry — the ``unknown`` flag forbids using any stat
    computed from it. This is the opposite of the old report MC, which fed the 0.5
    into the stats and merely flagged it: here the flag is a HARD no-score for
    gating.

    P0-4 (usable MC without hiding unmodeled holdings): a CONSERVATIVELY-RESERVED
    position (``risk_modeled=False`` — a gated-off holding with no subscribed leg
    books, so its marginals are UNAVAILABLE) is NOT sampled. Its legs never enter
    the leg universe or the correlation matrix, so its missing marginal can NEVER
    force the whole model UNKNOWN and can NEVER poison the decomposition of the
    quote-eligible (risk-modeled) positions. Instead its EXACT premium is summed
    into ``reserved_loss_cc`` — a deterministic reserve the risk MC adds OUTSIDE
    the model ES. Its whole-account risk therefore stays in global capital
    accounting without ever being scored against a fabricated ``p=0.5``.
    """
    positions = list(positions)
    # P0-4: split the book. Only RISK-MODELED positions are sampled; RESERVED
    # holdings (unavailable marginals) carry a deterministic premium reserve.
    modeled_positions = [p for p in positions if p.risk_modeled]
    reserved_loss_cc = float(
        sum(p.max_loss_cc for p in positions if not p.risk_modeled)
    )

    # --- global leg universe: one LegModel per distinct ticker, YES marginal ---
    leg_index: dict[str, int] = {}
    legs: list[LegModel] = []
    event_by_index: dict[int, str | None] = {}
    game_of_index: dict[int, str | None] = {}
    unknown = False
    for position in modeled_positions:
        for leg in position.legs:
            if leg.market_ticker in leg_index:
                continue
            p = marginals(leg.market_ticker)
            if p is None:
                unknown = True
                p = 0.5  # placeholder ONLY; `unknown` forbids using the stats
            idx = len(legs)
            leg_index[leg.market_ticker] = idx
            legs.append(LegModel(p=p))
            event_by_index[idx] = leg.event_ticker
            game_of_index[idx] = (
                game_key(leg.event_ticker) if leg.event_ticker else None
            )

    n = len(legs)

    # --- positions → engine ComboPositions (RISK-MODELED only; P0-4) ----------
    combos = tuple(
        _position_to_combo(position, leg_index) for position in modeled_positions
    )

    if n == 0:
        empty = np.zeros((0, 0), dtype=np.float64)
        return BookModel(
            legs=(),
            positions=combos,
            corr_point=empty,
            corr_low=empty.copy(),
            corr_high=empty.copy(),
            leg_index=leg_index,
            event_by_index=event_by_index,
            unknown=unknown,
            reserved_loss_cc=reserved_loss_cc,
        )

    # --- within-game blocks: index sets keyed on the game code ---------------
    game_members: dict[str, list[int]] = {}
    for idx in range(n):
        game = game_of_index.get(idx)
        if game is None:
            continue  # an ungamed leg never correlates with another (fail-closed)
        game_members.setdefault(game, []).append(idx)

    # For each band, collect the (indices, rho) blocks build_block_corr consumes.
    # build_block_corr sets ONE pairwise-constant rho per block; when a game has a
    # single calibrated pair we use it, and when a game holds >2 legs with mixed
    # pair rhos we use the block's MOST CONSERVATIVE (max-magnitude-positive for
    # `high`, min for `low`) rho so the reported tail never understates — a game
    # block is a coarse but conservative summary of its pairwise structure, and
    # the pricer's own per-combo matrix is the exact object for a specific combo;
    # here we build the WHOLE-BOOK view where a single constant per game is the
    # tractable, conservative choice.
    def _blocks_for_band(band_idx: int) -> list[tuple[list[int], float]]:
        blocks: list[tuple[list[int], float]] = []
        for members in game_members.values():
            if len(members) < 2:
                continue
            # Gather every pair's band rho; pick the conservative representative.
            rhos: list[float] = []
            for a_pos in range(len(members)):
                for b_pos in range(a_pos + 1, len(members)):
                    ta = _ticker_of(leg_index, members[a_pos])
                    tb = _ticker_of(leg_index, members[b_pos])
                    band = (
                        within_game_rho(ta, tb) if within_game_rho is not None else None
                    )
                    if band is None:
                        band = flat_band
                    rhos.append(band[band_idx])
            if not rhos:
                continue
            # high band → the most positive rho (fattest joint tail); low band →
            # the most negative; point → the average (a neutral representative).
            if band_idx == 2:
                rho = max(rhos)
            elif band_idx == 0:
                rho = min(rhos)
            else:
                rho = float(np.mean(rhos))
            rho = _clamp_open_unit(rho)
            blocks.append((members, rho))
        return blocks

    corr_low = build_block_corr(
        n, _blocks_for_band(0), default_rho=cross_event_rho
    )
    corr_point = build_block_corr(
        n, _blocks_for_band(1), default_rho=cross_event_rho
    )
    corr_high = build_block_corr(
        n, _blocks_for_band(2), default_rho=cross_event_rho
    )

    return BookModel(
        legs=tuple(legs),
        positions=combos,
        corr_point=corr_point,
        corr_low=corr_low,
        corr_high=corr_high,
        leg_index=leg_index,
        event_by_index=event_by_index,
        unknown=unknown,
        reserved_loss_cc=reserved_loss_cc,
    )


def _ticker_of(leg_index: dict[str, int], idx: int) -> str:
    """Inverse lookup ticker←index (small books; O(n) is fine and pure)."""
    for ticker, i in leg_index.items():
        if i == idx:
            return ticker
    raise KeyError(idx)  # pragma: no cover — indices always come from leg_index


def _clamp_open_unit(rho: float) -> float:
    """build_block_corr requires rho strictly inside (-1, 1). Clamp a band value
    that a provider may have pushed to ±1 (comonotone/countermonotone) just
    inside, so the block builder never rejects it. The tiny inset (1e-6) is far
    below any material correlation and keeps the matrix PSD-repairable."""
    eps = 1e-6
    if rho <= -1.0:
        return -1.0 + eps
    if rho >= 1.0:
        return 1.0 - eps
    return rho


def combo_positions_for_quotes(
    hypotheticals: Sequence[OpenPosition],
    leg_index: dict[str, int],
) -> tuple[ComboPosition, ...]:
    """Map a batch of hypothetical (open-quote worse-side) positions to engine
    combos against an EXISTING leg universe — used for a mass-acceptance MC where
    the open quotes' worse-side fills are added as extra positions. Any leg not
    already in ``leg_index`` raises (the caller builds the universe with the
    quotes included first)."""
    return tuple(_position_to_combo(h, leg_index) for h in hypotheticals)
