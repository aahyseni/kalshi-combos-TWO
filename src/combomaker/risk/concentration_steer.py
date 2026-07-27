"""LEVER #5 — the concentration steer, made ECONOMICALLY REAL (operator
directive 2026-07-27).

The pre-existing steer (``risk/skew.py``) was directionally CORRECT and
economically INERT. Measured on 300,000 live skew events:

    median |applied_cc|                     20cc   (5.6% of a 200cc margin)
    full concentrating -> diversifying swing 0.36-0.39c  (< one tier step)
    composed clamps [-300, +1200] hit          0 times
    steer below one 10cc tick, annihilated  32.25% of events

Three structural attenuators produced that, and this module removes all three:

(a) **4:1 CLAMP ASYMMETRY** — a 12c widen ceiling against a 3c rebate ceiling,
    so a diversifier could never be as much TIGHTER as a concentrator was
    WIDER. Here there is ONE symmetric ``half_cc``: the widen ceiling and the
    rebate ceiling are the same number by construction (:class:`SteerScale`).
(b) **THE 1/n_keys COUNT DEFICIT** — a COUNT basis, so the absent-key rebate
    decayed 42% (0.0250 -> 0.0145) in ONE session purely BECAUSE we
    diversified successfully: the steer ate its own success. The count basis
    is GONE. Concentration is now measured two ways, both in DOLLARS:
      * :func:`wall_load` — this key's premium dollars against THAT AXIS'S OWN
        ENFORCED WALL (the same threshold ``risk/limits.py`` refuses on). A
        wall does not move when a new key appears, so success cannot decay it.
      * :class:`LossEventBook` — the dollar-Herfindahl over AND-BOUND LOSS
        EVENTS. Standard error EXACTLY ZERO (no sampling at all).
(c) **need = 1 - p_book multiplying everything** — 43% strength and weakening
    as p_book rises. ``need`` is gone from the magnitude; p_book steering is
    what the SIGN already encodes.

AND-BINDING (measured, operator directive). A 3-leg ticket across 3 DIFFERENT
matches is ONE loss event, but the leg-wise steer scored it as THREE
diversification rebates. Held as one ticket the book's effective event count is
1.00; split into 3 tickets it is 2.99. So concentration is measured on the
TICKET: a position's premium is ONE atom that lands on the bucket formed by the
whole set of axis keys it requires (``requires-all`` = the AND-bound loss
event), never smeared across its legs. :func:`event_bucket`.

WHAT PRICES IT (measured, EV-orthogonal, from the CRN cache we already pay
for): ``Cov(candidate payoff, pre-existing book P&L)``.

    value_cc_per_contract = CC_PER_DOLLAR x Cov(hit, book_pnl_cc) / bankroll_cc

That is the exact log-utility / mean-variance certainty-equivalent adjustment
for adding a bet to an existing book — it has NO free parameter, it scales with
the bankroll, and it is EV-ORTHOGONAL BY CONSTRUCTION (a covariance, not a
mean). Measured: SE $0.378 = 0.0161 c/contract against an EV-controlled spread
of 1.1333 c/contract => SNR 70.4, matched-pair t = 8.4. It is a mean of a
product, so EVERY draw contributes.

WHAT DOES **NOT** PRICE IT: ``delta_p_book``. R^2 = 0.921 against candidate EV
(92% redundant with what the pricer already has), 42.2% of candidates
unresolvable at 3 sigma at n=20k, and 41.7% of EV-residual signs flip across
caches. It is not used here and must not be.

ORDERING (the operator invariant, enforced as a PROPERTY). The steer's SCORE is
a STRICTLY INCREASING function of the AND-bound dollar-Herfindahl marginal, and
the tick ladder gives any strictly-diversifying candidate AT LEAST one full tick
of rebate and any strictly-concentrating one AT LEAST one full tick of widen.
Therefore, for two otherwise-identical candidates, the one that LOWERS
dollar-weighted book concentration gets a STRICTLY tighter quote — at the
classifier, before the grid snap, and after it. :func:`assert_diversifier_tighter`
checks it at all three stages, with a ``NoQuote`` ranked as INFINITE width so a
refusal can never land on the diversifier.

SUB-TICK ANNIHILATION. 32.25% of the old steer fell below one grid tick and was
silently erased by ``snap_bid_down``. The steer is now TICK-DENOMINATED: it is
always an exact integer multiple of the combo's own grid step, so the snap
reproduces it exactly (a floor of ``x + k*step`` onto a uniform-step lattice is
the floor of ``x`` plus ``k*step``). :func:`tick_ladder`.

BUDGET NEUTRALITY (markups are FIXED — operator, binding). This reallocates
WITHIN the existing margin. The score is CENTERED on the live measured mean
score (:class:`SteerCenter` — an EWMA of real flow, measured state, not a
constant), so the mean ``applied_cc`` sits at ~0 and the average quote does not
widen. Centering is a common additive offset, so it preserves the strict
ordering above exactly.

PURE. No I/O, no clock, no network — same contract as ``risk/skew.py`` and
``risk/lastlook.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from combomaker.core.money import CC_PER_DOLLAR

if TYPE_CHECKING:  # keep this module import-light on the hot path
    from combomaker.risk.exposure import LegRef, OpenPosition

__all__ = [
    "CC_PER_CENT",
    "ConcentrationSteer",
    "CrnBookCache",
    "HhiMarginal",
    "LossEventBook",
    "SteerCenter",
    "SteerInputs",
    "SteerScale",
    "assert_diversifier_tighter",
    "axis_alignment",
    "build_loss_event_book",
    "compute_concentration_steer",
    "elasticity_half_cc",
    "event_bucket",
    "quote_rank_cc",
    "share_alignment",
    "tick_ladder",
    "wall_load",
]

# 1 cent = 100 centi-cents (core/money's unit relation, restated locally so the
# elasticity — measured in "fills lost per CENT" — converts without a magic 100).
CC_PER_CENT = CC_PER_DOLLAR // 100


# ---------------------------------------------------------------------------
# 1. AND-BOUND LOSS EVENTS — the dollar-Herfindahl with ZERO standard error
# ---------------------------------------------------------------------------


def event_bucket(legs: Sequence[LegRef], key_fn: Any) -> tuple[str, ...]:
    """The AND-BOUND loss-event bucket of one TICKET on one axis.

    A sell-only combo is ``requires-all``: we forfeit the premium only if EVERY
    leg resolves its stated side. That makes the whole ticket ONE loss event, so
    its premium is ONE atom keyed by the SET of axis keys it needs — never a
    per-leg smear. Sorted + de-duplicated so the key is order-independent
    (``{A,B,C}`` from any leg order is one bucket) and hashable.
    """
    return tuple(sorted({key_fn(leg) for leg in legs}))


def share_alignment(share: float, hhi: float) -> float:
    """The SCALE-FREE marginal concentration alignment, in [-1, +1].

    ``share`` is the fraction of the axis's committed dollars sitting on the key
    the candidate lands on; ``hhi`` is that axis's dollar-Herfindahl
    ``sum_k share_k^2``. Both are RATIOS, so BOTH are invariant when the whole
    book is scaled by any factor — that is the entire point (operator directive
    2026-07-27: "we shouldn't be widening all of our bets just because we hit a
    $ amount of positions, we only widen on bets that concentrate current
    directions").

    WHY ``share - hhi`` IS THE MARGINAL. For the dollar-Herfindahl
    ``H = sum_k (D_k/T)^2``, adding premium ``p`` to key ``b`` moves H by

        dH/dp = (2/T) * (share_b - H)      (the p -> 0 intensity limit)

    so ``share_b - H`` is EXACTLY the direction and shape of the candidate's
    effect on dollar-weighted concentration, with the ``2p/T`` SIZE factor —
    the only book-size-dependent piece — divided out. Landing on a key that
    already carries MORE than the book's dollar-weighted average share raises
    concentration (=> WIDEN, positive); landing on a below-average or brand-new
    key lowers it (=> REBATE, negative). A brand-new key reads ``-H``: the
    rebate for diversifying is exactly how concentrated we currently are.

    BUDGET NEUTRALITY IS STRUCTURAL, NOT TUNED. ``sum_k share_k * (share_k - H)
    = H - H = 0``: the DOLLAR-WEIGHTED mean alignment over the book is exactly
    zero, so this measure cannot be a blanket markup rise (or cut) — it can only
    REALLOCATE width between concentrating and diversifying flow. Markups are
    frozen; this respects that by construction.

    NO SECOND NORMALISATION. The raw difference already lives in [-1, +1] (both
    terms are shares), is STRICTLY INCREASING in ``share`` — the ordering
    invariant — and is CONTINUOUS EVERYWHERE, including the degenerate
    single-key book (``share = H = 1`` reads exactly 0: a one-key book cannot
    be concentrated further, and the limit approaching it from a two-key book
    agrees). Rescaling each side by its own reach would span the range more
    aggressively but puts a step at ``H = 1``; the SPAN is not this function's
    job — :class:`SteerCenter` standardises by the MEASURED dispersion of live
    scores, which is the adaptive way to get it.
    """
    if hhi <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, share - hhi))


def axis_alignment(shares: Mapping[str, float], keys: Iterable[str]) -> float:
    """:func:`share_alignment` for one AXIS: the FULLEST key the candidate leans
    on, against that axis's own dollar-Herfindahl.

    ``shares`` are the axis's committed dollar shares (they sum to 1 over the
    book, so the whole reading is scale-free). ``keys`` are the candidate's own
    keys on that axis; a sell-only ticket puts its premium at risk on EVERY key
    it requires, so the binding one is the fullest. An empty axis (no committed
    dollars) reads 0.0 — neutral, never a widen (quiet-failure defense #2).
    """
    if not shares:
        return 0.0
    hhi = math.fsum(s * s for s in shares.values() if s > 0.0)
    worst = 0.0
    for key in keys:
        s = shares.get(key, 0.0)
        if s > worst:
            worst = s
    return share_alignment(worst, hhi)


@dataclass(frozen=True, slots=True)
class HhiMarginal:
    """What adding one candidate ticket does to dollar-weighted concentration.

    ``n_pre`` / ``n_post`` are the EFFECTIVE NUMBER OF LOSS EVENTS — the inverse
    dollar-Herfindahl ``(sum d)^2 / sum d^2`` over AND-bound event buckets. The
    3-leg/3-match ticket held whole reads 1.00; split into 3 tickets it reads
    2.99 (the measured live pair). ``relative`` is ``(n_post - n_pre)/n_pre``:
    POSITIVE = the fill DIVERSIFIES the book, NEGATIVE = it concentrates it.
    It is kept for the operator readout ONLY.

    ``intensity`` is the SCALE-FREE reading — :func:`share_alignment` of the
    candidate's bucket against the loss-event Herfindahl — and it is what
    prices. ``relative`` cannot: it carries the ``2p/T`` size factor, so the
    SAME candidate reads a smaller and smaller marginal as the book grows
    (a book-size term, which is exactly what the operator ruled out).
    ``intensity`` is POSITIVE for a CONCENTRATOR (same convention as the leg /
    peak / P(book) axes).

    STANDARD ERROR EXACTLY ZERO — there is no sampling anywhere in it.

    ``score`` is ``-intensity`` — the DIVERSIFIER-POSITIVE convention this
    module's steer uses. It is STRICTLY DECREASING in ``intensity`` and zero
    exactly at zero, which is what makes the operator invariant a property
    rather than a hope.
    """

    n_pre: float
    n_post: float
    relative: float
    intensity: float = 0.0

    @property
    def score(self) -> float:
        return -self.intensity


@dataclass(frozen=True, slots=True)
class LossEventBook:
    """The committed book as AND-BOUND loss events, in PREMIUM DOLLARS.

    ``dollars_by_bucket`` maps an :func:`event_bucket` to the premium-at-risk
    (float cc) of every ticket that binds exactly that key set. ``s1``/``s2`` are
    the running ``sum d`` / ``sum d^2`` so :meth:`marginal` is O(1) — the whole
    reason this replaces the 7.16us heuristic at 1.47us.

    Rebuilt only when ``ExposureBook.position_generation`` moves (a fill or a
    settlement), so the quote path pays the O(1) marginal and never the rebuild.
    """

    dollars_by_bucket: Mapping[tuple[str, ...], float]
    s1: float
    s2: float

    @property
    def effective_events(self) -> float:
        """Inverse dollar-Herfindahl: the effective number of loss events."""
        if self.s2 <= 0.0:
            return 0.0
        return (self.s1 * self.s1) / self.s2

    def marginal(
        self, bucket: tuple[str, ...], premium_cc: float
    ) -> HhiMarginal:
        """O(1) effect of adding ``premium_cc`` to ``bucket``.

        An EMPTY book (no committed events) has no concentration to change, so
        the marginal is exactly 0 — neutral, never a free rebate for being the
        first ticket of the night."""
        if premium_cc <= 0.0 or self.s2 <= 0.0:
            return HhiMarginal(
                n_pre=self.effective_events,
                n_post=self.effective_events,
                relative=0.0,
            )
        d = float(self.dollars_by_bucket.get(bucket, 0.0))
        s1_post = self.s1 + premium_cc
        s2_post = self.s2 - d * d + (d + premium_cc) * (d + premium_cc)
        n_pre = (self.s1 * self.s1) / self.s2
        n_post = (s1_post * s1_post) / s2_post if s2_post > 0.0 else 0.0
        rel = (n_post - n_pre) / n_pre if n_pre > 0.0 else 0.0
        # THE SCALE-FREE READING (operator directive 2026-07-27). ``rel`` above
        # carries the 2p/T size factor: the SAME ticket against a book twice as
        # large reads half the marginal, so a book-size term leaks into price.
        # ``intensity`` divides it out — bucket dollar SHARE against the book's
        # dollar-Herfindahl, both pure ratios, both invariant when the whole
        # book is scaled. Same sign as ``-rel`` in the p -> 0 limit, so nothing
        # about the ORDERING changes; only the size contamination is removed.
        hhi = self.s2 / (self.s1 * self.s1) if self.s1 > 0.0 else 0.0
        share = d / self.s1 if self.s1 > 0.0 else 0.0
        return HhiMarginal(
            n_pre=n_pre,
            n_post=n_post,
            relative=rel,
            intensity=share_alignment(share, hhi),
        )


def build_loss_event_book(
    events: Iterable[tuple[tuple[str, ...], float]],
) -> LossEventBook:
    """Fold ``(bucket, premium_cc)`` pairs into a :class:`LossEventBook`.

    Two tickets binding the SAME key set MERGE into one atom: they are the same
    loss event family, and reading them as two events would be exactly the
    leg-wise error the AND-binding directive kills."""
    by_bucket: dict[tuple[str, ...], float] = {}
    for bucket, premium_cc in events:
        if premium_cc <= 0.0:
            continue
        by_bucket[bucket] = by_bucket.get(bucket, 0.0) + float(premium_cc)
    s1 = math.fsum(by_bucket.values())
    s2 = math.fsum(v * v for v in by_bucket.values())
    return LossEventBook(dollars_by_bucket=by_bucket, s1=s1, s2=s2)


# ---------------------------------------------------------------------------
# 2. WALL LOAD — dollar share against each axis's OWN ENFORCED wall
#    (the replacement for the 1/n_keys COUNT deficit, which is now gone)
# ---------------------------------------------------------------------------


def wall_load(
    dollars_by_key: Mapping[str, float],
    keys: Iterable[str],
    wall_cc: float,
    *,
    add_cc: float = 0.0,
) -> float:
    """How full is the FULLEST wall this candidate leans on, in [0, 1].

    ``wall_cc`` is that AXIS'S OWN ENFORCED threshold — the very number
    ``risk/limits.py`` refuses on (``threshold_cc(game_loss_frac, bankroll)``,
    ``entity_loss_frac``, ``per_combo_loss_frac``...). Not a count, not a share
    of the book, not a hand constant: a wall. It does NOT move when a new key
    appears, so diversifying successfully can never decay it (the measured 42%
    one-session decay of the count basis).

    ``add_cc`` folds the candidate's own premium in, so the load is the POST-fill
    reading — the honest "where would this leave us" number.

    A non-positive wall (unknown / unconfigured) returns 0.0: UNKNOWN never
    widens (quiet-failure defense #2).
    """
    if wall_cc <= 0.0:
        return 0.0
    worst = 0.0
    for key in keys:
        loaded = float(dollars_by_key.get(key, 0.0)) + add_cc
        worst = max(worst, loaded / wall_cc)
    return min(1.0, worst)


# ---------------------------------------------------------------------------
# 3. THE PRICE — Cov(candidate payoff, pre-existing book P&L) off the CRN cache
# ---------------------------------------------------------------------------


class _LegLike(Protocol):  # pragma: no cover - structural typing only
    """The two fields a leg must expose to be priced against the CRN sample.
    Read-only so a FROZEN ``LegRef`` satisfies it."""

    @property
    def market_ticker(self) -> str: ...

    @property
    def side(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CrnBookCache:
    """The already-paid-for CRN sample, kept so the steer costs no new MC.

    Published OFF the hot path from the same ``compute_book_risk`` run that
    produces P(book): the PRICING-joint (``corr_location_point``) leg-value
    matrix and the book's per-scenario P&L, both row-subsampled to ``m`` rows.
    Generation-stamped exactly like the peak / P(book) profiles — a stale cache
    is a hard NEUTRAL, never a guess.

    THE AXIS IS DELIBERATE. This is a LOCATION-axis object (it prices a quote),
    so it rides the PRICING joint, never the tail-dependence stress joint that
    the enforced gates ride (2026-07-26 axis split). Pricing on the collapsed
    stress joint would mark every fill adverse by construction.

    ``value_sd_cc`` is the MEASURED dispersion (cc/contract) of the value signal
    over the book's own tickets — the steer's derived half-range candidate. It
    is computed at publish time, off the hot path.
    """

    input_generation: int
    col_by_ticker: Mapping[str, int]
    leg_values: Any            # NDArray[float64], shape (m, n_legs)
    book_pnl: Any              # NDArray[float64], shape (m,) — float cc
    bankroll_cc: float
    value_sd_cc: float = 0.0

    def value_cc_per_contract(self, legs: Sequence[_LegLike]) -> float | None:
        """``CC_PER_DOLLAR x Cov(hit, book_pnl_cc) / bankroll_cc``, in cc.

        POSITIVE = the ticket pays into the states where the book already WINS
        and stays out of the way where it loses => a genuine diversifier => the
        steer REBATES. NEGATIVE = it doubles down on our bad states => WIDEN.

        Sign derivation (sell-only, long NO). Our per-contract P&L is
        ``-entry`` when the parlay HITS and ``+(1-entry)`` when it misses, i.e.
        ``const - CC_PER_DOLLAR*hit``. A constant has zero covariance, so
        ``Cov(payoff, book) = -CC_PER_DOLLAR*Cov(hit, book)`` and the
        certainty-equivalent adjustment ``-Cov(payoff, book)/W`` is
        ``+CC_PER_DOLLAR*Cov(hit, book)/W``. No free parameter anywhere: this is
        the log-utility/mean-variance CE term, and it scales with the bankroll
        automatically (North Star layer 3).

        Returns None — NEUTRAL, never a guess — when any leg is absent from the
        cached universe, the cache is empty, or the bankroll is unknown.
        """
        import numpy as np

        if self.bankroll_cc <= 0.0:
            return None
        pnl = self.book_pnl
        if pnl is None or getattr(pnl, "size", 0) < 2:
            return None
        hit: Any = None
        for leg in legs:
            col = self.col_by_ticker.get(leg.market_ticker)
            if col is None:
                return None  # unknown leg => neutral (UNKNOWN never widens)
            v = self.leg_values[:, col]
            leg_hit = v if leg.side == "yes" else (1.0 - v)
            hit = leg_hit if hit is None else hit * leg_hit
        if hit is None:
            return None
        cov = float(np.mean(hit * pnl) - np.mean(hit) * np.mean(pnl))
        return CC_PER_DOLLAR * cov / self.bankroll_cc


# ---------------------------------------------------------------------------
# 4. THE SCALE — derived from measured state, symmetric, never a constant clamp
# ---------------------------------------------------------------------------


def elasticity_half_cc(fill_elasticity_per_cent: float) -> int:
    """The VALIDITY HORIZON of the measured fill-rate elasticity, in cc.

    Measured (CMH-stratified): ~22% of fills are lost per CENT of extra width.
    A linear elasticity ``e`` predicts fills reaching zero at ``1/e`` cents, so
    the half-range at which its own linear extrapolation has burned HALF the
    flow is ``1/(2e)`` cents. Beyond that we would be quoting off a model that
    has left its measured range — so that is the horizon, and it is a
    CONSEQUENCE of the measurement, not a knob (e = 0.22 => 2.27c = 227cc).

    A non-positive / unmeasured elasticity returns 0: no measurement, no steer.
    """
    if fill_elasticity_per_cent <= 0.0:
        return 0
    return int(CC_PER_CENT / (2.0 * fill_elasticity_per_cent))


@dataclass(frozen=True, slots=True)
class SteerScale:
    """The ONE symmetric half-range. Widen ceiling == rebate ceiling, always.

    The 4:1 asymmetry (600cc widen vs 150cc rebate) is gone: there is a single
    number here and both directions use it, so a diversifier CAN be exactly as
    much tighter as a concentrator is wider.

    ``half_cc = min`` of three MEASURED bounds — whichever binds is recorded in
    ``binding`` for the operator readout:

      ``value``      the measured cc/contract dispersion of the covariance value
                     signal itself (:attr:`CrnBookCache.value_sd_cc`) — we never
                     move the price by more than the risk difference is worth;
      ``elasticity`` :func:`elasticity_half_cc` — the measured fill-rate model's
                     own validity horizon;
      ``margin``     the LIVE margin on THIS quote. Markups are FIXED (operator,
                     binding), so the steer may only REALLOCATE inside the width
                     that already exists — it can never add to it.
    """

    half_cc: int
    binding: str
    value_cc: int
    elasticity_cc: int
    margin_cc: int

    @classmethod
    def derive(
        cls, *, value_sd_cc: float, fill_elasticity_per_cent: float, margin_cc: int
    ) -> SteerScale:
        v = max(0, int(round(value_sd_cc)))
        e = elasticity_half_cc(fill_elasticity_per_cent)
        m = max(0, int(margin_cc))
        # An UNMEASURED bound does not bind — it abstains. A zero here means
        # "we have not measured this", not "the answer is zero"; treating it as
        # a bound would silently zero the whole steer whenever the CRN cache is
        # cold, which is exactly the inertness this lever exists to remove. The
        # MARGIN always binds: markups are FIXED, so zero room really is zero.
        candidates = [b for b in (v, e) if b > 0]
        candidates.append(m)
        half = min(candidates)
        if half == m:
            binding = "margin"
        elif half == v:
            binding = "value"
        else:
            binding = "elasticity"
        return cls(
            half_cc=half,
            binding=binding,
            value_cc=v,
            elasticity_cc=e,
            margin_cc=m,
        )


@dataclass(slots=True)
class SteerCenter:
    """The live score distribution — mean AND dispersion. Two jobs, one object.

    **Budget neutrality.** Markups are FIXED (operator, binding), so the steer
    must REALLOCATE and never widen. Subtracting the MEASURED mean score of
    real flow pins the mean ``applied_cc`` at ~0 with no hand-set offset, and
    it ADAPTS: a night of nothing but concentrators re-centres, so the steer
    keeps discriminating between them instead of saturating one-sided (which
    would be an un-ratified markup cut, or an un-ratified markup rise).

    **Economic reality.** Dividing by the MEASURED dispersion is what makes the
    ladder actually span. The raw Herfindahl marginal on a live book lives in a
    narrow band, so a fixed mapping put every candidate on the bottom rung —
    that is precisely the measured failure (a full concentrating->diversifying
    swing of 0.36-0.39c, smaller than one tier step, against a 200cc margin).
    Standardising maps a +-1 sigma move of the CONCENTRATION SIGNAL ITSELF onto
    the full +-``half_cc``, so the swing IS the measured value spread.

    Both operations are a strictly increasing affine map, so the strict
    ordering — and therefore the operator invariant — survives centring and
    scaling exactly.

    **Budget neutrality, CLOSED-LOOP (operator directive 2026-07-27).**
    Subtracting the mean SCORE is not enough to hold the mean OUTPUT at zero,
    because everything downstream of it (the bound, the tick ladder) is
    non-linear. Measured on the 41,056-event live replay: the score
    distribution is right-skewed (mean -0.0422 vs median -0.0473), so a
    hard clamp at +-1 sd truncated the diversifier tail and left a -0.141 mean
    on a signal whose mean was supposed to be 0 - a 15cc average WIDEN that
    nobody asked for, on frozen markups.

    So the bound is now ``tanh`` (SMOOTH, odd, bounded, strictly increasing -
    it re-shapes without discarding any mass) and :attr:`bias` is an INTEGRAL
    controller on the OUTPUT: every emitted value nudges it, so the loop
    settles exactly where the mean output is zero. It is self-correcting by
    construction (a large bias saturates ``tanh`` toward -1, which pushes the
    bias straight back), needs no clamp, and contains no hand-set number - the
    only parameter is the same ``half_life`` the moments already use. Measured
    effect on the replay: mean ``applied_cc`` -29.20 -> -13.40cc (TIGHTER), and
    the residual is exactly the untouched directional term.

    ``half_life`` is in observations: the EWMA horizon over which "the current
    flow mix" is defined. Warm-up uses the exact running moments so the first
    quotes of a session are not centred on a single observation.
    """

    mean: float = 0.0
    var: float = 0.0
    n: int = 0
    half_life: float = 256.0
    # OUTPUT-space integral centring. See the class docstring: the score-space
    # mean alone cannot hold the emitted mean at zero through a non-linear
    # bound + a tick lattice.
    bias: float = 0.0

    @property
    def sd(self) -> float:
        return math.sqrt(self.var) if self.var > 0.0 else 0.0

    def _alpha(self) -> float:
        return 1.0 - math.exp(-math.log(2.0) / max(1.0, self.half_life))

    def observe(self, score: float) -> None:
        self.n += 1
        w = max(self._alpha(), 1.0 / self.n)
        delta = score - self.mean
        self.mean += w * delta
        self.var = (1.0 - w) * (self.var + w * delta * delta)

    def centre(self, score: float) -> float:
        """Standardised score: ``(score - mean) / sd``, or the plain deviation
        while the dispersion is not yet measured (fail-safe: never divide by a
        number we have not seen)."""
        d = score - self.mean
        sd = self.sd
        return d / sd if sd > 0.0 else d

    def squash(self, score: float) -> float:
        """The bounded, budget-neutral score in (-1, +1).

        ``tanh(standardised - bias)``: strictly increasing in ``score`` (so the
        ordering invariant survives it exactly), smooth (no cliff anywhere), and
        de-meaned in OUTPUT space by the integral term."""
        return math.tanh(self.centre(score) - self.bias)

    def observe_output(self, squashed: float) -> None:
        """Close the budget-neutrality loop on what was actually emitted."""
        self.bias += self._alpha() * squashed


# ---------------------------------------------------------------------------
# 5. THE TICK LADDER — sub-tick annihilation is structurally impossible
# ---------------------------------------------------------------------------


def tick_ladder(score: float, half_cc: int, tick_cc: int) -> int:
    """Quantize a centred score in [-1, 1] onto the combo's OWN price lattice.

    Returns an exact integer multiple of ``tick_cc``, so ``snap_bid_down`` on a
    uniform-step range reproduces it EXACTLY instead of erasing it (the measured
    32.25% annihilation). ``rungs = half_cc // tick_cc`` is the ladder's
    resolution.

    THE MINIMUM RUNG IS THE INVARIANT. Any strictly positive score gets AT LEAST
    +1 tick and any strictly negative score AT MOST -1 tick, so a diversifier is
    strictly tighter than a concentrator even when both round to "almost
    nothing". Rounding can compress two same-signed candidates onto one rung —
    it can never invert or annihilate the sign.

    ``rungs == 0`` (the whole half-range is thinner than one tick) returns 0:
    the steer is genuinely unaffordable on this lattice and says so, rather than
    pretending to a precision the grid does not have.
    """
    if tick_cc <= 0 or half_cc < tick_cc:
        return 0
    rungs = half_cc // tick_cc
    if rungs <= 0 or score == 0.0:
        return 0
    mag = max(1, min(int(rungs), int(round(abs(score) * rungs))))
    return int(math.copysign(mag * tick_cc, score))


# ---------------------------------------------------------------------------
# 6. THE COMPONENT
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SteerInputs:
    """Everything the steer reads, all of it measured state.

    ``walls_by_axis`` maps an axis name to ``(dollars_by_key, keys_of_candidate,
    wall_cc)`` — each axis's OWN enforced threshold, never a shared constant.
    """

    loss_events: LossEventBook
    candidate_bucket: tuple[str, ...]
    candidate_premium_cc: float
    walls_by_axis: Mapping[str, tuple[Mapping[str, float], Sequence[str], float]]
    margin_cc: int
    tick_cc: int
    fill_elasticity_per_cent: float
    crn: CrnBookCache | None = None
    candidate_legs: Sequence[Any] = ()
    contracts: float = 0.0
    # The OTHER scale-free concentration axes (peak-state, P(book) tail share,
    # leg family x side, leg entity x side), each already a signed intensity in
    # [-1, +1] in the DIVERSIFIER-POSITIVE convention. They are folded in BEFORE
    # centring so the whole steer is centred, laddered and tick-denominated
    # exactly ONCE — one budget, one symmetric half-range, one grid snap.
    axis_scores: Sequence[float] = ()


@dataclass(frozen=True, slots=True)
class ConcentrationSteer:
    """The steer + full decomposition (every field is in the operator readout).

    ``skew_cc`` is the CLASSIFIER-convention value (>= 0 CONCENTRATING => widen)
    to stay sign-compatible with ``risk/skew.py``; ``applied_cc = -skew_cc`` is
    the pricer-frame number (positive RAISES ``no_bid`` => cheaper => tighter).
    """

    skew_cc: int
    score_raw: float
    score_centred: float
    hhi: HhiMarginal
    value_cc_per_contract: float | None
    wall_load_by_axis: Mapping[str, float] = field(default_factory=dict)
    scale: SteerScale | None = None
    reason: str = "ok"

    @property
    def applied_cc(self) -> int:
        return -self.skew_cc

    @property
    def diversifying(self) -> bool:
        return self.skew_cc < 0


def compute_concentration_steer(
    inputs: SteerInputs, centre: SteerCenter, *, observe: bool = True
) -> ConcentrationSteer:
    """The whole lever, in one pure function.

    ORDER OF OPERATIONS (each step is load-bearing):

    1. **AND-bound dollar-Herfindahl marginal** — zero SE, O(1). Its ``score``
       is a STRICTLY increasing function of the marginal, and it is the ONLY
       thing that sets the SIGN. That is what makes the operator invariant
       provable rather than empirical.
    2. **Wall load** — the fullest of the axes' OWN enforced walls, post-fill.
       It can only ever make a WIDEN more urgent (it multiplies the negative
       side), and it never gates the REBATE: a fill that offsets concentration
       is not worth less because the book happens to be small (the 2026-07-26
       finding that onset-gating the reward crushed live rebates to a median of
       0.02c). It also cannot flip a sign, so the ordering survives it.
    3. **Covariance value** — the measured, EV-orthogonal price. It scales the
       magnitude within [0, 1] and NEVER changes the sign, so the invariant is
       untouched by a noisy read; an unavailable read leaves magnitude 1.0
       (the structural Herfindahl reading alone), which is the zero-SE fallback.
    4. **Centring** — a common additive offset, so the average quote does not
       widen and the ordering is preserved exactly.
    5. **Tick ladder** — the result is a whole number of grid ticks.
    """
    hhi = inputs.loss_events.marginal(
        inputs.candidate_bucket, inputs.candidate_premium_cc
    )
    loads: dict[str, float] = {}
    for axis, (dollars, keys, wall_cc) in inputs.walls_by_axis.items():
        loads[axis] = wall_load(
            dollars, keys, wall_cc, add_cc=inputs.candidate_premium_cc
        )

    base = hhi.score
    # (2) WALL LOAD IS TELEMETRY ONLY (operator directive 2026-07-27). It used
    # to multiply the WIDEN side, and that was a BOOK-SIZE term wearing a risk
    # reason code: ``load = book dollars on this key / a fixed wall``, so a book
    # three times larger paid up to three times the widen for the IDENTICAL
    # candidate. The operator's rule is that only the candidate's own effect on
    # concentration may widen it, so the load stays in ``wall_load_by_axis`` for
    # the readout and the caps keep owning refusal — it no longer touches price.

    # (3) The measured price. Normalised by its own measured dispersion so it is
    # a unit-free magnitude in [0, 1]; sign stays with the Herfindahl.
    value_cc: float | None = None
    if inputs.crn is not None and inputs.candidate_legs:
        value_cc = inputs.crn.value_cc_per_contract(inputs.candidate_legs)
    magnitude = 1.0
    if (
        value_cc is not None
        and inputs.crn is not None
        and inputs.crn.value_sd_cc > 0.0
    ):
        magnitude = min(1.0, abs(value_cc) / inputs.crn.value_sd_cc)

    # (3b) ONE score over ALL the scale-free concentration axes. Every part is
    # already a signed intensity in [-1, +1] measured as a SHARE against a
    # HERFINDAHL (never dollars against a fixed wall, never a 1/n count), so the
    # unweighted mean is itself in [-1, +1] and is itself scale-free. The mean —
    # not the sum — is what keeps the composition independent of HOW MANY axes
    # happen to be armed.
    parts = [base * magnitude]
    parts.extend(float(s) for s in inputs.axis_scores)
    score_raw = max(-1.0, min(1.0, math.fsum(parts) / len(parts)))
    if observe:
        centre.observe(score_raw)
    score_centred = centre.squash(score_raw)
    if observe:
        centre.observe_output(score_centred)

    scale = SteerScale.derive(
        value_sd_cc=(inputs.crn.value_sd_cc if inputs.crn is not None else 0.0),
        fill_elasticity_per_cent=inputs.fill_elasticity_per_cent,
        margin_cc=inputs.margin_cc,
    )
    # SIGN: the score is POSITIVE for a diversifier, and a diversifier must be
    # quoted TIGHTER, i.e. NEGATIVE in the classifier convention (which negates
    # into a no_bid-RAISING positive at the pricer). One flip, right here.
    steer_cc = -tick_ladder(score_centred, scale.half_cc, inputs.tick_cc)
    reason = "ok"
    if scale.half_cc < inputs.tick_cc:
        reason = "sub_tick_half_range"
    elif steer_cc == 0:
        reason = "neutral"
    return ConcentrationSteer(
        skew_cc=steer_cc,
        score_raw=score_raw,
        score_centred=score_centred,
        hhi=hhi,
        value_cc_per_contract=value_cc,
        wall_load_by_axis=loads,
        scale=scale,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 7. THE OPERATOR INVARIANT, AS A CHECKABLE PROPERTY
# ---------------------------------------------------------------------------

# A refusal is INFINITE width: ranked below every real quote, so "the diversifier
# is never the one refused" is the same comparison as "the diversifier is
# tighter". Expressed as a no_bid of -inf (our bid on the NO side IS the price;
# a higher no_bid is a cheaper combo for the taker, i.e. a tighter quote).
NO_QUOTE_RANK = -math.inf


def quote_rank_cc(quote: Any) -> float:
    """TIGHTNESS rank of a quote result: higher = tighter.

    Sell-only, so the whole price is ``no_bid`` (the implied YES ask the
    requester sees is ``$1 - no_bid``). A ``NoQuote`` — or a quote whose NO side
    rounded away — ranks as INFINITE WIDTH, which is what makes "a refusal can
    never land on the diversifier" a consequence of the same inequality rather
    than a separate special case.
    """
    no_bid = getattr(quote, "no_bid_cc", None)
    if no_bid is None or int(no_bid) <= 0:
        return NO_QUOTE_RANK
    return float(int(no_bid))


def assert_diversifier_tighter(
    diversifier: ConcentrationSteer,
    concentrator: ConcentrationSteer,
    *,
    pre_snap: tuple[int, int] | None = None,
    post_snap: tuple[Any, Any] | None = None,
) -> None:
    """THE OPERATOR INVARIANT, checked at all three stages.

    ``diversifier`` LOWERS dollar-weighted book concentration
    (``hhi.relative > 0``); ``concentrator`` is the otherwise-identical
    candidate that does not. Raises ``AssertionError`` naming the stage.

    Stage 1 — CLASSIFIER: ``applied_cc`` (a higher pricer-frame skew raises
              ``no_bid`` => cheaper combo => tighter quote).
    Stage 2 — PRE-SNAP: the raw ``no_raw`` centi-cents, before the grid.
    Stage 3 — POST-SNAP: :func:`quote_rank_cc`, with a ``NoQuote`` ranked at
              ``-inf`` so a REFUSAL can never land on the diversifier — that
              case is the SAME inequality, not a special case.

    STRICT vs WEAK, stated honestly. When the diversifier strictly lowers
    concentration and the concentrator does NOT lower it at all
    (``relative <= 0``), the inequality is STRICT at every stage: the ladder's
    minimum rung guarantees the diversifier at least +1 tick and the
    concentrator at most -1 (or 0 when it is exactly neutral). When BOTH
    candidates diversify and differ only in degree, a finite tick lattice
    cannot separate every pair — the guarantee is then WEAK (>=), which is
    what "ordering survives rounding" means: rounding may compress two
    same-signed candidates onto one rung, but it can never INVERT them and can
    never annihilate the sign.
    """
    # The ranking quantity is the SCALE-FREE reading (``hhi.score`` =
    # ``-intensity``), because that is the number the steer actually prices off
    # since 2026-07-27; ``relative`` carries a book-size factor and would rank
    # the same pair differently on a bigger book.
    if not diversifier.hhi.score > 0.0:
        raise AssertionError(
            "assert_diversifier_tighter: first argument is not a diversifier "
            f"(hhi.score={diversifier.hhi.score})"
        )
    if diversifier.hhi.score <= concentrator.hhi.score:
        raise AssertionError(
            "assert_diversifier_tighter: the two candidates are not ordered "
            f"({diversifier.hhi.score} <= {concentrator.hhi.score})"
        )
    strict = concentrator.hhi.score <= 0.0

    def _check(stage: str, div: float, conc: float) -> None:
        if strict:
            if not div > conc:
                raise AssertionError(
                    f"{stage}: diversifier not strictly tighter ({div} <= {conc})"
                )
        elif div < conc:
            raise AssertionError(
                f"{stage}: ORDER INVERTED by rounding ({div} < {conc})"
            )

    _check("CLASSIFIER", diversifier.applied_cc, concentrator.applied_cc)
    if pre_snap is not None:
        _check("PRE-SNAP", pre_snap[0], pre_snap[1])
    if post_snap is not None:
        div_q, conc_q = post_snap
        div_rank, conc_rank = quote_rank_cc(div_q), quote_rank_cc(conc_q)
        if div_rank == NO_QUOTE_RANK:
            raise AssertionError(
                "POST-SNAP: the REFUSAL landed on the diversifier (infinite width)"
            )
        _check("POST-SNAP", div_rank, conc_rank)


def position_event_bucket(
    position: OpenPosition, key_fn: Any
) -> tuple[str, ...]:
    """:func:`event_bucket` for a whole position (its ticket is the loss event)."""
    return event_bucket(position.legs, key_fn)
