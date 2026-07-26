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

from combomaker.core.money import CentiCents
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg
from combomaker.sim.book_model import WithinGameRhoProvider

# The synthetic pair rides in ONE event so build_sgp_correlation treats it as a
# same-event pair (the only case that gets a typed prior; cross-event pairs get
# cross_event_rho, which is the book model's own off-block default anyway).
_PAIR_EVENT = "WITHIN_GAME_RHO_PROBE"


def sgp_within_game_rho_provider(params: SgpParams) -> WithinGameRhoProvider:
    """A ``WithinGameRhoProvider`` backed by the shipped ``SgpParams``.

    Returns a function mapping ``(ticker_a, ticker_b)`` to the pricer's own
    ``(low, point, high)`` within-game rho band for that pair, computed by
    ``build_sgp_correlation`` on the two tickers as a 2-leg same-event group.
    Always returns a band (never None) for a distinct pair: an untyped/UNKNOWN
    pair resolves to build_sgp_correlation's flat fallback band, which is exactly
    the conservative widening the pricer applies — so the book-risk view is never
    blind to a pair the pricer would widen on. Identical tickers (degenerate
    self-pair) return None so the book model leaves the diagonal alone.
    """

    def provider(ticker_a: str, ticker_b: str) -> tuple[float, float, float] | None:
        if ticker_a == ticker_b:
            return None  # self-pair: no off-diagonal to fill
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
        sgp = build_sgp_correlation(legs, [[0, 1]], params, marginals=None)
        low = float(sgp.corr_low[0, 1])
        point = float(sgp.corr[0, 1])
        high = float(sgp.corr_high[0, 1])
        return (low, point, high)

    return provider
