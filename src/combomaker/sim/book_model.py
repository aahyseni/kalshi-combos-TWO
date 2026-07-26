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
4. **TWO NAMED JOINTS, split by AXIS (2026-07-26).** ``corr_location_point`` is
   the EXACT per-pair matrix at the pricing band — the joint the fills were priced
   on, and the only one the LOCATION axis (EV / P(book) / p_night) may use.
   ``corr_tail_stress_{low,point,high}`` collapse each game to one conservative
   scalar and are what EVERY ENFORCED GATE samples: CVaR at ``high`` is the
   conservative number that gates (the risk analogue of the pricer widening on the
   rho band) PLUS an explicit stand-in for the tail dependence a Gaussian copula
   does not have. Neither joint is a knob and neither substitutes for the other —
   the reasoning is written out in full at the construction site in
   ``build_book_model``.

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
    """The engine triple for one book, carrying TWO EXPLICITLY NAMED JOINTS.

    ``legs`` is the global leg universe (one ``LegModel`` per distinct
    ``market_ticker``, on its YES marginal). ``positions`` are the engine
    ``ComboPosition``s (leg_indices into ``legs``, per-leg NO handled via
    ``leg_sides``). ``leg_index`` maps a ticker to its latent index (for tail
    attribution). ``event_by_index`` maps a latent index to that leg's
    ``event_ticker`` (for per-game tail attribution). ``unknown`` is True iff a
    marginal for a RISK-MODELED position was missing — the whole model is then
    no-go (fail-closed).

    **THE TWO JOINTS (2026-07-26 AXIS SPLIT — read this before using either).**
    Both are global block-diagonal-by-game matrices of identical shape ``(n, n)``
    with cross-game pairs at ``cross_event_rho``; they differ ONLY in how a game's
    WITHIN-game pairs are filled, and each one owns exactly one axis:

    * ``corr_location_point`` — the **PRICING joint**. The EXACT per-pair matrix
      at the POINT band: every within-game pair carries its own measured rho, so
      this reproduces the pricer's own ``build_sgp_correlation`` for those legs.
      This is the joint the fills were actually priced on, so it — and only it —
      may produce the LOCATION axis (EV, P(book), p_night, std). It exists at the
      point band ONLY, because the location axis is DEFINED at the pricing band;
      there is no "location at high" to ask for.
    * ``corr_tail_stress_point`` / ``_low`` / ``_high`` — the **TAIL-DEPENDENCE
      STRESS joint**. Each game collapses to ONE scalar (max of its pair rhos at
      ``high``, min at ``low``, mean at ``point``) written onto EVERY pair in that
      game. Deliberately coarser and deliberately more adverse: it is a crude
      stand-in for the TAIL DEPENDENCE a Gaussian copula at low per-pair rho does
      not have (see the construction site in ``build_book_model``). EVERY ENFORCED
      GATE reads this joint (ES/CVaR, governing model ES, P(ruin), the loss-quantile
      envelope, det-max, tail attribution) — ``corr_tail_stress_high`` is the
      gating matrix.

    A field named plain ``corr_*`` no longer exists precisely so no caller can pick
    a joint by accident: ask for the location joint or the stress joint by name.

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
    # JOINT 1 — LOCATION / PRICING (exact per-pair, point band only).
    corr_location_point: NDArray[np.float64]
    # JOINT 2 — TAIL-DEPENDENCE STRESS (game-wide collapse, all three bands).
    corr_tail_stress_point: NDArray[np.float64]
    corr_tail_stress_low: NDArray[np.float64]
    corr_tail_stress_high: NDArray[np.float64]
    leg_index: dict[str, int]
    event_by_index: dict[int, str | None]
    unknown: bool
    reserved_loss_cc: float = 0.0

    def corr_tail_stress_for_band(self, band: str) -> NDArray[np.float64]:
        """The TAIL-DEPENDENCE STRESS matrix for a band ("point"|"low"|"high").

        This is the joint EVERY ENFORCED GATE samples (ES/CVaR, governing model
        ES, P(ruin), loss-quantile envelope, det-max, tail attribution). It is NOT
        the joint the quotes were priced on — for that ask for
        ``corr_location_point`` explicitly."""
        if band == "point":
            return self.corr_tail_stress_point
        if band == "low":
            return self.corr_tail_stress_low
        if band == "high":
            return self.corr_tail_stress_high
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
    # Resolve each distinct leg's marginal ONCE (the provider walks feed → settled
    # cache; call it per ticker, not per occurrence).
    _marg: dict[str, float | None] = {}

    def _priced(ticker: str) -> float | None:
        if ticker not in _marg:
            _marg[ticker] = marginals(ticker)
        return _marg[ticker]

    # P0-4 + 2026-07-23 fix: split the book. A position is SAMPLED only if it is
    # risk-modeled AND every one of its legs has a marginal. A ``risk_modeled=False``
    # holding (gated series) OR a risk-modeled position with an UNPRICEABLE/UNGRADED
    # leg — an active-but-EMPTY in-play book, or a closed-but-not-yet-graded leg — is
    # RESERVED instead: its EXACT max loss folds into the deterministic reserve. A
    # held position's max loss is a known FACT even when its leg cannot be priced, so
    # reserving it (conservative — assumes it loses in full) keeps the snapshot
    # USABLE rather than forcing the WHOLE model UNKNOWN, which historically darked
    # EVERY quote while one held combo's game was in progress (live 2026-07-23: a $5
    # combo with an in-play, empty-book leg blocked the entire book). Fail-closed
    # paired with the known-max-loss fact (the standing "full state awareness" rule).
    modeled_positions: list[OpenPosition] = []
    reserved_loss_cc = 0.0
    for position in positions:
        if position.risk_modeled and all(
            _priced(leg.market_ticker) is not None for leg in position.legs
        ):
            modeled_positions.append(position)
        else:
            reserved_loss_cc += float(position.max_loss_cc)

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
            p = _priced(leg.market_ticker)
            if p is None:
                # Unreachable: modeled_positions are all-priced by construction.
                # Kept as belt-and-braces fail-closed if a provider ever returns
                # non-deterministically within one build.
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
            corr_location_point=empty,
            corr_tail_stress_point=empty.copy(),
            corr_tail_stress_low=empty.copy(),
            corr_tail_stress_high=empty.copy(),
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

    # === THE TWO JOINTS (2026-07-26 AXIS SPLIT) ==============================
    #
    # This is the ONE place both within-game joints are built. They are built
    # side by side, from the SAME resolved pair bands, because the whole point is
    # that the difference between them is a deliberate modelling choice per AXIS —
    # not an accident, and not something a later reader may "unify".
    #
    # (1) LOCATION / PRICING joint — ``corr_location_point``, EXACT PER-PAIR.
    #     ``build_block_corr`` starts at ``default_rho`` and LATER blocks override
    #     earlier ones on overlapping pairs (pricing/copula.py), so a 2-index block
    #     ``([i, j], rho_ij)`` per within-game PAIR writes the exact per-pair
    #     matrix — byte-for-byte the object the pricer's own
    #     ``build_sgp_correlation`` produces (same provider, same pairs). It is the
    #     joint the fills were PRICED on, so it is the correct — and the only
    #     honest — joint for the LOCATION axis (EV, P(book), p_night, std). The
    #     game-wide collapse below is measurably WRONG for that axis: on the live
    #     48-position book it inflated 636 ordered pairs and deflated ZERO, lifted
    #     the within-game off-diagonal mean from +0.077 to +0.755, and FLIPPED 53
    #     measured-NEGATIVE pairs to +0.95 — real within-game HEDGES re-marked as
    #     near-comonotone, so a fill sold at fair+markup marked NEGATIVE on
    #     arrival BY CONSTRUCTION. It is also NON-STATIONARY on that axis: adding
    #     ANY leg to a game can only raise that game's max, so the marked book
    #     degraded monotonically as the book grew in a game, INCLUDING when the
    #     added position HEDGED it (the mutex-blind failure the standing directive
    #     flags as dangerous).
    #
    # (2) TAIL-DEPENDENCE STRESS joint — ``corr_tail_stress_{low,point,high}``,
    #     the game-wide COLLAPSE, PRESERVED DELIBERATELY. Every ENFORCED gate
    #     samples this one. **Why the tail keeps the collapse even though the
    #     per-pair matrix is the more accurate description of the pairwise
    #     structure:** the sampler is a GAUSSIAN COPULA, and a Gaussian copula has
    #     ZERO tail dependence at any rho < 1 — at a low per-pair rho its joint
    #     extremes are almost independent. Real games do NOT behave that way: one
    #     blowout hits every over in that game at once, an early red card sinks
    #     every side of the same match together, so the joint that prices the
    #     CENTRE of the distribution understates the CORNER of it. The game-wide
    #     max is a crude but explicit stand-in for that missing tail dependence:
    #     it says "in the 1% corner, treat a game as moving as one". Replacing it
    #     with the exact low-rho per-pair matrix does not merely re-describe the
    #     joint, it DELETES that stress — measured on the 48-position/144-leg live
    #     -shaped book (seed 7, 20k, band=high) the within-game off-diagonal mean
    #     fell +0.9217 -> +0.1486 and the enforced gates collapsed with it:
    #     governing_model_es_99 103,893 -> 0, es_99 83,971 -> 0, P(ruin) 0.0137 ->
    #     0.0000, and the 99th loss quantile flipped from a +75,405 LOSS to a
    #     48,393 GAIN. Those are exactly the objects ``risk/limits.py`` enforces
    #     (portfolio_kill_tail_prob, portfolio_cvar_frac,
    #     portfolio_ruin_prob_budget), so that change would have silently removed a
    #     live safety layer. The correlation BAND is uncertainty about the centre;
    #     it is not a tail-dependence model, and it cannot substitute for one.
    #     Therefore: the tail keeps the conservative construction until the
    #     sampler itself grows real tail dependence (a t-copula / explicit
    #     same-game shock factor), at which point THIS is the comment to revisit.
    #     No flag, no number: an axis chooses its joint by name, structurally.
    #
    # Both joints are PSD-repaired downstream by ``build_block_corr``
    # (``nearest_psd``; measured max |delta| 0.0032 on the live book).
    #
    # Structural (Dixon-Coles) legs are unaffected by either: ``sim/structural_book``
    # samples a plan's legs exactly from the scoreline joint and NEVER reads their
    # corr entries, and the structural/copula split is decided downstream from the
    # leg universe, which this does not touch.

    # Ticker by latent index, resolved ONCE. (The pre-split path did an O(n)
    # reverse scan of ``leg_index`` per leg per pair per band — O(n^3) — inside
    # the pair loop.)
    ticker_by_index: list[str] = [""] * n
    for _ticker, _i in leg_index.items():
        ticker_by_index[_i] = _ticker

    # Resolve each unordered within-game PAIR's (low, point, high) band ONCE — the
    # pre-split code re-queried the provider for every pair on EVERY band (3x the
    # calls). Kept GROUPED BY GAME and in the original nested-loop order so the
    # collapse below aggregates exactly the same rhos in exactly the same order as
    # the pre-existing code did (float-identical max/min/mean).
    pair_bands_by_game: list[
        tuple[list[int], list[tuple[int, int, tuple[float, float, float]]]]
    ] = []
    for members in game_members.values():
        if len(members) < 2:
            continue
        pairs: list[tuple[int, int, tuple[float, float, float]]] = []
        for a_pos in range(len(members)):
            i = members[a_pos]
            for b_pos in range(a_pos + 1, len(members)):
                j = members[b_pos]
                band = (
                    within_game_rho(ticker_by_index[i], ticker_by_index[j])
                    if within_game_rho is not None
                    else None
                )
                pairs.append((i, j, band if band is not None else flat_band))
        pair_bands_by_game.append((members, pairs))

    def _location_blocks() -> list[tuple[list[int], float]]:
        """ONE 2-index block per within-game pair at the POINT band — the exact
        per-pair matrix, no game-wide collapse (JOINT 1)."""
        return [
            ([i, j], _clamp_open_unit(band[1]))
            for _members, pairs in pair_bands_by_game
            for i, j, band in pairs
        ]

    def _tail_stress_blocks(band_idx: int) -> list[tuple[list[int], float]]:
        """ONE whole-GAME block per game, carrying that game's most CONSERVATIVE
        representative rho (JOINT 2 — the tail-dependence stress). ``high`` takes
        the most positive pair rho (fattest joint tail), ``low`` the most
        negative, ``point`` the average. Preserved verbatim from the pre-split
        build so no enforced gate can move; see the block comment above for why
        the tail keeps it."""
        blocks: list[tuple[list[int], float]] = []
        for members, pairs in pair_bands_by_game:
            rhos = [band[band_idx] for _i, _j, band in pairs]
            if not rhos:
                continue
            if band_idx == 2:
                rho = max(rhos)
            elif band_idx == 0:
                rho = min(rhos)
            else:
                rho = float(np.mean(rhos))
            blocks.append((members, _clamp_open_unit(rho)))
        return blocks

    corr_location_point = build_block_corr(
        n, _location_blocks(), default_rho=cross_event_rho
    )
    corr_tail_stress_low = build_block_corr(
        n, _tail_stress_blocks(0), default_rho=cross_event_rho
    )
    corr_tail_stress_point = build_block_corr(
        n, _tail_stress_blocks(1), default_rho=cross_event_rho
    )
    corr_tail_stress_high = build_block_corr(
        n, _tail_stress_blocks(2), default_rho=cross_event_rho
    )

    return BookModel(
        legs=tuple(legs),
        positions=combos,
        corr_location_point=corr_location_point,
        corr_tail_stress_point=corr_tail_stress_point,
        corr_tail_stress_low=corr_tail_stress_low,
        corr_tail_stress_high=corr_tail_stress_high,
        leg_index=leg_index,
        event_by_index=event_by_index,
        unknown=unknown,
        reserved_loss_cc=reserved_loss_cc,
    )


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


def position_to_combo(
    position: OpenPosition, leg_index: dict[str, int]
) -> ComboPosition:
    """Public alias of :func:`_position_to_combo` (P0-1). Maps ONE risk-modeled
    ``OpenPosition`` to its engine ``ComboPosition`` against an EXISTING shared
    ``leg_index``. Used by the candidate-aware evaluator to build the PRE and POST
    combo lists against the SAME merged leg universe, so both are scored on the
    SAME sampled leg-value matrix (common random numbers). Any leg not already in
    ``leg_index`` raises (the caller assembled the merged universe first)."""
    return _position_to_combo(position, leg_index)
