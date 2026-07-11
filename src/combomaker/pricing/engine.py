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
from combomaker.core.money import CC_PER_DOLLAR, CentiCents, cc_from_prob
from combomaker.core.quantity import CentiContracts, qty_from_contracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.config import PricingConfig
from combomaker.ops.logging import get_logger
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate, price_containment, price_joint_matrices
from combomaker.pricing.legs import KalshiBookSource, LegBelief, OddsSource, blend_beliefs
from combomaker.pricing.legtypes import LegType, classify_leg
from combomaker.pricing.quote import (
    ConstructedQuote,
    NoQuote,
    QuoteParams,
    construct_farm_quote,
    construct_quote,
    free_money_caps,
)
from combomaker.pricing.relationships import Relationship, RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.pricing.structural import StructuralPricer, structural_applicable
from combomaker.rfq.models import Rfq, RfqLeg

log = get_logger(__name__)

# DO-6 basket width adder (2026-07-10): the measured overbid shape is an
# 8-16-leg all-NO basket drawn from ONE single prop family — >= 8 legs, every
# leg NO-side, all legs the same family below. The width itself is the
# config-tunable QuoteParams.basket_width_extra_cc (0 disables).
_BASKET_MIN_LEGS = 8
_BASKET_PROP_FAMILIES = frozenset(
    {
        LegType.PLAYER_HR,
        LegType.PLAYER_HIT,
        LegType.PLAYER_TB,
        LegType.PLAYER_HRR,
        LegType.PLAYER_KS,
    }
)


