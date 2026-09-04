"""The ONE candidate-edge function every EV the bot judges derives from
(2026-09-04 fee-seam repair, item 3 of the repair design).

    edge_cc = (side_fair − bid) · contracts − fee_cc

YES side: side_fair = fair. NO side: the combo settles on the COMPLEMENT, so
side_fair = $1 − fair — but ONLY when ``combo_no_pays_complement`` is
verified True; unverified ⇒ None (the NO payout is UNKNOWN and is never an
assumed complement, defense #5 / hard rule 6).

``fee_cc`` is the fee THIS fill pays (the measured schedule at the bid, or
the exchange-reported fee on a recovery replay). None = no fee model wired
or the fee is UNKNOWN: the gross edge is returned — the pre-2026-09-04
figure — so a rig without a fee model is bit-identical; an UNKNOWN fee type
never reaches a quote in the first place (construct_quote no-quotes it).

Pure integer arithmetic; the lifecycle wraps it with fee resolution so the
fill ledger's ``expected_edge_cc``, the confirm-time admission EV, the
quote-time candidate EV / eviction key and the KILL-marginal input are the
SAME number to the cent — the fee enters exactly once, here.
"""

from __future__ import annotations

from combomaker.core.conventions import Side
from combomaker.core.money import CC_PER_DOLLAR


def gross_edge_cc(
    *,
    fair_cc: int,
    bid_cc: int,
    qty_centi: int,
    our_side: Side,
    complement_verified: bool | None,
) -> int | None:
    """(side_fair − bid) · contracts, int cc — the fee-blind figure."""
    if our_side is Side.YES:
        return (int(fair_cc) - int(bid_cc)) * int(qty_centi) // 100
    if complement_verified:
        side_fair = CC_PER_DOLLAR - int(fair_cc)
        return (side_fair - int(bid_cc)) * int(qty_centi) // 100
    return None


def candidate_edge_cc(
    *,
    fair_cc: int,
    bid_cc: int,
    qty_centi: int,
    our_side: Side,
    complement_verified: bool | None,
    fee_cc: int | None,
) -> int | None:
    """Fee-NET candidate edge (see module docstring)."""
    gross = gross_edge_cc(
        fair_cc=fair_cc,
        bid_cc=bid_cc,
        qty_centi=qty_centi,
        our_side=our_side,
        complement_verified=complement_verified,
    )
    if gross is None:
        return None
    if fee_cc is None:
        return gross
    return gross - int(fee_cc)
