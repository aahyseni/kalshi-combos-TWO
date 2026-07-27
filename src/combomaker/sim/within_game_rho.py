"""Book-risk within-game correlation provider, built the PRICER's way.

The portfolio-risk MC (``sim/book_model.build_book_model``) needs a
``WithinGameRhoProvider`` — a function ``(ticker_a, ticker_b) -> (low, point,
high) | None`` — to fill each within-game block of its global correlation matrix.
Without one it falls back to the flat ``DEFAULT_FLAT_BAND`` (a mild positive with
a wide band), which is BLIND to the real per-pair structure the pricer quotes on
(a same-game 2-leg book would show a flat tail, not the correlated tail the pricer
actually carries).

This module builds that provider from the SHIPPED ``SgpParams`` (the engine's own
``engine.sgp_params``) by calling the pricer's REAL ``build_sgp_correlation`` on
the two tickers as a synthetic 2-leg same-event pair, then reading the off-diagonal
of its (low, point, high) matrices. Hard rule 8: this REUSES the exact function the
pricer calls to build a combo's correlation — it never hardcodes or reimplements a
rho. A pair the pricer would type UNKNOWN falls through to ``build_sgp_correlation``'s
own flat fallback band (the fail-safe positive-with-negative-reach band), which the
provider returns like any other — so the risk view widens on an untyped pair exactly
as the pricer does.

Orientation note: ``build_sgp_correlation`` resolves marginal-DEPENDENT priors
(fav/dog moneyline curves, etc.) only when marginals are supplied; the whole-book
risk view has no single marginal per pair, so this provider calls it WITHOUT
marginals — the plain (marginal-less) entry applies, which is the pricer's own
fallback for a marginal-less caller and carries the conservative band. That is the
correct book-level summary: a per-combo exact orientation is the pricer's job at
quote time; the risk MC wants the conservative marginal-less band per PAIR.

NESTED SAME-LADDER EXCEPTION (2026-07-27). One class of pair is NOT a prior at
all: two rungs of ONE entity's ONE counting variable (a starter's K-ladder,
KXMLBKS-…-SEAGKIRBY68-3 x -4) are COMONOTONE BY ARITHMETIC — K>=4 implies K>=3 —
so their coupling is DERIVED from the two marginals, not looked up. Left
marginal-less, such a pair fell through to the plain ``player_ks|player_ks``
entry: +0.0400, the value MEASURED FOR TWO OPPOSING STARTERS, so the book's
largest concentration was modelled as if it were two different pitchers. This
provider therefore accepts an OPTIONAL marginal source (``bind_marginals`` /
the ``marginals=`` argument) used for EXACTLY ONE purpose — resolving those
nested pairs through the pricer's own ``sgp.nested_ladder_rho``. Every other
pair stays on the marginal-less call, so the orientation reasoning above is
unchanged and the blast radius is exactly the nested class. Unbound (tests,
offline tools), the provider behaves BYTE-IDENTICALLY to before.

WHERE THIS BAND LANDS (2026-07-26 AXIS SPLIT). ``build_book_model`` feeds this ONE
provider into BOTH of its named joints, and they use it differently on purpose:
  * ``corr_location_point`` — this band's POINT value is written onto each
    within-game pair INDIVIDUALLY (one 2-index block per pair), so the assembled
    book matrix reproduces the pricer's own per-pair ``build_sgp_correlation``
    matrix exactly — including measured-NEGATIVE (hedging) pairs. That is the
    joint the EV / P(book) axis marks on, because it is the joint we PRICED on.
  * ``corr_tail_stress_{low,point,high}`` — a game's pair bands are COLLAPSED to
    one scalar (max at ``high``) and written onto every pair in that game. Every
    ENFORCED gate rides this one: it is a deliberate over-statement standing in
    for the tail dependence a Gaussian copula does not have. See the construction
    site in ``sim/book_model.py`` for the full reasoning.
"""

from __future__ import annotations

from collections.abc import Callable

from combomaker.core.money import CentiCents
from combomaker.pricing.legtypes import classify_leg
from combomaker.pricing.sgp import (
    SgpParams,
    build_sgp_correlation,
    nested_ladder_rho,
    same_nested_ladder,
)
from combomaker.rfq.models import RfqLeg
from combomaker.sim.book_model import WithinGameRhoProvider

