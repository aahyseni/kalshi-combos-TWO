"""Quote construction: fair → width → two maker bids on the market grid.

Invariants (property-tested):
- Both prices are OUR BIDS: grid rounding is always DOWN (maker-favorable,
  quiet-failure defense #4). A rounded-away side becomes a decline ("0"),
  never a rounded-up bid.
- The quote never offers free money against currently executable leg prices:
  yes_bid ≤ min executable ask of the selected legs − margin (combo YES value
  is dominated by each leg); no_bid ≤ Σ executable asks of the opposite legs
  − margin (combo NO is dominated by the complement basket). Caps are
  computed at small size — top-of-book is the tightest, safest bound.
- yes_bid + no_bid ≤ $1 − min_capture, always.
- Fees (the MEASURED schedule — pricing/fee_observer.py) shape the quote in
  one of two modes (``fee_mode``, FeeConfig.mode, 2026-09-04 fee-seam repair):
  "width" subtracts the fee from each bid (the pre-existing arithmetic);
  "floor" keeps the bid at fair − margin but FLOORS the retained margin at
  the smallest margin whose fee-net edge at the quote's own quantity clears
  the confirm gate (> 0), and caps the inventory rebate at margin − floor,
  so a fill can never be won and then refused over its fee — every bid whose
  markup already clears the floor is byte-identical to the fee-blind price
  (see ``construct_quote``).
- Sell-parlays-only (``QuoteParams.sell_parlays_only``): force ``yes_bid = 0`` so
  we can only ever end up LONG NO (sell the parlay), never LONG YES (the fade
  side that accepting our yes_bid would hand us — settlement backtest −14¢/ct).
  The markup still rides on ``no_bid`` (implied YES ask = $1 − no_bid); declining
  a side is a supported one-sided quote
  (docs/reports/2026-07-08-combo-yes-no-side-mechanics.md).

Width components are explicit and logged: base + per-leg + model uncertainty
(legs + correlation, from JointEstimate) + size + time-to-event + in-play +
basket (DO-6: extra width on 8+-leg all-NO single-prop-family baskets, the
measured +25-35c/$1 overbid shape — the caller detects the shape, this module
adds the width). Inventory skew arrives from the risk engine (0 until Phase 4
wires it).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from combomaker.core.money import CC_PER_DOLLAR, CentiCents, cc_from_prob
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.orderbook import OrderbookMirror
from combomaker.pricing.fees import FeeModel, FeeType, FeeUnknownError
from combomaker.pricing.fit_challenge import StructuralFitRecord
from combomaker.pricing.joint import JointEstimate

_CAP_PROBE_QTY = CentiContracts(100)  # 1 contract: tightest executable bound


@dataclass(frozen=True, slots=True)
class QuoteParams:
    base_width_cc: int = 200
    per_leg_width_cc: int = 100
    leg_count_convexity: float = 1.0
    uncertainty_width_scale: float = 1.0     # × uncertainty(prob) × $1
    size_width_cc_per_100: int = 50          # per 100 contracts of RFQ size
    time_wide_threshold_s: float = 6 * 3600.0
    time_width_cc: int = 200                 # scaled up as close_time nears
    in_play_extra_cc: int = 800
    # DO-6 basket width adder: extra width when the caller flags an 8+-leg
    # all-NO single-prop-family basket (basket_extra_applies). Only ever
    # WIDENS; <= 0 disables.
    basket_width_extra_cc: int = 250
    min_capture_cc: int = 100                # required minimum spread capture
    free_money_margin_cc: int = 100
    # Fade defense: quote combos ONE-SIDED as a pure parlay seller. When True,
    # yes_bid is forced to 0 (we decline the YES side) so we can only ever be
    # long NO. Pricing math is otherwise unchanged; the sell markup rides on
    # no_bid. Default False keeps the primitive two-sided; prod/demo YAML sets it.
    sell_parlays_only: bool = False


@dataclass(frozen=True, slots=True)
class NoQuote:
    reason: ReasonCode
    detail: str
    # Structural route verdict when the decline happened AFTER a Dixon-Coles
    # inversion (e.g. the tie x total pickoff guard) — carried so the lifecycle
    # records the REJECT off the pricing path. None otherwise.
    fit: StructuralFitRecord | None = None


@dataclass(frozen=True, slots=True)
class ConstructedQuote:
    yes_bid_cc: CentiCents      # 0 = decline that side
    no_bid_cc: CentiCents
    fair_cc: CentiCents
    width_components_cc: dict[str, int]
    # True only for a FARMED impossible combo (construct_farm_quote): fair is 0,
    # yes_bid is 0, and the position must be watched by the settlement guard.
    farmed: bool = False
    # Structural route verdict copied from the JointEstimate (build 2026-09-04
    # item B): ACCEPT / CHALLENGE (priced structurally or hybrid) or REJECT
    # (priced on the copula fallback). None on every non-Dixon-Coles path.
    structural_fit: StructuralFitRecord | None = None

    @property
    def total_width_cc(self) -> int:
        return sum(self.width_components_cc.values())


def free_money_caps(
    leg_books: list[OrderbookMirror], sides: list[str]
) -> tuple[CentiCents | None, CentiCents | None]:
    """(yes_cap, no_cap) from executable leg prices; None ⇒ that side unquotable.

    yes cap: combo YES ≤ every selected leg ⇒ dominated by the cheapest
    executable selected-side ask. no cap: combo NO ≤ Σ complements.
    """
    selected_asks: list[int] = []
    complement_asks: list[int] = []
    for book, side in zip(leg_books, sides, strict=True):
        if not book.valid:
            return None, None
        opposite = "no" if side == "yes" else "yes"
        sel = book.executable_buy(side, _CAP_PROBE_QTY)
        comp = book.executable_buy(opposite, _CAP_PROBE_QTY)
        if sel is None or comp is None:
            return None, None
        selected_asks.append(int(sel.worst_price_cc))
        complement_asks.append(int(comp.worst_price_cc))
    yes_cap = min(selected_asks)
    no_cap = min(CC_PER_DOLLAR, sum(complement_asks))
    return CentiCents(yes_cap), CentiCents(no_cap)


def construct_quote(
    *,
    joint: JointEstimate,
    n_legs: int,
    qty: CentiContracts,
    grid: PriceGrid,
    fee_model: FeeModel,
    fee_type: FeeType,
    fee_multiplier: Fraction,
    time_to_close_s: float,
    in_play: bool,
    yes_cap_cc: CentiCents | None,
    no_cap_cc: CentiCents | None,
    inventory_skew_cc: int = 0,
    markup_cc: int = 0,
    width_multiplier: float = 1.0,
    basket_extra_applies: bool = False,
    dnp_ask_floor_cc: int | None = None,
    params: QuoteParams | None = None,
    fee_mode: str = "width",
    retained_floor_cc: int | None = None,
) -> ConstructedQuote | NoQuote:
    """``fee_mode`` (2026-09-04 fee-seam repair; FeeConfig.mode):

    * ``"width"`` — the fee at the plausible fill range is SUBTRACTED from
      each bid (the pre-existing arithmetic, byte-identical).
    * ``"floor"`` — the fee is NOT subtracted. Instead the retained margin is
      floored at ``m_min`` — the smallest margin whose fee-net edge at THIS
      quote's quantity clears the confirm gate's ``> 0`` predicate (see the
      derivation inline: ``m_min = ⌈(F + 1)·100/qty⌉`` with F the whole-fill
      fee at the fee-maximising price of the plausible bid range) — and the
      inventory REBATE may give back at most ``margin − m_min`` (replacing
      the fee-blind ``margin // 2`` cap when a measured cell floor is
      supplied, otherwise the tighter of the two). Every bid whose markup
      already clears ``m_min`` is BYTE-IDENTICAL to the fee-blind price at
      zero skew; only sub-fee tiers (the ML-parlay razor) move, to
      fair − m_min. Because F is taken over the widest plausible bid range,
      the floor can never under-cover the fee at the bid actually posted
      (property-tested: every floor bid's confirm edge is ≥ 1 cc).

    ``retained_floor_cc`` (item 2 — risk/retained_edge_floor.py): the
    combo's cell's MEASURED adverse-selection floor EXCLUDING the fee; the
    fee at the bid is added here, so the minimum retained margin after the
    rebate is ``fee + retained_floor_cc``. Only ever tightens the rebate.
    """
    if fee_mode not in ("floor", "width"):
        raise ValueError(f"fee_mode must be 'floor' or 'width', got {fee_mode!r}")
    p = params or QuoteParams()
    fair_cc = cc_from_prob(joint.p)

    width: dict[str, int] = {
        "base": p.base_width_cc,
        "legs": int(p.per_leg_width_cc * (n_legs**p.leg_count_convexity)),
        "uncertainty": int(joint.uncertainty * CC_PER_DOLLAR * p.uncertainty_width_scale),
        "size": p.size_width_cc_per_100 * int(qty) // 10_000,
    }
    if time_to_close_s < p.time_wide_threshold_s:
        closeness = 1.0 - max(0.0, time_to_close_s) / p.time_wide_threshold_s
        width["time"] = int(p.time_width_cc * closeness)
    if in_play:
        width["in_play"] = p.in_play_extra_cc
    if width_multiplier != 1.0:
        # Archetype adjustment (e.g. favorites-stack tightening). Never below
        # half the base spread — a multiplier is a tilt, not an override.
        scaled = max(int(sum(width.values()) * width_multiplier), p.base_width_cc // 2)
        width = {"scaled": scaled}
    if basket_extra_applies and p.basket_width_extra_cc > 0:
        # DO-6 (2026-07-10): 8+-leg all-NO single-prop-family baskets overbid
        # +25-35c/$1 on file. Added AFTER every normal width component (incl.
        # the archetype multiplier, which must never scale it away) and BEFORE
        # the maker-favorable snap below. Strictly additive under the > 0
        # guard: it can only WIDEN the quote, never tighten it; a tunable of 0
        # (or below) disables it.
        width["basket"] = p.basket_width_extra_cc
    half = sum(width.values()) // 2
    # Maker markup (profit margin over fair) from MarkupPolicy — the tier/sport
    # edge. The defensive width is the FLOOR (uncertainty protection); the markup
    # drives the margin when larger. margin == half when markup_cc == 0 (the
    # default), so an un-set markup is BIT-IDENTICAL to the pre-markup pricer.
    margin = max(half, markup_cc)

    # REBATE NEVER EXCEEDS THE EDGE (2026-07-26). The inventory steer's REBATE
    # direction (positive ``inventory_skew_cc`` — it RAISES ``no_bid``, making
    # the combo cheaper to attract balancing flow) is now economically
    # meaningful rather than the hundredths-of-a-cent it used to be, so it can
    # exceed the margin on a thin mains combo and quote us THROUGH fair. Such a
    # quote is not just thin — it is negative-EV, and the confirm-time
    # admission gate DECLINES negative-EV fills, so it would manufacture
    # exactly the won-then-reneged auctions the 2026-07-25 audit eliminated.
    # POST-REBATE EDGE FLOOR (2026-08-16, operator-ratified package —
    # tightened from the 2026-07-26 "whole edge" cap): the rebate may give
    # away at most HALF the margin, so every rebated quote retains >= half
    # its tier edge. Measured need: the whole-edge cap manufactured sub-1c
    # coinflip fills — 24% of post-restart premium landed at 0.84% retained
    # edge (16/94 fills compressed to <0.5c/ct, one $33.70 ticket at
    # 0.19c/ct), the exact composition that pins P(book) at ~0.45. Derived
    # from the live margin (a structural fraction, not a new knob); the
    # WIDEN direction is untouched — making a combo dearer is always safe —
    # and the free-money clamp still applies below.
    # In "floor" mode with a MEASURED cell floor supplied, that floor REPLACES
    # the hand fraction below (item 2); every other path keeps it.
    if fee_mode == "width" or retained_floor_cc is None:
        if inventory_skew_cc > margin // 2:
            inventory_skew_cc = margin // 2

    # The fill happens at the BID, not at fair, and the quadratic fee peaks at
    # $0.50 — computing it at fair under-charges the side whose bid sits
    # nearer $0.50. Take the max fee over the plausible fill range instead.
    def side_fee(side_fair_cc: int, probe_margin: int, fee_below_bid: int) -> int:
        fee_at_fair = int(
            fee_model.fee_per_contract_cc(
                price_cc=CentiCents(side_fair_cc), fee_type=fee_type, multiplier=fee_multiplier
            )
        )
        lower = max(0, side_fair_cc - probe_margin - abs(inventory_skew_cc) - fee_below_bid)
        nearest_to_peak = min(max(CC_PER_DOLLAR // 2, lower), side_fair_cc)
        fee_in_range = int(
            fee_model.fee_per_contract_cc(
                price_cc=CentiCents(nearest_to_peak), fee_type=fee_type, multiplier=fee_multiplier
            )
        )
        return max(fee_at_fair, fee_in_range)

    # "floor" (2026-09-04 review fix M3): the floor is DERIVED from the confirm
    # gate's own predicate at THIS quote's quantity. The gate admits a fill
    # only when candidate_edge_cc > 0, where
    #     edge = floor(retained · qty / 100) − trade_fee_cc(bid, qty)
    # (rfq/edge.py: gross edge floor-divided over centi-contracts; the fee is
    # the exchange's single ceiling over the WHOLE fill). A per-contract floor
    # (margin ≥ ⌈fee/contract⌉) left exactly-zero and −1 cc edges on 266 of
    # 7,500 razor probes (fair 2090 → bid 7850 × 1.00 ct → 0; fair 2316 → bid
    # 7620 × 1.20 ct → −1): won-then-declined auctions, the class the floor
    # exists to eliminate. So: with F = the whole-fill fee at the fee-
    # maximising price of the plausible bid range, the smallest retained
    # margin m with floor(m · qty / 100) ≥ F + 1 is
    #     m_min = ⌈(F + 1) · 100 / qty⌉
    # — the "+1" is the strict inequality of the gate, the ⌈⌉ and the 100/qty
    # are the two roundings; nothing here is a tuned number.
    def side_fee_total(side_fair_cc: int, probe_margin: int) -> int:
        lower = max(0, side_fair_cc - probe_margin - abs(inventory_skew_cc))
        nearest_to_peak = min(max(CC_PER_DOLLAR // 2, lower), side_fair_cc)
        fee_at_fair = int(
            fee_model.trade_fee_cc(
                price_cc=CentiCents(side_fair_cc), qty=qty, fee_type=fee_type,
                multiplier=fee_multiplier,
            )
        )
        fee_in_range = int(
            fee_model.trade_fee_cc(
                price_cc=CentiCents(nearest_to_peak), qty=qty, fee_type=fee_type,
                multiplier=fee_multiplier,
            )
        )
        return max(fee_at_fair, fee_in_range)

    def margin_floor_for(fee_total_cc: int) -> int:
        # A zero-size RFQ has no fill to floor: fall back to one contract so
        # the arithmetic stays defined (the gate never sees such a fill).
        q = max(1, int(qty))
        return -(-(fee_total_cc + 1) * 100 // q)  # ceil division

    try:
        fee_peak = int(
            fee_model.fee_per_contract_cc(
                price_cc=CentiCents(CC_PER_DOLLAR // 2),
                fee_type=fee_type,
                multiplier=fee_multiplier,
            )
        )
        if fee_mode == "floor":
            # The final margin is at most max(margin, m_min(F)) and F is at
            # most the whole-fill fee at the 50c peak, so probing
            # [side_fair − max(margin, m_min(F_peak)) − |skew|, side_fair]
            # bounds the fee at any bid we could post.
            fee_peak_total = int(
                fee_model.trade_fee_cc(
                    price_cc=CentiCents(CC_PER_DOLLAR // 2), qty=qty, fee_type=fee_type,
                    multiplier=fee_multiplier,
                )
            )
            probe_margin = max(margin, margin_floor_for(fee_peak_total))
            floor_yes = margin_floor_for(side_fee_total(int(fair_cc), probe_margin))
            floor_no = margin_floor_for(
                side_fee_total(CC_PER_DOLLAR - int(fair_cc), probe_margin)
            )
            fee_yes = fee_no = 0  # the fee is NOT subtracted from the bid
        else:
            # "width": today's probe — the bid sits a further fee (<= fee_peak)
            # below fair − margin; the per-contract fee is subtracted.
            fee_yes = side_fee(int(fair_cc), margin, fee_peak)
            fee_no = side_fee(CC_PER_DOLLAR - int(fair_cc), margin, fee_peak)
    except FeeUnknownError as exc:
        return NoQuote(ReasonCode.SKIP_CLASSIFIER_UNKNOWN, f"fee model: {exc}")

    if fee_mode == "floor":
        # FEE-AWARE RETAINED-MARGIN FLOOR (2026-09-04). The fee is a cost of
        # the fill, not a width: the bid stays at fair − margin, the margin
        # is floored at m_min (a tier below the fee cannot exist — the razor
        # dissolves into max(razor, measured fee)), and the rebate may never
        # give back what the fee then takes. With a measured cell floor
        # (item 2) the post-rebate margin is at least m_min + floor.
        fee_floor = floor_no if p.sell_parlays_only else max(floor_yes, floor_no)
        if margin < fee_floor:
            margin = fee_floor
        rebate_cap = margin - fee_floor
        if retained_floor_cc is not None:
            rebate_cap = min(rebate_cap, max(0, margin - fee_floor - retained_floor_cc))
        if inventory_skew_cc > rebate_cap:
            inventory_skew_cc = rebate_cap

    # Inventory skew: positive = we are long the joint event, so bid less for
    # YES and more for NO (attract the flow that flattens us).
    yes_raw = fair_cc - margin - fee_yes - inventory_skew_cc
    no_raw = (CC_PER_DOLLAR - fair_cc) - margin - fee_no + inventory_skew_cc

    # DNP SCALAR ASK FLOOR (2026-08-06, pricing/dnp_scalar.py): for a
    # single-player-driven combo the caller passes the DNP settlement value
    # ⌊∏s⌋ (centi-cents). The implied YES ask ($1 − no_bid) must never sit
    # BELOW it: a taker who KNOWS the player is scratched (h=1 — the measured
    # 3-for-3 pickoff class) would otherwise buy our YES under what the
    # exchange will pay it on the scratch. Clamping ``no_raw`` DOWN only ever
    # RAISES the ask (never lowers one — property-tested), and both later
    # clamps (free-money, grid snap-down) also only lower ``no_bid``, so the
    # floor survives them. Inactive (byte-identical arithmetic) whenever the
    # normal ask already clears the floor — i.e. whenever the DNP gap
    # Δ = ⌊∏s⌋ − fair is inside the margin — which is every normal combo.
    if dnp_ask_floor_cc is not None and dnp_ask_floor_cc > 0:
        no_raw = min(no_raw, CC_PER_DOLLAR - dnp_ask_floor_cc)

    clamped_free_money = False
    if yes_cap_cc is not None and yes_raw > yes_cap_cc - p.free_money_margin_cc:
        yes_raw = yes_cap_cc - p.free_money_margin_cc
        clamped_free_money = True
    if no_cap_cc is not None and no_raw > no_cap_cc - p.free_money_margin_cc:
        no_raw = no_cap_cc - p.free_money_margin_cc
        clamped_free_money = True
    if yes_cap_cc is None or no_cap_cc is None:
        return NoQuote(
            ReasonCode.SKIP_NO_FREE_MONEY_CHECK,
            "executable leg prices unavailable — cannot prove the quote is arb-free",
        )

    def snap(raw: int) -> CentiCents:
        if raw <= 0:
            return CentiCents(0)  # decline this side
        snapped = grid.snap_bid_down(CentiCents(min(raw, CC_PER_DOLLAR)))
        return CentiCents(0) if snapped is None else snapped

    # Sell-parlays-only: hard-decline the YES side. Nothing downstream (the
    # both-zero no-quote check, the capture check) lifts it back up. This builder
    # zeroes YES here; the engine boundary (PricingEngine._enforce_sell_only) is
    # the authoritative belt-and-suspenders across ALL quote builders.
    yes_bid = CentiCents(0) if p.sell_parlays_only else snap(yes_raw)
    no_bid = snap(no_raw)

    if yes_bid == 0 and no_bid == 0:
        return NoQuote(
            ReasonCode.SKIP_PRICING_FAILED,
            ("sell-only: no-bid rounded away" if p.sell_parlays_only else "both sides rounded away")
            + (" (free-money clamp)" if clamped_free_money else ""),
        )
    if yes_bid + no_bid > CC_PER_DOLLAR - p.min_capture_cc:
        # By construction this shouldn't happen; refuse rather than trust math
        # that just contradicted itself.
        return NoQuote(
            ReasonCode.SKIP_PRICING_FAILED,
            f"capture check failed: {yes_bid}+{no_bid} > {CC_PER_DOLLAR - p.min_capture_cc}",
        )
    return ConstructedQuote(
        yes_bid_cc=yes_bid,
        no_bid_cc=no_bid,
        fair_cc=fair_cc,
        width_components_cc=width,
    )


def construct_farm_quote(
    *,
    farm_ask_cc: CentiCents,
    n_legs: int,
    qty: CentiContracts,
    grid: PriceGrid,
    no_cap_cc: CentiCents | None,
    params: QuoteParams | None = None,
    size_cap: CentiContracts,
) -> ConstructedQuote | NoQuote:
    """One-sided quote that FARMS a logically-impossible combo.

    The combo's YES can never settle (a logical tautology), so its true fair is
    $0 and the ONLY safe structure is: never buy the worthless YES, only ever
    end up long the certain-NO side. Hard invariants (property-tested):

    - ``yes_bid_cc = 0`` for EVERY input — we can never go long the YES.
    - ``no_bid = grid.snap_bid_down($1 − farm_ask)`` (maker-favorable: rounding
      the NO bid DOWN means we pay LESS for the certain winner), bounded by the
      free-money ``no_cap`` exactly as ``construct_quote`` does.
    - ``fair_cc = 0`` (the true fair of an impossible combo).

    ``farm_ask_cc`` is the naive-independence value of the selected YES side
    (computed by the caller, strictly below every selected leg's marginal). If
    the ask is non-positive, the NO bid rounds away, the implied sell price
    rounds to 0, or ``size_cap`` is 0, we return a ``NoQuote`` — there is
    nothing to farm, never a degenerate quote.
    """
    p = params or QuoteParams()
    if int(size_cap) <= 0:
        return NoQuote(ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "farm size caps to 0 contracts")
    if int(farm_ask_cc) <= 0:
        # A worthless YES priced at 0 leaves nothing to sell; and we must NEVER
        # bid the YES ourselves, so there is no quote to make.
        return NoQuote(
            ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "farm ask rounds to 0 — nothing to sell"
        )
    if no_cap_cc is None:
        return NoQuote(
            ReasonCode.SKIP_NO_FREE_MONEY_CHECK,
            "executable leg prices unavailable — cannot prove the farm no-bid is arb-free",
        )

    # We offer YES at farm_ask ⇔ we BID NO at $1 − farm_ask, snapped DOWN.
    no_raw = CC_PER_DOLLAR - int(farm_ask_cc)
    if no_raw > int(no_cap_cc) - p.free_money_margin_cc:
        no_raw = int(no_cap_cc) - p.free_money_margin_cc
    if no_raw <= 0:
        return NoQuote(ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "farm no-bid non-positive after cap")
    snapped = grid.snap_bid_down(CentiCents(min(no_raw, CC_PER_DOLLAR)))
    no_bid = CentiCents(0) if snapped is None else snapped
    if int(no_bid) <= 0:
        return NoQuote(ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "farm no-bid rounds away")
    sell_price_cc = CC_PER_DOLLAR - int(no_bid)  # what the taker pays for YES
    if sell_price_cc <= 0:
        return NoQuote(ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE, "farm sell price rounds to 0")
    # width_components_cc holds ONLY cc values (total_width_cc sums them): the
    # premium we collect per farmed YES contract. Leg count / size cap are
    # audited by the engine's decision record, not smuggled into a money dict.
    _ = (n_legs, qty)  # part of the interface; not needed for the farm price
    return ConstructedQuote(
        yes_bid_cc=CentiCents(0),  # HARD INVARIANT: never long the worthless YES
        no_bid_cc=no_bid,
        fair_cc=CentiCents(0),     # true fair of an impossible combo
        width_components_cc={"farm_sell_price": sell_price_cc},
        farmed=True,
    )
