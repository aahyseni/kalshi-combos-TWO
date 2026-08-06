"""DNP scalar-settlement guard for SINGLE-PLAYER-DRIVEN combos (2026-08-06).

THE RULE (docs-first, fetched for the exact sniped tickers — see
docs/reports/2026-08-06-dnp-scalar-pickoff.md §2 and docs/dnp_scalar_settlement.md
§7/§7.1): a Kalshi player-prop leg whose player is scratched / not in the
starting lineup / starts with 0 PA "resolves to the fair market price" (a
SCALAR, not 0/1), and a multivariate collection multiplies scalar outcomes,
floor-rounded to the cent. So a combo whose legs ALL ride ONE player pays, on
that player's DNP, the INDEPENDENCE PRODUCT of the leg marks — the one state
of the world where the correlation credit we sell is marked to zero. Measured
cost: 3 Ketel Marte anti-correlated combos sniped for −$39.75, 3-for-3 on the
structure's offered volume (the era's only same-player scalar settlements).

TWO MECHANISMS, no hand-set numbers (operator ratification 2026-08-06,
verbatim: "I don't want to get picked off by full scalar ones"):

1. **Void-branch pricing** (expected value): fair = (1−h)·V_corr + h·⌊∏s⌋
   with h = the MEASURED per-family DNP hazard from our own settled-leg
   corpus (baseline table below; merged live with the session's settled-leg
   cache on the settlement cadence — never typed, refreshed by re-running
   ``tools/measure_dnp_hazard.py``). Applied only when the DNP value exceeds
   the correlated fair (Δ > 0): the correction may only ever RAISE our ask,
   mirroring the UNKNOWN→widen convention (quiet-failure defense #2).
2. **Sniper robustness floor** (the taker knows h = 1): the informed taker's
   payoff is Δ = ⌊∏s⌋ − V_corr, and no base-rate mixture stops an actor who
   KNOWS the scratch. ``construct_quote`` therefore floors the implied YES
   ask of an in-scope combo at ⌊∏s⌋ — the DNP settlement value — so a
   DNP-informed taker pays at least what the combo settles at: we either
   profit or don't lose on the scratch. The floor binds exactly when the
   normal ask (fair + margin) sits below ⌊∏s⌋, i.e. when Δ exceeds the
   quote's total margin — every small-Δ combo (24/24 of the era's normal
   same-player book) is untouched, byte-identically.

SCOPE (conservative, the measured pickoff class): combos where ONE player's
DNP voids EVERY leg at once — all legs are DNP-able single-named-player props
and every extractable player entity matches. UNKNOWN entity extraction on a
prop leg FAILS CLOSED into scope (widen side). Multi-player all-prop combos
(partial-scalar branches, mixed prop×game combos) are OUT OF SCOPE this
build — their DNP branch needs the mixed binary×scalar math; noted as the
follow-up in the 2026-08-06 build report with its own measured exposure.

Entity extraction reuses the leg-axis ticker convention (segment 2 of the
4-segment prop ticker = the player/entity code — the exact segment
``risk.exposure.leg_entity_key`` aggregates on and
``relationships._mlb_prop_entity`` parses; raw segments live-verified
2026-07-25). No new parser: any shape doubt ⇒ entity None ⇒ fail-closed.

Farmed logically-impossible combos never reach this module (engine prefix),
and need no floor: ``construct_farm_quote`` already sells YES at the
independence product of the selected sides — on a DNP scalar settlement the
farm taker pays ≥ ⌊∏s⌋ by construction.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from combomaker.core.money import CC_PER_DOLLAR
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.legtypes import LegType, classify_leg
from combomaker.rfq.models import RfqLeg

# Leg families that settle "fair market price" on a player DNP — the
# single-named-player stat props whose rules carry the clause, doc-verified:
# MLB HIT/HR/HRR/TB/KS + OUTS/RBI/SB (all 9 MLB contract PDFs audited
# 2026-07-10 — every prop is scalar-able; rules_secondary re-fetched for the
# sniped HIT/HRR/TB tickers 2026-08-06) and the soccer anytime-scorer clause
# (FRA v MAR rules text, 2026-07-09). Game-scoped families (moneyline, total,
# spread, RFI, corners, …) are NOT here: their scalar trigger is the 48-hour
# rain rule, not a single player's scratch, so a combo carrying one is not
# single-player-driven and stays out of scope.
DNP_SCALAR_FAMILIES: frozenset[LegType] = frozenset(
    {
        LegType.PLAYER_HIT,
        LegType.PLAYER_HR,
        LegType.PLAYER_HRR,
        LegType.PLAYER_TB,
        LegType.PLAYER_KS,
        LegType.PLAYER_OUTS,
        LegType.PLAYER_RBI,
        LegType.PLAYER_SB,
        LegType.PLAYER_GOAL,
    }
)


@dataclass(frozen=True, slots=True)
class DnpScalarScope:
    """A combo the guard covers: single-player-driven, all legs DNP-able.

    ``entity`` is the shared player/entity ticker segment, or None when at
    least one leg's entity could not be extracted (UNKNOWN ⇒ fail-closed into
    scope). ``product`` is the independence product of the SELECTED leg
    marginals (the DNP settlement value before the cent floor);
    ``floor_cc`` is ⌊product⌋ on the cent grid in centi-cents — the value the
    exchange pays the combo YES on the all-scalar branch."""

    entity: str | None
    families: frozenset[LegType]
    product: float
    floor_cc: int


def _prop_entity(market_ticker: str) -> str | None:
    """The player/entity segment of a DNP-able prop ticker, or None on any
    shape doubt (fail-closed — the caller treats None as same-player). Mirrors
    the leg-axis convention: segment 2 of the 4-segment MLB prop ticker
    (``KXMLBHIT-26AUG052140SDAZ-AZKMARTE4-1`` → ``AZKMARTE4``); see
    ``risk.exposure.leg_entity_key`` / ``relationships._mlb_prop_entity``."""
    parts = market_ticker.upper().split("-")
    if len(parts) != 4 or not parts[2]:
        return None
    return parts[2]


def floor_product_cc(product: float) -> int:
    """The combo's DNP settlement value on the cent grid, in centi-cents:
    scalar outcomes are "multiplied (rounded down)" per the collection rules.
    The tiny epsilon only ever pushes a float-representation knife-edge UP a
    cent — the conservative (ask-raising) direction."""
    if product <= 0.0:
        return 0
    if product >= 1.0:
        return CC_PER_DOLLAR
    return int(math.floor(product * 100.0 + 1e-9)) * 100


def single_player_scope(
    legs: Iterable[RfqLeg],
    beliefs: Iterable[LegBelief],
    sides: Iterable[str],
) -> DnpScalarScope | None:
    """Detect a single-player-driven combo and compute its DNP branch value.

    Returns None (out of scope — quote byte-identically unchanged) when any
    leg is not a DNP-able prop family, or when two legs carry DIFFERENT known
    player entities (multi-player: the all-scalar-at-once branch does not
    exist for one scratch; partial-scalar math is the noted follow-up).
    UNKNOWN entity extraction never exits scope — fail closed, widen side."""
    legs = list(legs)
    beliefs = list(beliefs)
    sides = list(sides)
    if not legs:
        return None
    families: set[LegType] = set()
    entities: set[str] = set()
    unknown_entity = False
    for leg in legs:
        family = classify_leg(leg.market_ticker)
        if family not in DNP_SCALAR_FAMILIES:
            return None
        families.add(family)
        entity = _prop_entity(leg.market_ticker)
        if entity is None:
            unknown_entity = True
        else:
            entities.add(entity)
    if len(entities) > 1:
        return None  # distinct KNOWN players — multi-player, out of scope
    product = 1.0
    for belief, side in zip(beliefs, sides, strict=True):
        product *= belief.p if side == "yes" else 1.0 - belief.p
    entity = None if unknown_entity or not entities else next(iter(entities))
    return DnpScalarScope(
        entity=entity,
        families=frozenset(families),
        product=product,
        floor_cc=floor_product_cc(product),
    )


# ------------------------------------------------------------------ hazards

# BASELINE MEASURED DNP LEG-RESULT COUNTS — family → (scalar_n, finalized_n).
# MEASURED, never typed: generated by ``tools/measure_dnp_hazard.py`` from our
# own settled-leg corpus (exchange-truth `GET /markets` graded results for
# every leg of every settled live-era combo; 2026-08-06 pull, 992 settlements
# → 997 finalized single-player prop legs, 23 scalar = pooled 2.31%; the
# forensics' 978/2.35% excludes the OUTS rows). Refresh by RE-RUNNING the
# tool on a fresh corpus pull — never by editing numbers (same convention as
# conditionals_mlb.SAME_PLAYER_CONDITIONALS). At runtime the engine MERGES
# the live session's settled-leg cache on top (settlement cadence), so the
# hazard keeps adapting between regenerations.
BASELINE_DNP_LEG_RESULTS: Mapping[str, tuple[int, int]] = {
    # family (LegType.value) : (scalar results, finalized results)
    "player_hit": (6, 214),
    "player_hr": (3, 67),
    "player_hrr": (6, 288),
    "player_ks": (7, 383),
    "player_outs": (0, 19),
    "player_rbi": (0, 10),
    "player_sb": (0, 2),
    "player_tb": (1, 14),
}


@dataclass(frozen=True, slots=True)
class DnpHazards:
    """Per-family DNP hazard rates derived from settled-leg COUNTS.

    Derivation rules (no tuned constants):
    - pooled rate = Σ scalar / Σ finalized over every family present.
    - a family's OWN rate is used only when the family is not THIN: its
      sample must be at least ``1 / pooled`` finalized legs — the size at
      which one scalar is expected at the pooled rate, so observing zero (or
      few) is informative. A THIN family FAILS CLOSED to
      ``max(own rate, pooled)``: never below pooled on a sample too small to
      trust, and never below its own observed rate either (the widen side —
      underestimating h under-prices the void branch).
    - a combo spanning several families takes the MAX over its families —
      the widen side (the hazard is a player-level event; per-family rates
      differ by population, and underestimating h under-prices the void
      branch).
    Returns None when the corpus has no scalar observations at all — the
    caller then skips the mixture (the robustness FLOOR needs no h and stays
    armed regardless, so a missing corpus is never a loss channel)."""

    counts: Mapping[str, tuple[int, int]]

    def pooled(self) -> float | None:
        scalar = sum(s for s, _ in self.counts.values())
        total = sum(t for _, t in self.counts.values())
        if total <= 0 or scalar <= 0:
            return None
        return scalar / total

    def family_rate(self, family: str) -> float | None:
        """The family's own rate when its sample clears the thinness bar; a
        THIN family fails closed to max(own, pooled); None when pooled is
        unknown."""
        pooled = self.pooled()
        if pooled is None:
            return None
        cell = self.counts.get(family)
        if cell is None:
            return pooled
        scalar, total = cell
        if total <= 0:
            return pooled
        own = scalar / total
        if total < 1.0 / pooled:
            return max(own, pooled)
        return own

    def hazard_for(self, families: Iterable[LegType | str]) -> float | None:
        rates: list[float] = []
        for family in families:
            name = family.value if isinstance(family, LegType) else family
            rate = self.family_rate(name)
            if rate is not None:
                rates.append(rate)
        return max(rates) if rates else None

    def merged(self, live: Mapping[str, tuple[int, int]]) -> DnpHazards:
        """Baseline + live-session counts, summed per family. The live counts
        come from the settled-leg cache (exchange graded results observed this
        session) — strictly additive evidence on the same measurement."""
        merged = {k: v for k, v in self.counts.items()}
        for family, (scalar, total) in live.items():
            base_scalar, base_total = merged.get(family, (0, 0))
            merged[family] = (base_scalar + scalar, base_total + total)
        return DnpHazards(counts=merged)


def baseline_hazards() -> DnpHazards:
    return DnpHazards(counts=BASELINE_DNP_LEG_RESULTS)


def counts_from_outcomes(
    outcomes: Iterable[tuple[str, str]],
) -> dict[str, tuple[int, int]]:
    """(ticker, result) pairs → per-family (scalar_n, finalized_n) counts.
    ``result`` is the exchange grade: "yes"/"no" (binary) or "scalar". Tickers
    outside the DNP-able prop families are ignored. Used by the lifecycle's
    settlement-cadence refresh over the settled-leg cache."""
    counts: dict[str, tuple[int, int]] = {}
    for ticker, result in outcomes:
        if result not in ("yes", "no", "scalar"):
            continue
        family = classify_leg(ticker)
        if family not in DNP_SCALAR_FAMILIES:
            continue
        scalar, total = counts.get(family.value, (0, 0))
        counts[family.value] = (
            scalar + (1 if result == "scalar" else 0),
            total + 1,
        )
    return counts
