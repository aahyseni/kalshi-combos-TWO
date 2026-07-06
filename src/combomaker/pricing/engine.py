"""Pricing engine: RFQ → priced quote (or a reasoned refusal).

The full top-down pipeline, hot-path safe (in-memory state only — peeks, never
fetches):

  legs → beliefs (Kalshi books; external sources blend in when configured)
       → relationship classification (UNKNOWN/IMPOSSIBLE ⇒ no-quote)
       → copula joint with priced uncertainty
       → quote construction (fees, width, free-money caps, grid)

Sizing note: for target-cost RFQs the exchange's cost→contracts conversion is
UNVERIFIED (Phase 2.5 list); the estimate here feeds only the size-width adder
— never money math — and is deliberately rounded UP (more size ⇒ more width).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

from combomaker.core.conventions import Conventions
from combomaker.core.money import CC_PER_DOLLAR
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.config import PricingConfig
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate, price_joint_matrices
from combomaker.pricing.legs import KalshiBookSource, LegBelief, OddsSource, blend_beliefs
from combomaker.pricing.quote import (
    ConstructedQuote,
    NoQuote,
    QuoteParams,
    construct_quote,
    free_money_caps,
)
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import Rfq


class PricingEngine:
    def __init__(
        self,
        feed: OrderbookFeed,
        metadata: MetadataCache,
        conventions: Conventions,
        config: PricingConfig,
        *,
        extra_sources: list[tuple[OddsSource, float]] | None = None,
    ) -> None:
        self._feed = feed
        self._metadata = metadata
        self._config = config
        self._book_source = KalshiBookSource(feed)
        # External providers (devig quarantined inside their adapters) blended
        # against the Kalshi book at weight 1.0. A source returning None just
        # drops out; sources DISAGREEING beyond threshold is a no-quote.
        self._extra_sources = list(extra_sources or [])
        self._fee_model = FeeModel(
            FeeSchedule.from_strings(config.fee.taker_coef, config.fee.maker_coef),
            conventions,
        )
        self._fee_type = FeeType.parse(config.fee.default_fee_type)
        self._fee_multiplier = Fraction(Decimal(config.fee.default_multiplier))
        self._sgp_params = SgpParams(
            pair_rho=dict(config.correlation.pair_rho),
            default_rho=config.correlation.same_event_rho,
            cross_event_rho=config.correlation.cross_event_rho,
            typed_uncertainty=config.correlation.typed_rho_uncertainty,
            untyped_uncertainty=config.correlation.untyped_rho_uncertainty,
        )
        quote_fields = {
            k: v
            for k, v in config.quote.model_dump().items()
            if k
            not in (
                "longshot_fair_threshold",
                "longshot_min_rel_uncertainty",
                "favorite_leg_threshold",
                "favorite_width_multiplier",
            )
        }
        self._quote_params = QuoteParams(**quote_fields)
        self._archetype = config.quote

    def price(
        self,
        rfq: Rfq,
        *,
        time_to_close_s: float,
        in_play: bool = False,
        inventory_skew_cc: int = 0,
    ) -> ConstructedQuote | NoQuote:
        if not rfq.is_combo or not rfq.all_leg_sides_known:
            return NoQuote(ReasonCode.SKIP_CLASSIFIER_UNKNOWN, "not a well-formed combo")

        relationship = classify_legs(rfq.legs, self._metadata)
        if relationship.kind is RelationshipKind.IMPOSSIBLE:
            return NoQuote(ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "; ".join(relationship.notes))
        if relationship.kind is RelationshipKind.UNKNOWN:
            return NoQuote(ReasonCode.SKIP_CLASSIFIER_UNKNOWN, "; ".join(relationship.notes))

        beliefs: list[LegBelief] = []
        for leg in rfq.legs:
            book_belief = self._book_source.marginal(leg.market_ticker)
            if book_belief is None:
                return NoQuote(
                    ReasonCode.SKIP_PRICING_FAILED, f"no belief for leg {leg.market_ticker}"
                )
            weighted: list[tuple[LegBelief, float]] = [(book_belief, 1.0)]
            for source, weight in self._extra_sources:
                extra = source.marginal(leg.market_ticker)
                if extra is not None:
                    weighted.append((extra, weight))
            blended = blend_beliefs(
                weighted, max_disagreement=self._config.max_source_disagreement
            )
            if blended is None:
                return NoQuote(
                    ReasonCode.SKIP_SOURCES_DISAGREE,
                    f"sources disagree on {leg.market_ticker}: "
                    + ", ".join(f"{b.source}={b.p:.3f}" for b, _ in weighted),
                )
            beliefs.append(blended)
        sides = [leg.side for leg in rfq.legs]

        sgp = build_sgp_correlation(
            list(rfq.legs), relationship.same_event_groups, self._sgp_params
        )
        joint = price_joint_matrices(
            beliefs, sides, sgp.corr, sgp.corr_low, sgp.corr_high, extra_notes=sgp.notes
        )
        joint = self._apply_longshot_floor(joint)

        combo_meta = self._metadata.peek(rfq.market_ticker)
        if combo_meta is None or combo_meta.grid is None:
            return NoQuote(
                ReasonCode.SKIP_CLASSIFIER_UNKNOWN,
                f"no price grid for combo market {rfq.market_ticker}",
            )

        qty = self._resolve_qty(rfq, fair_prob=joint.p)
        if qty is None:
            return NoQuote(ReasonCode.SKIP_CLASSIFIER_UNKNOWN, "unresolvable RFQ size")

        leg_books = [self._feed.book(leg.market_ticker) for leg in rfq.legs]
        yes_cap, no_cap = free_money_caps(leg_books, sides)

        return construct_quote(
            joint=joint,
            n_legs=len(rfq.legs),
            qty=qty,
            grid=combo_meta.grid,
            fee_model=self._fee_model,
            fee_type=self._fee_type,
            fee_multiplier=self._fee_multiplier,
            time_to_close_s=time_to_close_s,
            in_play=in_play,
            yes_cap_cc=yes_cap,
            no_cap_cc=no_cap,
            inventory_skew_cc=inventory_skew_cc,
            width_multiplier=self._width_multiplier(beliefs, sides),
            params=self._quote_params,
        )

    def _apply_longshot_floor(self, joint: JointEstimate) -> JointEstimate:
        """Below the longshot threshold, absolute uncertainty must not shrink
        with P (the gradient does) — floor it relative to fair, protecting
        whoever ends up short the longshot side."""
        cfg = self._archetype
        if joint.p >= cfg.longshot_fair_threshold:
            return joint
        floor = joint.p * cfg.longshot_min_rel_uncertainty
        if joint.uncertainty >= floor:
            return joint
        return replace(
            joint,
            uncertainty=floor,
            notes=(*joint.notes, f"longshot uncertainty floor {floor:.4f}"),
        )

    def _width_multiplier(self, beliefs: list[LegBelief], sides: list[str]) -> float:
        """Favorites-stack tightening: every selected side comfortably likely
        ⇒ a well-estimated product and price-insensitive flow."""
        cfg = self._archetype
        if cfg.favorite_width_multiplier >= 1.0:
            return 1.0
        selected = [b.p if s == "yes" else 1.0 - b.p for b, s in zip(beliefs, sides, strict=True)]
        if all(p >= cfg.favorite_leg_threshold for p in selected):
            return cfg.favorite_width_multiplier
        return 1.0

    def _resolve_qty(self, rfq: Rfq, *, fair_prob: float) -> CentiContracts | None:
        if rfq.contracts is not None:
            return rfq.contracts
        if rfq.target_cost_cc is not None:
            # Width-sizing estimate only (see module docstring): assume the
            # accepted side costs at least fair×$1 per contract, round UP.
            denom = max(int(fair_prob * CC_PER_DOLLAR), 100)
            estimated = -(-int(rfq.target_cost_cc) * 100 // denom)
            return CentiContracts(estimated)
        return None