def is_single_family_no_basket(legs: list[RfqLeg], sides: list[str]) -> bool:
    """True iff the combo is a DO-6 basket: >= 8 legs, every leg NO-side, and
    every leg classifies to the SAME single prop family (player_hr/player_hit/
    player_tb/player_hrr/player_ks). Any doubt (mixed families, a YES leg,
    UNKNOWN typing, short combo) is False — the adder then simply does not
    fire; it never replaces the normal width, only ever adds to it."""
    if len(legs) < _BASKET_MIN_LEGS:
        return False
    if any(side != "no" for side in sides):
        return False
    families = {classify_leg(leg.market_ticker) for leg in legs}
    return len(families) == 1 and next(iter(families)) in _BASKET_PROP_FAMILIES


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
            pair_uncertainty=dict(config.correlation.pair_rho_uncertainty),
            pair_rho_by_sport={
                sport: dict(table)
                for sport, table in config.correlation.pair_rho_by_sport.items()
            },
            oriented_curve={
                k: list(v) for k, v in config.correlation.oriented_curve.items()
            },
            oriented_curve_uncertainty=dict(
                config.correlation.oriented_curve_uncertainty
            ),
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
                # Farming knobs live on QuoteConfig (read via self._archetype),
                # not on QuoteParams — exclude or QuoteParams(**quote_fields)
                # rejects them.
                "farm_impossible_combos",
                "farm_markup",
                "farm_max_contracts",
            )
        }
        self._quote_params = QuoteParams(**quote_fields)
        self._archetype = config.quote
        if self._quote_params.sell_parlays_only and conventions.combo_no_pays_complement is None:
            # Sell-only makes EVERY fill a NO position, but the lifecycle declines
            # every NO-side confirm until this convention is verified (Phase 2.5
            # combo round-trip). The maker will quote but never fill — surface it.
            log.warning(
                "sell_parlays_only_inert_until_combo_no_verified",
                detail="sell-only quotes only the NO (seller) side; every NO-side "
                "confirm is declined until combo_no_pays_complement is verified.",
            )
        self._structural = (
            StructuralPricer(config.structural, config.margin_total, config.mlb_runs)
            if (
                config.structural.enabled
                or config.margin_total.enabled_sports
                or config.mlb_runs.enabled
            )
            else None
        )

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
            # A LOGICALLY-CERTAIN impossibility (relationship.farmable) can only
            # settle NO, so we FARM it — quote the certain-NO side — instead of
            # declining. Metadata-dependent impossibilities (mutual exclusion)
            # are NOT farmable and always fall through to the no-quote.
            if relationship.farmable and self._archetype.farm_impossible_combos:
                farmed = self._farm_impossible(rfq, relationship)
                if farmed is not None:
                    return self._enforce_sell_only(farmed)
            return NoQuote(ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "; ".join(relationship.notes))
        if relationship.kind is RelationshipKind.UNKNOWN:
            return NoQuote(ReasonCode.SKIP_CLASSIFIER_UNKNOWN, "; ".join(relationship.notes))

        beliefs = self._fetch_beliefs(rfq)
        if isinstance(beliefs, NoQuote):
            return beliefs
        sides = [leg.side for leg in rfq.legs]

        joint: JointEstimate | None = None
        fallback_note: str | None = None
        if (
            relationship.kind is RelationshipKind.CONTAINMENT
            and relationship.containment is not None
        ):
            # Logical containment (1H-BTTS ⟹ FT-BTTS): joint = P(subset), pinned
            # here so it never reaches the copula (which would price the pair at
            # a pairwise ρ and under-quote the certain part).
            joint = price_containment(beliefs, sides, relationship.containment)
        elif relationship.kind is RelationshipKind.CONTAINMENT:
            # CONTAINMENT/CONDITIONAL-IN-LARGER-COMBO collapse (2026-07-11):
            # the classifier recorded ≥1 containment and/or same-player
            # conditional pair inside a >2-leg combo (the shapes that used to
            # decline UNKNOWN "not modeled"). Same mechanical template as
            # nested bands — drop each implied superset leg / collapse each
            # window pair into a band super-leg / collapse each conditional
            # pair into its bare-path 2-leg joint — dispatched before
            # structural/copula. Any failure declines.
            collapsed = self._price_nested_bands(rfq, beliefs, sides, relationship)
            if isinstance(collapsed, NoQuote):
                return collapsed
            joint = collapsed
        elif relationship.kind is RelationshipKind.NESTED_BAND:
            # Nested-band arithmetic is EXACT (P(low) − P(high)); it must never
            # fall to the copula (flat-0.6 overprices live match-corner bands by
            # +1.8c to +6.6c of fair, 2026-07-09 prod mids). Collapse each band
            # pair into a super-leg, then price the reduced set as usual. Any
            # failure declines — never a copula guess on a band shape.
            banded = self._price_nested_bands(rfq, beliefs, sides, relationship)
            if isinstance(banded, NoQuote):
                return banded
            joint = banded
        elif self._structural is not None and structural_applicable(
            list(rfq.legs), relationship.same_event_groups
        ):
            joint, reason = self._structural.try_price(list(rfq.legs), beliefs, sides)
            if joint is None:
                fallback_note = f"structural fallback: {reason}"
        if joint is None:
            sgp = build_sgp_correlation(
                list(rfq.legs),
                relationship.same_event_groups,
                self._sgp_params,
                marginals=[b.p for b in beliefs],
            )
            notes = (*sgp.notes, fallback_note) if fallback_note else sgp.notes
            joint = price_joint_matrices(
                beliefs, sides, sgp.corr, sgp.corr_low, sgp.corr_high, extra_notes=notes
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

        quote = construct_quote(
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
            basket_extra_applies=is_single_family_no_basket(list(rfq.legs), sides),
            params=self._quote_params,
        )
        return self._enforce_sell_only(quote)

    def _enforce_sell_only(
        self, quote: ConstructedQuote | NoQuote
    ) -> ConstructedQuote | NoQuote:
        """Engine-boundary guarantee: in sell-only mode NO combo quote may carry a
        non-zero yes_bid, whichever builder produced it. Both builders already zero
        YES at construction; this is the single authoritative choke point that ALSO
        catches any future builder that forgets. A non-zero YES reaching here is a
        bug — correct it to 0 and log loudly (never silently ship long-YES)."""
        if (
            self._quote_params.sell_parlays_only
            and isinstance(quote, ConstructedQuote)
            and int(quote.yes_bid_cc) != 0
        ):
            log.warning("sell_only_yes_bid_forced_zero", was_yes_bid_cc=int(quote.yes_bid_cc))
            if int(quote.no_bid_cc) == 0:
                # Zeroing YES on a YES-only quote would leave an invalid both-zero
                # quote — decline cleanly instead of ever emitting (0, 0).
                return NoQuote(
                    ReasonCode.SKIP_PRICING_FAILED, "sell-only: only the YES side was quotable"
                )
            return replace(quote, yes_bid_cc=CentiCents(0))
        return quote

    def _fetch_beliefs(self, rfq: Rfq) -> list[LegBelief] | NoQuote:
        """Per-leg blended marginal beliefs (Kalshi book + configured external
        sources). Fail-closed: a missing book belief or sources disagreeing
        beyond threshold returns a NoQuote — never a hole in the price."""
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
        return beliefs

    def _farm_impossible(self, rfq: Rfq, relationship: Relationship) -> ConstructedQuote | None:
        """Quote a farmable-impossible combo by shorting the certain-NO side at
        the naive-INDEPENDENCE YES value × ``farm_markup``.

        Fail-closed: returns None (⇒ the caller keeps the ordinary
        SKIP_LOGICALLY_IMPOSSIBLE no-quote) whenever we cannot farm SAFELY —
        missing/absent leg beliefs or sources disagreeing (never farm blind),
        no combo grid, no free-money cap, unresolvable size, or a degenerate
        price. The farm quote's yes_bid is 0 by construction: we can only ever
        end up long the certain-NO side, never the worthless YES."""
        beliefs = self._fetch_beliefs(rfq)
        if isinstance(beliefs, NoQuote):
            return None  # no prices ⇒ never farm; fall back to the no-quote
        sides = [leg.side for leg in rfq.legs]
        # Naive independence of the SELECTED sides — the operator's chosen
        # anchor. An impossible combo's YES is dominated by each leg, so this
        # product is strictly below every selected-leg marginal (arb-free).
        farm_prob = self._archetype.farm_markup
        for belief, side in zip(beliefs, sides, strict=True):
            farm_prob *= belief.p if side == "yes" else 1.0 - belief.p
        farm_prob = min(max(farm_prob, 0.0), 1.0)
        # Round the worthless YES value DOWN: never overstate it, keep it strictly
        # below the leg marginals, and let a sub-tick value collapse to 0 (⇒
        # construct_farm_quote returns "nothing to farm").
        farm_ask_cc = cc_from_prob(farm_prob, rounding="down")

        combo_meta = self._metadata.peek(rfq.market_ticker)
        if combo_meta is None or combo_meta.grid is None:
            return None
        qty = self._resolve_qty(rfq, fair_prob=farm_prob)
        if qty is None:
            return None
        leg_books = [self._feed.book(leg.market_ticker) for leg in rfq.legs]
        _yes_cap, no_cap = free_money_caps(leg_books, sides)
        farm_cap = qty_from_contracts(self._archetype.farm_max_contracts)
        size_cap = CentiContracts(min(int(qty), int(farm_cap)))
        result = construct_farm_quote(
            farm_ask_cc=farm_ask_cc,
            n_legs=len(rfq.legs),
            qty=qty,
            grid=combo_meta.grid,
            no_cap_cc=no_cap,
            params=self._quote_params,
            size_cap=size_cap,
        )
        if isinstance(result, NoQuote):
            return None  # degenerate farm ⇒ ordinary no-quote
        log.info(
            "farm_impossible_combo",
            combo_ticker=rfq.market_ticker,
            farm_ask_cc=int(farm_ask_cc),
            no_bid_cc=int(result.no_bid_cc),
            size_cap_centi=int(size_cap),
            notes="; ".join(relationship.notes),
        )
        return result

    def _price_nested_bands(
        self,
        rfq: Rfq,
        beliefs: list[LegBelief],
        sides: list[str],
        relationship: Relationship,
    ) -> JointEstimate | NoQuote:
        """Collapse each nested-band pair (yes-LOW + no-HIGH rungs of one
        ladder) AND each containment window pair ({A no, B yes} of A ⟹ B —
        the same arithmetic P(B) − P(A), 2026-07-11 universal-window rule)
        into a SUPER-LEG whose marginal is the EXACT band probability, drop
        each containment pair's implied SUPERSET leg (CONTAINMENT collapse,
        2026-07-11), collapse each same-player CONDITIONAL pair into a
        super-leg whose p is the bare path's 2-leg conditional joint
        (WIRE-4), then price the reduced leg set with the ordinary SGP/copula
        machinery. NESTED_BAND relationships carry only ``bands`` (ladder
        rungs + windows); the N-leg CONTAINMENT relationship carries
        ``containments`` / ``conditionals`` (+ any window ``bands``) — one
        collapse engine serves both, structural deliberately skipped (its
        ticker-parse inversion would misread a super-leg's synthetic
        marginal).

        - A bare 2-leg band reduces to ONE leg: the joint IS the arithmetic,
          no ρ anywhere in the price.
        - The classifier guarantees every band's game holds ONLY its two legs
          among the kept legs, so a band super-leg only ever meets other legs
          CROSS-game (cross_event_rho), where representing it by its low
          (superset) leg is exact (band × same-game-neighbour shapes decline
          UNKNOWN upstream).
        - A containment pair's joint IS the subset leg's selected marginal
          (price_containment semantics), so the superset leg simply drops;
          the kept subset keeps its own belief and single-leg uncertainty.
        - A conditional pair's joint is computed EXACTLY like the bare 2-leg
          path (build_sgp_correlation on the pair + price_joint_matrices —
          the sgp implied-rho seam over SAME_PLAYER_CONDITIONALS, all four
          side mixes); the super-leg carries that joint as a YES-side
          marginal with the pair joint's uncertainty. The classifier
          guarantees the pair's game holds NO other kept leg (V2 refutation
          2026-07-11: a same-game neighbour would see the super-leg through
          the kept leg's YES-side rho, whose SIGN is wrong for NO-side
          mixes), so a conditional super-leg only ever meets other legs
          CROSS-game — re-checked below fail-closed.
        - Width: a band super-leg carries u_low + u_high (a difference's
          errors add — conservative linear sum, same convention as
          price_joint).
        - Fail-closed: P(low) ≤ P(high) means the leg books contradict the
          ladder ordering (stale/crossed data) ⇒ NoQuote, never a clamped-to-0
          fair whose sell-only NO bid would quote near $1 on bad data. A
          degenerate (≤ 0) conditional pair joint declines identically.
        - Inverted CONTAINMENT marginals (subset priced above superset) are
          NOT a decline: mirror price_containment's Fréchet clamp on the bare
          pair — cap the kept subset's selected marginal at the superset's.
        """
        if (
            not relationship.bands
            and not relationship.containments
            and not relationship.conditionals
        ):
            # Classifier bug guard: the kind without pairs must refuse, never
            # fall through to a copula guess.
            return NoQuote(
                ReasonCode.SKIP_PRICING_FAILED,
                "band/containment collapse kind without pairs",
            )
        band_p: dict[int, float] = {}
        band_u: dict[int, float] = {}
        dropped: set[int] = set()
        for low_i, high_i in relationship.bands:
            p_band = beliefs[low_i].p - beliefs[high_i].p
            if p_band <= 0.0:
                return NoQuote(
                    ReasonCode.SKIP_PRICING_FAILED,
                    f"nested band inverted: P({rfq.legs[low_i].market_ticker})="
                    f"{beliefs[low_i].p:.4f} <= P({rfq.legs[high_i].market_ticker})="
                    f"{beliefs[high_i].p:.4f}",
                )
            band_p[low_i] = p_band
            band_u[low_i] = beliefs[low_i].uncertainty + beliefs[high_i].uncertainty
            dropped.add(high_i)
        # Conditional super-legs (WIRE-4): the pair's 2-leg joint via the
        # EXACT bare-path machinery (same functions, same params, same-game
        # group) so the collapse can never drift from the bare 2-leg price.
        cond_p: dict[int, float] = {}
        cond_u: dict[int, float] = {}
        cond_notes: list[str] = []
        for keep_i, drop_i in relationship.conditionals:
            pair_beliefs = [beliefs[keep_i], beliefs[drop_i]]
            pair_sides = [sides[keep_i], sides[drop_i]]
            pair_sgp = build_sgp_correlation(
                [rfq.legs[keep_i], rfq.legs[drop_i]],
                ((0, 1),),
                self._sgp_params,
                marginals=[b.p for b in pair_beliefs],
            )
            pair_joint = price_joint_matrices(
                pair_beliefs,
                pair_sides,
                pair_sgp.corr,
                pair_sgp.corr_low,
                pair_sgp.corr_high,
            )
            if pair_joint.p <= 0.0:
                return NoQuote(
                    ReasonCode.SKIP_PRICING_FAILED,
                    "conditional super-leg degenerate: "
                    f"P({rfq.legs[keep_i].market_ticker} {pair_sides[0]} & "
                    f"{rfq.legs[drop_i].market_ticker} {pair_sides[1]}) = "
                    f"{pair_joint.p:.6f}",
                )
            cond_p[keep_i] = pair_joint.p
            cond_u[keep_i] = pair_joint.uncertainty
            dropped.add(drop_i)
            cond_notes.append(
                f"conditional super-leg: P({rfq.legs[keep_i].market_ticker} "
                f"{pair_sides[0]} & {rfq.legs[drop_i].market_ticker} "
                f"{pair_sides[1]}) = {pair_joint.p:.4f} (same-player table)"
            )
        # Containment drops ((); no-op on NESTED_BAND): the implied superset
        # leg's only trace is the Fréchet cap min(P_subset, P_superset) on the
        # kept subset's SELECTED marginal — the exact clamp price_containment
        # applies to the bare 2-leg pair.
        selected = [
            b.p if s == "yes" else 1.0 - b.p for b, s in zip(beliefs, sides, strict=True)
        ]
        sub_cap: dict[int, float] = {}
        cont_notes: list[str] = []
        for sub_i, sup_i in relationship.containments:
            dropped.add(sup_i)
            sub_cap[sub_i] = min(sub_cap.get(sub_i, 1.0), selected[sup_i])
            cont_notes.append(
                f"containment collapse: dropped {rfq.legs[sup_i].market_ticker} "
                f"(implied by {rfq.legs[sub_i].market_ticker})"
            )
        clamped: dict[int, float] = {}  # kept subset leg -> YES-space belief p
        for sub_i, cap in sub_cap.items():
            if sub_i in dropped or selected[sub_i] <= cap:
                continue
            clamped[sub_i] = cap if sides[sub_i] == "yes" else 1.0 - cap
            cont_notes.append(
                f"containment Fréchet clamp: P({rfq.legs[sub_i].market_ticker} "
                f"{sides[sub_i]})={selected[sub_i]:.4f} capped to superset "
                f"{cap:.4f}"
            )
        keep = [i for i in range(len(rfq.legs)) if i not in dropped]
        # Defensive mirror of the classifier's conditional isolation guard
        # (V2 refutation 2026-07-11; relationships._collapse_containments is
        # the authoritative decline — keep in sync, incl. the copies in
        # tools/backtests/{wc,mlb}_backtest.py): a conditional super-leg with
        # a same-game KEPT companion must never price here either.
        for keep_i, _drop_i in relationship.conditionals:
            for group in relationship.same_event_groups:
                if keep_i in group and any(
                    k in keep for k in group if k != keep_i
                ):
                    return NoQuote(
                        ReasonCode.SKIP_PRICING_FAILED,
                        "conditional super-leg game carries other kept legs: "
                        "conditional-vs-neighbour correlation sign unmodeled",
                    )
        remap = {old: new for new, old in enumerate(keep)}
        reduced_legs: list[RfqLeg] = [rfq.legs[i] for i in keep]

        def reduced_belief(i: int) -> LegBelief:
            if i in band_p:
                return replace(
                    beliefs[i],
                    p=band_p[i],
                    uncertainty=band_u[i],
                    source=f"{beliefs[i].source}+band",
                )
            if i in cond_p:
                return replace(
                    beliefs[i],
                    p=cond_p[i],
                    uncertainty=cond_u[i],
                    source=f"{beliefs[i].source}+conditional",
                )
            if i in clamped:
                return replace(
                    beliefs[i], p=clamped[i], source=f"{beliefs[i].source}+contained"
                )
            return beliefs[i]

        reduced_beliefs = [reduced_belief(i) for i in keep]
        reduced_sides = [
            "yes" if (i in band_p or i in cond_p) else sides[i] for i in keep
        ]
        reduced_groups = [
            g
            for g in (
                tuple(remap[i] for i in group if i in remap)
                for group in relationship.same_event_groups
            )
            if len(g) >= 2
        ]
        sgp = build_sgp_correlation(
            reduced_legs,
            reduced_groups,
            self._sgp_params,
            marginals=[b.p for b in reduced_beliefs],
        )
        band_notes = tuple(
            f"nested band exact: P({rfq.legs[low_i].market_ticker}) - "
            f"P({rfq.legs[high_i].market_ticker}) = {band_p[low_i]:.4f}"
            for low_i, high_i in relationship.bands
        )
        return price_joint_matrices(
            reduced_beliefs,
            reduced_sides,
            sgp.corr,
            sgp.corr_low,
            sgp.corr_high,
            extra_notes=(*sgp.notes, *band_notes, *cont_notes, *cond_notes),
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
