"""DIVERSITY-AWARE EVICTION KEY + DERIVED OPEN-QUOTE CAPACITY (2026-07-31).

Operator ruling (verbatim): "we can bump the 200 up, but 5 $1 EV quotes
shouldn't lose to 1 $5 EV quote, especially if the 5 quotes are diverse."

Two mechanisms, both pure functions here (rule 8: the lifecycle imports and
drives them; nothing in this module touches exchange or book state):

1.  MARGINAL BOOK VALUE — the slot-eviction ranking key becomes

        key(q) = dEV(q) x P(accept | size-bucket, MEASURED) - dES99(q)

    * dEV: the SAME stored quote-time EV the absolute-EV key ranks on today
      (``OpenQuoteRisk.expected_edge_cc`` / ``_quote_candidate_ev_cc``).
    * P(accept | size-bucket): measured on OUR OWN tape, in-process counters
      of quotes sent vs accepted per premium-at-risk bucket, bounded with
      exact Clopper-Pearson intervals at the RATIFIED alpha anchor
      (``portfolio_kill_tail_prob`` = 0.02 — the same anchor the derived
      burst floor rides; no new hand number). The measured flow fact this
      encodes: acceptance per quoted RFQ is 10-30x HIGHER on small quotes
      (<$5: ~1.57/1k pre-arming) than the raw EV key can see, so ranking on
      raw EV alone structurally starves the small diverse book the operator
      wants (the 7/26-28 mornings' 53-85 small resting quotes).
    * dES99: the quote's marginal contribution to the book tail, ALLOCATED
      from the additive per-game CVaR decomposition the book-risk MC already
      publishes (``BookRiskSnapshot.per_game_tail_cc``, sum over games = the
      book CVaR exactly) — NO new model, no extra MC. A diverse quote on a
      game with no tail contribution scores ~0; a hedge (negative
      contribution) scores NEGATIVE dES99 (a key BOOST — "hedges are +EV");
      a fifth quote on the hot game pays its concentration.

    FAIL-CLOSED (quiet-failure defense 2): when the acceptance table is THIN
    — either bucket has no usable Clopper-Pearson interval, or the table
    cannot DISCRIMINATE buckets (within-bucket CP ratio wider than the
    measured between-bucket spread) — the caller falls back to the current
    absolute-EV key, i.e. exactly today's behaviour. UNKNOWN is never a
    convenient loser.

    CHURN HYSTERESIS — DERIVED, never typed: the candidate is scored at its
    bucket's CP LOWER bound and the incumbent at its bucket's CP UPPER
    bound, so an eviction requires the candidate to beat the incumbent by
    more than the measurement's own confidence gap. The noise floor of the
    key IS the CP interval propagated through the key — it shrinks as the
    counters grow, exactly as a measured floor must. (The existing
    anti-thrash density ledger stays armed on top.)

2.  DERIVED OPEN-QUOTE CAPACITY — replaces the hand-bumped
    ``max_open_quotes`` (20 -> 60 -> 120 -> 200, four manual bumps; verified
    against docs/api-notes: Kalshi documents NO maker open-quote cap
    (docs/research/rfq_throughput/04-exchange-constraints.md, create-quote
    absence) — the ONLY exchange constraint is the WRITE TOKEN BUCKET
    (docs/api-notes/limits-account.md: CreateQuote/DeleteQuote cost 2 tokens
    each; the tier ceiling is OBSERVED live via GET /account/limits,
    ``ApiTierLimits``)). A standing book of N quotes must be re-postable
    every TTL (a reprice or an expiry-relight both cost one delete + one
    create per cycle — a PROTOCOL cost, 4 tokens), so the write bucket
    itself bounds the standing book:

        capacity = (tier_write_rate - kill_reserve_rate - measured_flow_rate)
                   x ttl_s / refresh_cost_tokens

    * tier_write_rate: the OBSERVED account write refill (never assumed).
    * kill_reserve_rate: the withdrawal/kill bucket's sustained rate — the
      cancel-all path must never compete with resting maintenance.
    * measured_flow_rate: our own measured token spend on NEW quoting
      (sent-quote rate x create+delete cost, sampled from live counters).
      This term double-counts the standing book's own refresh (a reprice
      re-registers as a sent quote), which is strictly CONSERVATIVE —
      derived capacity is understated, never overstated.
    * refresh_cost_tokens: create + delete = 4, the documented endpoint
      costs (a protocol fact, not a tuned constant).

    MASS-ACCEPTANCE GUARD (why capacity is a THROUGHPUT bound, not a risk
    bound): admitting more RESTING quotes admits zero additional risk —
    every fill still passes the exact confirm-path enforcement
    (reservations + candidate MC + waiver), the fill-velocity governor, and
    the mass-acceptance worst-case caps, all of which bind on the ADMITTED
    book independently of how many quotes rest. The measured worst accept
    bursts ever (25 accepts / $400 per 60s, 7/28-7/29 tape) are enforced at
    confirm; the failure direction of a larger standing book under a mass-
    acceptance wave is RENEGES (confirm declines), never uncapped loss.

    FAIL-CLOSED: no measured flow window yet (first ~60s after boot), an
    unreadable tier, or a derivation that cannot admit a single quote =>
    the caller keeps TODAY'S configured ``max_open_quotes`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "SIZE_BUCKET_EDGES_CC",
    "AcceptanceCounters",
    "BucketBounds",
    "DerivedCapacity",
    "allocate_des99_cc",
    "binom_tail_gt",
    "clopper_pearson_lower",
    "clopper_pearson_upper",
    "derive_open_quote_capacity",
    "diversity_key",
    "size_bucket",
]


# --- exact binomial tail + Clopper-Pearson upper --------------------------
# Kept IN SYNC with ``risk/burst_floor.py`` (the derived-burst-floor build,
# WITHDRAWN from the wiring 2026-07-31 but its pure math is unchanged).
# Inlined rather than imported so this module — which ships — does not
# depend on a module whose feature was withdrawn and may be deleted.


def binom_tail_gt(n: int, p: float, k: int) -> float:
    """Exact ``P(Binom(n, p) > k)`` by the term recurrence (no lgamma, no
    allocation; ``k + 1`` multiplies)."""
    if k >= n or n <= 0:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    term = (1.0 - p) ** n
    cdf = term
    ratio = p / (1.0 - p)
    for i in range(k):
        term *= (n - i) / (i + 1) * ratio
        cdf += term
    return max(0.0, 1.0 - cdf)


def clopper_pearson_upper(x: int, n: int, alpha: float) -> float:
    """Exact one-sided Clopper-Pearson UPPER bound
    ``sup{p: P(X<=x; n,p) > alpha}`` by bisection on the exact CDF.
    Degenerate inputs return 1.0 (NO information => the widest bound =>
    fail CLOSED — an unmeasured incumbent is never under-credited)."""
    if n <= 0 or x >= n or x < 0:
        return 1.0
    if not (0.0 < alpha < 1.0):
        return 1.0
    lo, hi = x / n, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if 1.0 - binom_tail_gt(n, mid, x) > alpha:
            lo = mid
        else:
            hi = mid
    return hi

# The MEASUREMENT PARTITION of the acceptance tape: premium-at-risk dollars
# $5 / $15 / $50 / $150 in centi-cents ($1 = 10_000 cc). These are the exact
# buckets the 2026-07-31 flow study measured the acceptance collapse in
# (<$5: 1.57->0.57 per 1k, $5-15: 1.07->0.27, $15-50: 8.19->1.55,
# $50-150: 19.79->8.99, >$150: 0), kept IDENTICAL so the live counters are
# directly comparable with the tape baseline. A partition of a measurement,
# not a risk knob — no admission/size decision reads an edge directly.
SIZE_BUCKET_EDGES_CC: tuple[int, ...] = (50_000, 150_000, 500_000, 1_500_000)
N_BUCKETS = len(SIZE_BUCKET_EDGES_CC) + 1


def size_bucket(det_cc: int) -> int:
    """The acceptance-tape bucket of a quote's premium at risk (worse quotable
    side, ``_quote_det_consumed_cc`` — the same figure the det axis charges)."""
    for i, edge in enumerate(SIZE_BUCKET_EDGES_CC):
        if det_cc < edge:
            return i
    return N_BUCKETS - 1


def clopper_pearson_lower(x: int, n: int, alpha: float) -> float:
    """Exact one-sided Clopper-Pearson LOWER bound,
    ``sup{p : P(X >= x; n, p) <= alpha}``, by bisection on the exact upper
    tail — the same recurrence ``burst_floor.binom_tail_gt`` computes, kept
    in the SMALL-COUNT regime (``k = x - 1`` accepts, tiny) where it cannot
    underflow (the textbook duality ``1 - upper(n - x, n)`` walks the
    recurrence ``n - x`` terms from ``(1-p)^n`` at ``p -> 1``, which
    underflows to a bound of exactly ``x/n`` — a silently WIDER-than-nothing
    lie; found at n=1000). Degenerate inputs return 0.0 — NO information =>
    the smallest possible acceptance credit => fail CLOSED (a thin bucket
    can never inflate a candidate's key)."""
    if n <= 0 or x <= 0 or x > n:
        return 0.0
    if not (0.0 < alpha < 1.0):
        return 0.0
    lo, hi = 0.0, x / n
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        # P(X >= x) = P(X > x - 1): above the true lower bound this tail
        # exceeds alpha.
        if binom_tail_gt(n, mid, x - 1) > alpha:
            hi = mid
        else:
            lo = mid
    return lo


@dataclass(frozen=True, slots=True)
class BucketBounds:
    """One bucket's measured acceptance with its exact CP interval."""

    quoted: int
    accepted: int
    p_hat: float
    lower: float
    upper: float


class AcceptanceCounters:
    """In-process per-bucket acceptance tape: quotes SENT vs quotes ACCEPTED.

    Cumulative from boot, the ``burst_floor.AcceptanceTape`` precedent: an
    in-process tape has no staleness surface, restarts re-measure honestly
    (the table is THIN after a boot and the caller fails closed to the
    absolute-EV key until it isn't — conservative, self-arming intraday at
    the live ~19k sent/day). O(1) increments; nothing here allocates on the
    hot path."""

    __slots__ = ("_accepted", "_quoted")

    def __init__(self) -> None:
        self._quoted = [0] * N_BUCKETS
        self._accepted = [0] * N_BUCKETS

    def record_quoted(self, det_cc: int) -> None:
        self._quoted[size_bucket(det_cc)] += 1

    def record_accepted(self, det_cc: int) -> None:
        self._accepted[size_bucket(det_cc)] += 1

    def bounds(self, bucket: int, alpha: float) -> BucketBounds | None:
        """The bucket's measured acceptance with exact CP bounds; None when
        the bucket has no quoted denominator at all."""
        n = self._quoted[bucket]
        if n <= 0:
            return None
        x = min(self._accepted[bucket], n)
        return BucketBounds(
            quoted=n,
            accepted=x,
            p_hat=x / n,
            lower=clopper_pearson_lower(x, n, alpha),
            upper=clopper_pearson_upper(x, n, alpha),
        )

    def discriminating(self, alpha: float) -> bool:
        """True when the table can RANK buckets better than its own noise.

        Derived criterion, no typed threshold: the table discriminates once
        at least one PAIR of accept-carrying buckets has DISJOINT
        Clopper-Pearson intervals (one bucket's exact lower bound above
        another's exact upper bound) — the tape has then measurably
        separated two size classes at confidence 1 - alpha. Until that
        happens any between-bucket ranking is inside the measurement's own
        noise — fail closed (the caller keeps today's absolute-EV key)."""
        carrying = [
            b
            for i in range(N_BUCKETS)
            if (b := self.bounds(i, alpha)) is not None and b.accepted > 0
        ]
        if len(carrying) < 2:
            return False
        best_lower = max(b.lower for b in carrying)
        worst_upper = min(b.upper for b in carrying)
        return best_lower > worst_upper

    def snapshot(self) -> dict[str, list[int]]:
        return {"quoted": list(self._quoted), "accepted": list(self._accepted)}


def diversity_key(ev_cc: int, p_accept: float, des99_cc: float) -> float:
    """Marginal book value of a resting quote, float cc: realized-EV rate
    minus marginal tail contribution. The caller chooses WHICH CP bound of
    P(accept) to pass (candidate: lower; incumbent: upper) — that asymmetry
    IS the derived churn hysteresis."""
    return float(ev_cc) * p_accept - des99_cc


def allocate_des99_cc(
    quote_games: frozenset[str] | set[str],
    quote_det_cc: int,
    tail_by_game_cc: dict[str, float],
    det_by_game_cc: dict[str, float],
) -> float:
    """The quote's share of the book's per-game tail decomposition.

    Each game's CVaR contribution (additive: sum over games = book CVaR,
    hedge games NEGATIVE) is allocated across that game's resting/committed
    premium at risk proportionally to this quote's det consumption — a
    model-free within-game split of the MC's own numbers. Games absent from
    the decomposition contribute 0 (a diversifier on an untouched game adds
    no tail — that is the point). Missing/zero denominators contribute 0,
    never a credit."""
    total = 0.0
    for game in quote_games:
        tail = tail_by_game_cc.get(game)
        if tail is None:
            continue
        denom = det_by_game_cc.get(game, 0.0)
        if denom <= 0.0:
            continue
        share = min(1.0, quote_det_cc / denom)
        total += tail * share
    return total


@dataclass(frozen=True, slots=True)
class DerivedCapacity:
    """One capacity derivation with every input it was derived from (the log
    line the operator arms from prints exactly this)."""

    capacity: int
    usable: bool
    reason: str
    tier_write_rate_per_s: float
    reserve_rate_per_s: float
    measured_flow_tokens_per_s: float | None
    ttl_s: float
    refresh_cost_tokens: int
    fallback: int


def derive_open_quote_capacity(
    *,
    tier_write_rate_per_s: float,
    reserve_rate_per_s: float,
    measured_flow_tokens_per_s: float | None,
    ttl_s: float,
    refresh_cost_tokens: int,
    fallback: int,
) -> DerivedCapacity:
    """The write bucket's own bound on a standing quote book (module
    docstring, mechanism 2). Fail-closed to ``fallback`` (today's configured
    cap) on any unusable input; the derivation NEVER returns a capacity the
    bucket cannot actually refresh."""

    def _closed(reason: str) -> DerivedCapacity:
        return DerivedCapacity(
            capacity=fallback,
            usable=False,
            reason=reason,
            tier_write_rate_per_s=tier_write_rate_per_s,
            reserve_rate_per_s=reserve_rate_per_s,
            measured_flow_tokens_per_s=measured_flow_tokens_per_s,
            ttl_s=ttl_s,
            refresh_cost_tokens=refresh_cost_tokens,
            fallback=fallback,
        )

    if measured_flow_tokens_per_s is None or measured_flow_tokens_per_s < 0:
        return _closed("no_measured_flow_window")
    if tier_write_rate_per_s <= 0:
        return _closed("tier_write_rate_unusable")
    if ttl_s <= 0 or refresh_cost_tokens <= 0:
        return _closed("invalid_ttl_or_cost")
    headroom = (
        tier_write_rate_per_s - reserve_rate_per_s - measured_flow_tokens_per_s
    )
    derived = int(headroom * ttl_s / refresh_cost_tokens)
    if derived < 1:
        # A capacity that cannot admit one quote is a 100%-decline dressed as
        # a cap (the 2026-07-23 lesson: a cap must be PROVEN to quote).
        return _closed("no_headroom")
    return DerivedCapacity(
        capacity=derived,
        usable=True,
        reason="derived",
        tier_write_rate_per_s=tier_write_rate_per_s,
        reserve_rate_per_s=reserve_rate_per_s,
        measured_flow_tokens_per_s=measured_flow_tokens_per_s,
        ttl_s=ttl_s,
        refresh_cost_tokens=refresh_cost_tokens,
        fallback=fallback,
    )