# The synthetic pair rides in ONE event so build_sgp_correlation treats it as a
# same-event pair (the only case that gets a typed prior; cross-event pairs get
# cross_event_rho, which is the book model's own off-block default anyway).
_PAIR_EVENT = "WITHIN_GAME_RHO_PROBE"

# The per-leg P(YES) source used ONLY for nested-ladder resolution. None for a
# leg the caller cannot price — the pair then keeps its marginal-less treatment.
MarginalSource = Callable[[str], float | None]


class SgpWithinGameRho:
    """A ``WithinGameRhoProvider`` backed by the shipped ``SgpParams``.

    Callable as ``(ticker_a, ticker_b) -> (low, point, high) | None``: the
    pricer's own within-game rho band for that pair, computed by
    ``build_sgp_correlation`` on the two tickers as a 2-leg same-event group.
    Always returns a band (never None) for a distinct pair: an untyped/UNKNOWN
    pair resolves to build_sgp_correlation's flat fallback band, which is exactly
    the conservative widening the pricer applies — so the book-risk view is never
    blind to a pair the pricer would widen on. Identical tickers (degenerate
    self-pair) return None so the book model leaves the diagonal alone.

    ``marginals`` (optional, also settable later via ``bind_marginals`` because
    the live wiring builds this provider BEFORE the lifecycle that owns the
    marginal source) is used for the nested-ladder class ONLY — see the module
    docstring. Unbound, or bound but unable to price either leg, the pair takes
    the unchanged marginal-less path.
    """

    __slots__ = ("_params", "_marginals", "_sgp_cache", "_sgp_cache_max")

    def __init__(
        self,
        params: SgpParams,
        marginals: MarginalSource | None = None,
        *,
        sgp_cache_max_pairs: int = 400_000,
    ) -> None:
        self._params = params
        self._marginals = marginals
        # PURE-FUNCTION MEMO (2026-07-27 confirm-window fix). The NON-nested
        # branch below is ``build_sgp_correlation(<two synthetic legs>, [[0,1]],
        # self._params, marginals=None)`` — a function of the ORDERED TICKER PAIR
        # and ``self._params`` and NOTHING else (no clock, no live book, no
        # marginals: that argument is literally None on this branch). ``_params``
        # is assigned once in ``__init__`` and never reassigned, so the mapping
        # pair -> band is CONSTANT for the life of this provider and memoising it
        # is byte-identical, not an approximation.
        #
        # WHY IT MATTERS: the confirm-path candidate gate resolves rho for EVERY
        # unordered pair of EVERY leg ticker in the book (O(T^2)); measured live
        # at ~0.126 ms/pair, that was 730-790 ms of SYNCHRONOUS event-loop time
        # per accept at T=108 / 5,778 pairs — the dominant term that pushed the
        # gate past the exchange's confirm window and discarded 70 already-won
        # auctions. Book-to-book the ticker set barely moves (one fill adds a
        # handful of tickers), so the memo makes each accept pay only for the
        # pairs that are genuinely NEW.
        #
        # The NESTED-LADDER branch is deliberately NOT cached: it reads the LIVE
        # marginals (p_a, p_b), so its band legitimately moves with the market.
        # It is also cheap — the allocation-free ``same_nested_ladder`` string
        # test rejects almost every pair before any work happens.
        self._sgp_cache: dict[tuple[str, str], tuple[float, float, float]] = {}
        # Pure MEMORY bound (not a risk/policy number): tickers churn daily, so
        # the live working set is far below this. On overflow the whole map is
        # dropped and refills — correctness is unaffected either way.
        self._sgp_cache_max = int(sgp_cache_max_pairs)

    def bind_marginals(self, marginals: MarginalSource) -> None:
        """Attach the per-leg P(YES) source used for nested-ladder resolution."""
        self._marginals = marginals

    def invalidate_sgp_cache(self) -> None:
        """Drop the pure-function pair memo. Only needed if ``SgpParams`` is ever
        mutated in place (nothing does today — it is assigned once at wiring);
        exists so a future params hot-reload has a correct seam to call."""
        self._sgp_cache.clear()

    @property
    def sgp_cache_size(self) -> int:
        """Number of memoised pairs (observability / tests)."""
        return len(self._sgp_cache)

    def _nested_band(self, ticker_a: str, ticker_b: str) -> tuple[float, float, float] | None:
        """The EXACT comonotone band for a same-ladder rung pair, or None.

        Delegates the whole decision — family registry, entity/game/series match,
        rung ordering, degenerate marginals — to the pricer's own
        ``sgp.nested_ladder_rho`` (hard rule 8: one implementation, two callers),
        so the risk view and the quote path can never disagree about which pairs
        are nested or about the value they take. Band width is ZERO: the coupling
        is an arithmetic identity, not a prior.

        ORDER MATTERS on this path: the candidate gate resolves rho for EVERY
        unordered ticker pair (O(n^2) at book size), and the marginal source is a
        live feed walk. So the TYPE-FREE, allocation-free ``same_nested_ladder``
        string test rejects almost every pair FIRST; classification and the two
        marginal reads happen only for pairs that already share a ladder."""
        marginals = self._marginals
        if marginals is None:
            return None
        if not same_nested_ladder(ticker_a, ticker_b):
            return None
        p_a = marginals(ticker_a)
        if p_a is None:
            return None
        p_b = marginals(ticker_b)
        if p_b is None:
            return None
        resolved = nested_ladder_rho(
            classify_leg(ticker_a), classify_leg(ticker_b), ticker_a, ticker_b, p_a, p_b
        )
        if resolved is None:
            return None
        latent = resolved[0]
        return (latent, latent, latent)

    def __call__(self, ticker_a: str, ticker_b: str) -> tuple[float, float, float] | None:
        if ticker_a == ticker_b:
            return None  # self-pair: no off-diagonal to fill
        nested = self._nested_band(ticker_a, ticker_b)
        if nested is not None:
            return nested
        # Pure-function memo (see __init__): this branch depends only on the two
        # tickers and the immutable params. Keyed on the ORDERED pair — no
        # symmetry is assumed of build_sgp_correlation.
        cache_key = (ticker_a, ticker_b)
        cached = self._sgp_cache.get(cache_key)
        if cached is not None:
            return cached
        legs = (
            RfqLeg(
                market_ticker=ticker_a,
                event_ticker=_PAIR_EVENT,
                side="yes",
                yes_settlement_value_cc=CentiCents(0),
            ),
            RfqLeg(
                market_ticker=ticker_b,
                event_ticker=_PAIR_EVENT,
                side="yes",
                yes_settlement_value_cc=CentiCents(0),
            ),
        )
        # One same-event group over both legs; no marginals (whole-book view has
        # no single per-pair marginal — the plain entry / conservative band wins).
        sgp = build_sgp_correlation(legs, [[0, 1]], self._params, marginals=None)
        low = float(sgp.corr_low[0, 1])
        point = float(sgp.corr[0, 1])
        high = float(sgp.corr_high[0, 1])
        band = (low, point, high)
        if len(self._sgp_cache) >= self._sgp_cache_max:
            self._sgp_cache.clear()  # memory bound only; refills correctly
        self._sgp_cache[cache_key] = band
        return band


def sgp_within_game_rho_provider(
    params: SgpParams, marginals: MarginalSource | None = None
) -> SgpWithinGameRho:
    """Build the shipped-``SgpParams`` within-game rho provider (see
    :class:`SgpWithinGameRho`). Returns the object rather than a closure so the
    live wiring can ``bind_marginals`` after the lifecycle exists; it satisfies
    ``WithinGameRhoProvider`` by being callable."""
    return SgpWithinGameRho(params, marginals)


def _provider_signature_guard(provider: SgpWithinGameRho) -> WithinGameRhoProvider:
    """Type-checker-only proof that ``SgpWithinGameRho`` still satisfies the
    ``WithinGameRhoProvider`` contract ``build_book_model`` calls it through. If
    the class's ``__call__`` ever drifts from ``(str, str) -> band | None`` this
    fails at type-check time instead of at the first live book-risk recompute."""
    return provider
