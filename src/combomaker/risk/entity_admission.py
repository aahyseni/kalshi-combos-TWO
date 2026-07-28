"""TIERED ENTITY LOAD — the operator's widening/admission spec, built literally.

    "Risk engine = protection not limitation. Those risk caps should be
     protecting us as intended, right now they're limiting us."
                                                    — operator, 2026-07-28

THE SPEC (docs/reports/2026-07-27-widening-spec-operator.md, restated
2026-07-28). Judge a combo on its SPECIFIC LEGS and DOLLAR AMOUNTS — never on
the worst leg alone, never on the whole family:

    per-entity load, as a PERCENT OF BANKROLL
      < 1%  no action
      1-2%  tier 1 widen
      2-3%  tier 2 widen
      > 3%  DECLINE

and then, NET ACROSS THE COMBO: "it should be judged based on other legs as
well, if they diversify further or concentrate other legs we have."

THE THREE PERCENTAGES ARE THE ONLY CONSTANTS IN THIS FILE. The 3% decline line
is NOT one of them — it is ``RiskLimits.entity_loss_frac``, already ratified,
and it is passed IN so this module can never disagree with the wall the caller
enforces. 1% and 2% are NEW POLICY ANCHORS (NORTH STAR layer 2 — operator risk
appetite stated once, not knobs). Everything else here is derived from the live
bankroll and the live book.

WHY THE PREVIOUS ATTEMPT WAS DELETED, so it cannot come back. Its
``certify_entity_admission`` NEVER READ THE KEY: a 1-leg $40 ticket dumping
100% of its premium onto the ALREADY-HOTTEST key returned
``certified=True / 'diversifying'``, BYTE-IDENTICAL to $40 spread across four
fresh entities, because the whole rule reduced to ``L < 2T/(N_eff-1)`` — a pure
ticket-SIZE test wearing a diversification label. It also relocated per-key
protection to the 5% per-combo ceiling for EVERY key. Both disqualifying. This
module reads, for every key the candidate touches, that key's OWN prior dollars
and that key's OWN candidate contribution, and its decision changes when either
moves. ``tests/test_admission_fixes.py`` pins exactly that.

THE DESIGN NOTE THE BRIEF ASKED FOR — why a tier sidesteps the share trap.
An honest per-key SHARE test admits nothing, ever: ``(E+L)/(T+L) > E/T``
whenever ``E < T``, so every ticket touching a key raises that key's honest
share. That is why the deleted ``net_effect.py`` inverted and admitted 0 of 140
sweeps. The tier is NOT a share: it is the entity's own load measured against
an ABSOLUTE percent of bankroll, so a cool key stays cool no matter how the
book's total moves, and the test has a fixed point to bite on.

THE SIZE EVENT vs THE ACCUMULATION EVENT — the decision the brief asked us to
make and justify. Measured on the live tape:

  * **77.9%** of ``skip_entity_loss_cap`` refusals are candidates whose OWN
    premium alone exceeds the $70.95 wall — no accumulation at all.
  * **54.8%** of breach-key instances sit on keys with **ZERO** prior dollars.
  * Median ticket SENT $9.19; median ticket REFUSED $127.05; largest ticket ever
    sent $69.27 against a $70.95 wall.

A first-ever structure on an EMPTY key is not "every combo riding one player one
way across all games" — that is the accumulation the 3% wall was ratified to
stop. It is a SIZE event, and SIZE already has two ratified protections the
operator explicitly kept unchanged: the per-COMBO wall (5%) and
``skip_size_above_max``. So the tiers split the two cases at the point where
they actually differ — the key's PRIOR load:

    prior load >= 1% (tier 1 or worse)  -> ACCUMULATION. The 3% wall refuses,
                                           byte-identical to today. No
                                           certificate can waive it.
    prior load  < 1% (cool)             -> SIZE. The entity axis abstains and
                                           the per-COMBO wall governs, exactly
                                           as the brief directs, and ONLY when
                                           the combo is NET DIVERSIFYING in
                                           dollars.

STATED LOUDLY, NEVER SILENTLY (the previous build's sin was that it was silent):
a certified SIZE admission lets ONE structure carry a key up to the per-COMBO
wall. Across structures nothing changes — the moment a key is at or above 1% it
is warm, and the 3% accumulation wall is the only thing that speaks. The
resulting invariant, which the suite pins:

    an entity may exceed the 3% ACCUMULATION wall only by the footprint of a
    SINGLE structure, never above what one structure is allowed to carry
    (per-combo), and once it is above 1% no further structure may be added
    beyond 3%.

FAIL-CLOSED (hard rule 6 / quiet-failure defense #2). Non-positive bankroll, a
non-positive decline fraction, no candidate dollars, a key the candidate does
not touch, or any arithmetic doubt ⇒ ``certified=False`` with a verdict naming
the outcome. UNKNOWN is never a convenient default; the wall then refuses.

SCOPE. Nothing here raises a limit. It produces (a) a tier per key, (b) a
dollar-weighted NET widen weight for the combo, and (c) a certificate the entity
cap may honour. Every portfolio SCALE protection — per-combo, per-game, slate,
det-max, ruin, CVaR/KILL-tail, the halts — is untouched by anything in this
file, and the per-COMBO cap is byte-identical in every flag state.

Money is integer centi-cents and every comparison is exact integer or Fraction
arithmetic (hard rule 5). The floats in this module are REPORTING ONLY and no
decision reads one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

__all__ = [
    "ENTITY_TIER1_FRAC",
    "ENTITY_TIER2_FRAC",
    "TIER_1",
    "TIER_2",
    "TIER_DECLINE",
    "TIER_NONE",
    "EntityAdmissionCertificate",
    "EntityLoad",
    "TicketConcentration",
    "certify_entity_admission",
    "combo_widen_weight",
    "effective_n",
    "entity_loads",
    "entity_tier",
    "ticket_concentration",
    "tier_widen_weight",
]

# --- THE ONLY NEW CONSTANTS IN THIS BUILD -------------------------------------
# POLICY ANCHORS (NORTH STAR layer 2): the operator's risk appetite on the entity
# axis, stated once. The third percentage — the 3% DECLINE line — is deliberately
# NOT here: it is ``RiskLimits.entity_loss_frac``, already ratified and already
# enforced, and every function below takes it as an argument so this module can
# never drift from the wall the caller binds on.
ENTITY_TIER1_FRAC = Fraction(1, 100)
ENTITY_TIER2_FRAC = Fraction(2, 100)

TIER_NONE = 0
"""< 1% of bankroll on this entity — no action."""
TIER_1 = 1
"""1-2% of bankroll — widen, tier 1."""
TIER_2 = 2
"""2-3% of bankroll — widen, tier 2."""
TIER_DECLINE = 3
"""> the ratified decline line (``entity_loss_frac``, 3%) — DECLINE."""


def _thr(frac: Fraction, bankroll_cc: int) -> int:
    """A %-of-bankroll threshold in integer cc — the SAME integer-exact form
    ``limits.threshold_cc`` uses, duplicated here only so this module stays
    import-free of the checker (``limits`` imports THIS file)."""
    return frac.numerator * bankroll_cc // frac.denominator


def entity_tier(load_cc: int, bankroll_cc: int, decline_frac: Fraction) -> int:
    """The operator's tier for ONE entity's load. Exact integers, no float.

    Boundaries are ``>`` on the lower edge, matching the wall the checker
    already enforces (``loss_cc > entity_thr`` is the breach), so ``TIER_DECLINE``
    is true on EXACTLY the loads the 3% wall refuses today — not one cc apart.

    A non-positive bankroll cannot support a percent-of-bankroll tier: fail
    closed to ``TIER_DECLINE`` (the caller's %-cap layer has already refused for
    unusable bankroll, so this can only ever agree with it, never soften it).
    """
    if bankroll_cc <= 0 or decline_frac <= 0:
        return TIER_DECLINE
    if load_cc > _thr(decline_frac, bankroll_cc):
        return TIER_DECLINE
    if load_cc > _thr(ENTITY_TIER2_FRAC, bankroll_cc):
        return TIER_2
    if load_cc > _thr(ENTITY_TIER1_FRAC, bankroll_cc):
        return TIER_1
    return TIER_NONE


def tier_widen_weight(
    load_cc: int, bankroll_cc: int, decline_frac: Fraction
) -> Fraction:
    """The GRADED widen weight for one entity's load — 0 below tier 1.

    "the response is GRADED instead of a cliff at 3%" (the widening spec). The
    weight is the load's own position against the ratified wall,

        w = min(load, wall) / wall      (exact Fraction; clamped at 1)

    switched ON by the tier: ``TIER_NONE`` (< 1%) ⇒ exactly 0, "no action". So
    tier 1 (1-2%) always lands in ``[1/3, 2/3]`` and tier 2 (2-3%) always lands
    in ``(2/3, 1]`` on the ratified 3% line — measurably different by
    construction, monotone within each tier, and containing NO number of its own:
    the shape is entirely (load, bankroll, the ratified wall).

    THE LOAD THAT FEEDS IT IS THE **PRIOR** LOAD (``EntityLoad.prior_cc``) — the
    dollars ALREADY riding that entity, never the candidate's own size. That is
    the whole point of the axis: "widen only when a SPECIFIC LEG is over its own
    limit". A fresh pitcher nobody has bet weighs EXACTLY ZERO no matter how big
    the incoming ticket is, which is what stops the axis pricing us out of the
    other ~21 props on a 24-prop slate. Ticket SIZE is already priced by the
    frozen markup tiers and bounded by the per-COMBO wall; it must not be charged
    twice.

    It is a WEIGHT, not centi-cents: the caller multiplies it by whatever widen
    budget it already owns, so this module can never widen a quote by itself.
    """
    if bankroll_cc <= 0 or decline_frac <= 0:
        return Fraction(0)
    if entity_tier(load_cc, bankroll_cc, decline_frac) == TIER_NONE:
        return Fraction(0)
    wall = _thr(decline_frac, bankroll_cc)
    if wall <= 0:
        return Fraction(0)
    return Fraction(min(int(load_cc), wall), wall)


@dataclass(frozen=True, slots=True)
class EntityLoad:
    """ONE entity key's dollar state under a candidate — the atom of the spec.

    ``prior_cc`` is what the key already carries from OTHER structures (committed
    book + any other in-flight reservation); ``add_cc`` is what THIS candidate
    batch puts on it; ``post_cc`` is the accumulated number the wall binds on and
    is exactly ``snapshot.loss_by_entity_cc[key]``.

    ``prior_tier`` decides ACCUMULATION vs SIZE and drives ``widen_weight`` (we
    widen for concentration we ALREADY have, never for the size of the ticket in
    front of us — that is markup's and per-combo's job). ``post_tier`` is the
    load we would be CARRYING after the fill: reporting, and the tier the wall
    itself binds on.
    """

    key: str
    prior_cc: int
    add_cc: int
    post_cc: int
    prior_tier: int
    post_tier: int
    widen_weight: Fraction


def entity_loads(
    *,
    candidate_legs_by_position: Iterable[tuple[Iterable[str], int]],
    post_by_key: Mapping[str, int],
    bankroll_cc: int,
    decline_frac: Fraction,
) -> tuple[EntityLoad, ...]:
    """Decompose the candidate's entity footprint into prior / add / post.

    ``candidate_legs_by_position`` is ``(entity keys of the position, its
    max_loss_cc)`` for every candidate/reservation in this check — the whole
    in-flight batch, deliberately, exactly as the accumulated wall reads it.
    ``post_by_key`` is the checker's own ``snapshot.loss_by_entity_cc`` (committed
    + reserved + candidates), so ``post`` here is never a second computation of
    the number the cap refuses on — it IS that number, and ``prior`` is derived
    from it by subtraction. A key can therefore never disagree with the wall.

    An AND-bound ticket loses its whole premium or nothing, so a position
    contributes its FULL ``max_loss_cc`` to EVERY entity key it touches — the
    same attribution ``exposure.loss_by_entity_cc`` makes, and the one the wall
    binds on. Duplicate keys within one position count once.

    Pure: no clock, no config, no I/O.
    """
    add: dict[str, int] = {}
    for keys, loss_cc in candidate_legs_by_position:
        loss = int(loss_cc)
        if loss <= 0:
            continue
        for key in set(keys):
            add[key] = add.get(key, 0) + loss
    out: list[EntityLoad] = []
    for key in sorted(add):
        add_cc = add[key]
        post_cc = int(post_by_key.get(key, add_cc))
        prior_cc = max(0, post_cc - add_cc)
        out.append(
            EntityLoad(
                key=key,
                prior_cc=prior_cc,
                add_cc=add_cc,
                post_cc=post_cc,
                prior_tier=entity_tier(prior_cc, bankroll_cc, decline_frac),
                post_tier=entity_tier(post_cc, bankroll_cc, decline_frac),
                widen_weight=tier_widen_weight(
                    prior_cc, bankroll_cc, decline_frac
                ),
            )
        )
    return tuple(out)


def combo_widen_weight(loads: Sequence[EntityLoad]) -> Fraction:
    """THE NET-ACROSS-THE-COMBO widen — dollar-weighted, never the worst leg.

    Operator: "it should be judged based on other legs as well, if they diversify
    further or concentrate other legs we have." So the combo's widen is the
    DOLLAR-WEIGHTED MEAN of its per-key weights,

        Σ add_cc(k)·w(k) / Σ add_cc(k)

    not ``max(w)``. A combo carrying one leg at tier 1 plus four fresh legs is
    net diversifying and prices like it: on equal per-key dollars its weight is
    ``w_hot / 5``, a fifth of the pure-add reading. Exact Fraction; 0 on an empty
    or dollar-less set (nothing to widen for).
    """
    denom = sum(int(x.add_cc) for x in loads)
    if denom <= 0:
        return Fraction(0)
    return (
        sum((Fraction(int(x.add_cc)) * x.widen_weight for x in loads), Fraction(0))
        / denom
    )


# --- REPORTING-ONLY concentration measure --------------------------------------
# Kept because the operator's MEASURE for this build is "the change in
# dollar-weighted effective N if they had all filled" (admitting more must not
# lower it), and the tape replay + the shadow read-out print it. NO ADMISSION
# DECISION READS THESE — that was precisely the deleted build's defect.


@dataclass(frozen=True, slots=True)
class TicketConcentration:
    """The once-counted AND-BOUND-TICKET dollar state of a book. REPORTING."""

    total_cc: int
    sumsq: int
    peak_cc: int
    n_tickets: int

    @property
    def eff_n(self) -> float:
        return effective_n(self.total_cc, self.sumsq)


def effective_n(total_cc: int, sumsq: int) -> float:
    """T²/Σw² — the dollar-weighted effective number of LOSS EVENTS (0 if
    degenerate). 90 only if ninety tickets carry equal dollars, collapsing toward
    10 the moment ten of them carry the money. A float, deliberately: REPORTING
    ONLY."""
    if total_cc <= 0 or sumsq <= 0:
        return 0.0
    return (float(total_cc) * float(total_cc)) / float(sumsq)


def ticket_concentration(losses: Iterable[int]) -> TicketConcentration:
    """Enumerate a book's once-counted ticket concentration. One entry PER
    TICKET (``OpenPosition.max_loss_cc``) — never per leg, never per entity key
    (that attribution triple-counts: measured live $15,708,943cc attributed
    against $5,276,688cc of real premium, 2.98x). Non-positive entries skipped."""
    total = 0
    sumsq = 0
    peak = 0
    n = 0
    for value in losses:
        v = int(value)
        if v <= 0:
            continue
        total += v
        sumsq += v * v
        if v > peak:
            peak = v
        n += 1
    return TicketConcentration(
        total_cc=total, sumsq=sumsq, peak_cc=peak, n_tickets=n
    )


@dataclass(frozen=True, slots=True)
class EntityAdmissionCertificate:
    """The enumerated tiered state of ONE breaching key plus the verdict.

    ``key`` is the (family:entity x direction) key that breached the ratified
    wall. ``loads`` is the WHOLE candidate footprint — every key it touches, with
    that key's own prior and added dollars — so the certificate carries the
    evidence for "judged on its specific legs and dollar amounts" and a reader
    can check the decision without re-deriving anything.
    """

    key: str
    certified: bool
    verdict: str
    loads: tuple[EntityLoad, ...]
    diversifying_cc: int
    concentrating_cc: int
    widen_weight: Fraction
    ceiling_cc: int
    bankroll_cc: int

    @property
    def load(self) -> EntityLoad | None:
        """The breaching key's own load row (None if it is not in the batch)."""
        for x in self.loads:
            if x.key == self.key:
                return x
        return None

    @property
    def key_loss_cc(self) -> int:
        """The accumulated loss on the breaching key — the number the wall read."""
        row = self.load
        return 0 if row is None else row.post_cc

    @property
    def prior_cc(self) -> int:
        row = self.load
        return 0 if row is None else row.prior_cc

    @property
    def add_cc(self) -> int:
        row = self.load
        return 0 if row is None else row.add_cc

    @property
    def prior_tier(self) -> int:
        row = self.load
        return TIER_DECLINE if row is None else row.prior_tier

    @property
    def post_tier(self) -> int:
        row = self.load
        return TIER_DECLINE if row is None else row.post_tier

    @property
    def candidate_total_cc(self) -> int:
        """The candidate batch's dollars, counted ONCE PER TICKET — i.e. the
        distinct premium at risk, not the per-key attributed sum."""
        return self.diversifying_cc + self.concentrating_cc

    @property
    def widen_weight_pct(self) -> float:
        """REPORTING ONLY (the read-out prints a percent)."""
        return float(self.widen_weight) * 100.0

    @property
    def n_cool_keys(self) -> int:
        return sum(1 for x in self.loads if x.prior_tier == TIER_NONE)

    @property
    def n_loaded_keys(self) -> int:
        return sum(1 for x in self.loads if x.prior_tier != TIER_NONE)


