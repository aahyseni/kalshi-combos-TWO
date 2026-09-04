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

2. ``exposure_backed`` — when no CRN value exists (cache cold/stale, steer
   disabled), the rebate may only carry components backed by HELD exposure
   they offset: the directional offset (skew.py requires a non-zero net in
   the opposite direction) and the peak-miss rebate (the cached committed
   peak profile) are; a LEG-AXIS rebate on a family/entity whose MIRROR
   direction the book does not hold is NOT — "diversifying into a family we
   hold nothing of" reduces no ES (the 8/12 ``KXMLBHR:no leg_diversifying
   −26cc`` on an empty cell) — so that axis's rebate component is removed.

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
    rule: str               # "none" | "es_value" | "exposure_backed"
    cap_cc: int | None      # the es_value cap when that rule applied
    unbacked_cc: int        # leg-axis rebate removed under exposure_backed


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
    unbacked = 0
    if leg_axis_armed:
        if family_cc < 0 and not _axis_backed(candidate_family_keys, shares_by_family):
            unbacked += -family_cc
        if entity_cc < 0 and not _axis_backed(candidate_entity_keys, shares_by_entity):
            unbacked += -entity_cc
    return RebateBound(max(0, rebate_cc - unbacked), "exposure_backed", None, unbacked)
