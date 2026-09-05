"""REBATE BOUND BY MEASURED VALUE (2026-09-04 build A item 2, last clause).

An inventory REBATE (a positive pricer-frame skew — it RAISES our NO bid to
win balancing flow) must be backed by risk it actually removes. Two rules,
in order of what is measured:

1. ``es_value`` — when the concentration steer's CRN cache has priced the
   candidate, ``value_cc_per_contract`` = CC_PER_DOLLAR·Cov(hit, book P&L)/W
   is the exact certainty-equivalent value of adding this ticket to the
   book (risk/concentration_steer.py — no free parameter). A rebate may not
   exceed that value; a candidate that reduces no ES (value <= 0) earns no
   rebate.

2. ``measured_floor`` — when no CRN value exists (cache cold/stale, steer
   disabled), the rebate passes through here UNCHANGED and is bounded at
   quote time by the MEASURED per-cell retained-edge floor
   (risk/retained_edge_floor.py, applied in pricing/quote.py: rebate <=
   margin − fee − the cell's shrunk measured adverse selection). The record
   of what each shape cost us is the bound, so losing shapes (rfi×rfi, HR
   baskets) still earn nothing beyond their measured loss.

   RETIRED 2026-09-04 night: the build-A ``exposure_backed`` rule (strip every
   leg-axis rebate on a family/entity whose mirror the book does not hold).
   On the 9/4 tape it removed the rebate from 77% of sends (0.3–0.5c on the
   wire, the margin auctions are won by) and contradicted the operator's
   diversity-via-pricing doctrine. ``unbacked_cc`` remains as telemetry.

Widening is never touched: the bound only ever LOWERS a rebate.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


def mirror_key(key: str) -> str | None:
    """``SERIES:yes`` ↔ ``SERIES:no`` (and ``SERIES:ENTITY:side``); None when
    the key carries no recognisable direction suffix."""
    head, sep, side = key.rpartition(":")
    if not sep:
        return None
    if side == "yes":
        return f"{head}:no"
    if side == "no":
        return f"{head}:yes"
    return None


def _axis_backed(keys: Iterable[str], shares: Mapping[str, float]) -> bool:
    for key in keys:
        mirror = mirror_key(key)
        if mirror is not None and shares.get(mirror, 0.0) > 0.0:
            return True
    return False


@dataclass(frozen=True, slots=True)
class RebateBound:
    rebate_cc: int          # the bounded pricer-frame rebate (>= 0)
    rule: str               # "none" | "es_value" | "measured_floor"
    cap_cc: int | None      # the es_value cap when that rule applied
    unbacked_cc: int        # TELEMETRY: what the RETIRED exposure_backed rule
                            # would have removed (never subtracted since 9/4)


def bound_rebate(
    rebate_cc: int,
    *,
    value_cc_per_contract: float | None,
    family_cc: int,
    entity_cc: int,
    candidate_family_keys: Iterable[str],
    candidate_entity_keys: Iterable[str],
    shares_by_family: Mapping[str, float],
    shares_by_entity: Mapping[str, float],
    leg_axis_armed: bool,
) -> RebateBound:
    """``rebate_cc`` is the pricer-frame skew about to be applied; <= 0 (a
    widen or nothing) passes through untouched. ``family_cc``/``entity_cc``
    are the leg-axis components in the CLASSIFIER frame (negative = a
    rebate contribution)."""
    if rebate_cc <= 0:
        return RebateBound(rebate_cc, "none", None, 0)
    if value_cc_per_contract is not None:
        cap = max(0, math.ceil(value_cc_per_contract))
        return RebateBound(min(rebate_cc, cap), "es_value", cap, 0)
    # RETIRED 2026-09-04 night (operator: "I'd also like to fill more
    # quantity, to have a more diverse book"): the exposure_backed rule that
    # removed every leg-axis rebate on a family/entity whose mirror the book
    # did not hold. Measured on the 9/4 tape it stripped the skew rebate from
    # 77% of sends (17,119 of 22,151; median 0.55c, 0.3-0.5c on the wire) —
    # the margin we win auctions by (field clears our fair +0.05-0.25c, 8/16)
    # — and it contradicted the diversity-via-pricing doctrine: the leg-axis
    # rebate exists precisely for families we do NOT hold. The ES argument
    # ("reduces no ES") is the concentration steer's job and applies only when
    # that steer has PRICED the candidate (rule 1). The rebate passes through
    # here and is bounded at quote time by the MEASURED per-cell retained-edge
    # floor (risk/retained_edge_floor.py via pricing/quote.py: rebate <=
    # margin - fee - measured adverse selection of the cell), so losing shapes
    # (rfi x rfi, HR baskets) still earn nothing beyond their measured loss.
    # ``unbacked`` is computed for telemetry only.
    unbacked = 0
    if leg_axis_armed:
        if family_cc < 0 and not _axis_backed(candidate_family_keys, shares_by_family):
            unbacked += -family_cc
        if entity_cc < 0 and not _axis_backed(candidate_entity_keys, shares_by_entity):
            unbacked += -entity_cc
    return RebateBound(rebate_cc, "measured_floor", None, unbacked)