def certify_entity_admission(
    *,
    key: str,
    loads: Sequence[EntityLoad],
    bankroll_cc: int,
    ceiling_cc: int,
) -> EntityAdmissionCertificate:
    """Certify (or refuse) ONE breaching entity key against the tiered spec.

    ``ceiling_cc`` is the LIVE per-COMBO wall, derived by the caller from the
    LIVE bankroll — the ONLY ceiling a certified SIZE event may reach, and an
    already-ratified anchor, so this introduces no number.

    THE RULE, in the operator's terms and in this order:

      1. **DECLINE what the wall is for.** If the key was ALREADY loaded before
         this candidate (``prior_tier >= 1``, i.e. >= 1% of bankroll), the breach
         is ACCUMULATION ACROSS STRUCTURES — exactly what the ratified 3% wall
         exists to refuse. Nothing certifies it. (This is also what keeps the
         per-entity wall at 3%: a warm key can never go past it.)
      2. **NET ACROSS THE COMBO.** The candidate's dollars must land more on COOL
         keys than on loaded ones — ``diversifying_cc > concentrating_cc``,
         measured in DOLLARS across ALL the combo's legs, never on the worst leg.
      3. **PER-COMBO STILL GOVERNS THE SIZE.** The key's accumulated load must
         fit ``ceiling_cc``.

    Every branch is fail-closed and names its outcome. Pure: no clock, no config,
    no I/O, no float in any decision path.
    """
    rows = tuple(loads)
    diversifying = sum(
        int(x.add_cc) for x in rows if x.prior_tier == TIER_NONE
    )
    concentrating = sum(
        int(x.add_cc) for x in rows if x.prior_tier != TIER_NONE
    )

    def cert(certified: bool, verdict: str) -> EntityAdmissionCertificate:
        return EntityAdmissionCertificate(
            key=key,
            certified=certified,
            verdict=verdict,
            loads=rows,
            diversifying_cc=diversifying,
            concentrating_cc=concentrating,
            widen_weight=combo_widen_weight(rows),
            ceiling_cc=int(ceiling_cc),
            bankroll_cc=int(bankroll_cc),
        )

    if bankroll_cc <= 0 or ceiling_cc <= 0:
        return cert(False, "degenerate_bankroll")
    row = next((x for x in rows if x.key == key), None)
    if row is None:
        # A certificate for a key this candidate does not touch never travels.
        return cert(False, "key_not_in_candidate")
    if row.add_cc <= 0:
        return cert(False, "no_candidate_dollars")

    # (1) ACCUMULATION — the case the 3% wall was ratified for. Never waived.
    if row.prior_tier == TIER_DECLINE:
        return cert(False, "accumulation_over_wall")
    if row.prior_tier == TIER_2:
        return cert(False, "accumulation_tier2")
    if row.prior_tier == TIER_1:
        return cert(False, "accumulation_tier1")

    # (2) NET ACROSS THE COMBO, in dollars, over ALL its legs.
    if diversifying <= concentrating:
        return cert(False, "net_concentrating")

    # (3) SIZE — per-combo governs, and only per-combo.
    if row.post_cc > int(ceiling_cc):
        return cert(False, "over_size_ceiling")

    return cert(True, "size_event_on_cool_key")
