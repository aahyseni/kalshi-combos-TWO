"""THE ONE WRITE-TOKEN BUDGET (extracted from ops/supervisor.py 2026-07-26).

Kalshi rate-limits by TOKEN BUCKET, not by request count. Every write endpoint
carries a token COST and the tier carries a per-second token CEILING —
``CreateQuote``/``DeleteQuote`` cost **2 tokens each**, Advanced tier is **300
write tokens/s** on a 1-second bucket (docs/api-notes/openapi-comms.md, the
"Rate limits" note; the same costs are authoritative from
``GET /account/endpoint_costs``). Two consequences the codebase must obey:

  * A BURST of writes has to be paced in TOKENS, never in "how many coroutines
    may run at once". A fan-out of N against latency L is ``N / L`` requests/s =
    ``2N / L`` tokens/s — a number that is not a property of our code at all,
    but of the exchange's current latency, and that DOUBLES every time the
    exchange gets twice as fast. A fan-out of 8 at 40 ms is 400 tokens/s; the
    same 8 at 5 ms is 3,200 tokens/s. Both blow the 300 t/s ceiling and both
    look identical in the source.
  * There must be exactly ONE budget concept. A 429 is not a local error: it
    lands on the shared account bucket, so the supervisor's kill path and the
    bot's own withdrawal wave are spending from the same place. They are sized
    from the same config source (``SupervisorConfig.write_budget_capacity`` /
    ``write_budget_refill_s``) so there is one number for the operator to move.

Callers today:
  * ``ops.supervisor.SafetySupervisor.emergency_cancel_all`` — a RESERVED
    budget, so a 429 storm on the bot's own writes can never leave the kill
    path without tokens.
  * ``rfq.lifecycle.QuoteLifecycle._withdraw_batch`` — the end-of-game quote
    withdrawal wave (the 2026-07-26 incident's path).

Refill is PROPORTIONAL (a standard leaky bucket: ``capacity`` tokens per
``refill_s``, credited continuously) rather than an all-at-once refill at each
window boundary. Both give the same sustained rate and the same "full bucket
when an idle kill path fires", but the boundary variant admits up to 2x
``capacity`` inside a single second (spend the tail of window N at t-, spend a
freshly-refilled window N+1 at t+), which at capacity 200 is 400 tokens in one
1-second exchange bucket — over the 300 t/s ceiling, i.e. the very 429 the
budget exists to prevent. Proportional refill bounds any window T at
``capacity + rate * T`` (200 + 20 = 220 tokens in any 1 s), which is under the
ceiling by construction.

Deterministic under a fake clock; no wall-clock reads outside ``Clock``.
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.clock import Clock


@dataclass(slots=True)
class _BudgetState:
    tokens: int
    last_refill: float


@dataclass(frozen=True, slots=True)
class WriteBudget:
    """A token bucket for API WRITES, denominated in the exchange's own tokens.

    ``capacity`` tokens accrue per ``refill_s``; ``try_spend(cost)`` consumes
    ``cost`` tokens if they are there. The bucket starts FULL, so an idle caller
    (the kill path) always has its whole budget available the instant it fires.

    Frozen: the mutable state (tokens, last-refill) lives in a tiny inner box so
    the public handle stays hashable/immutable while the bucket refills.
    """

    clock: Clock
    capacity: int
    refill_s: float
    _state: _BudgetState
    # PRIORITY FLOOR (2026-07-27). Tokens ROUTINE spenders may not touch, so a
    # continuous high-volume consumer can never take the whole bucket away from
    # a low-volume, CORRECTNESS-CRITICAL one. 0 ⇒ the historical single-tier
    # behaviour (every caller equal), which is what every write caller keeps.
    reserve: int = 0

    @classmethod
    def create(
        cls, clock: Clock, *, capacity: int, refill_s: float, reserve: int = 0
    ) -> WriteBudget:
        if capacity < 1:
            raise ValueError("write budget capacity must be >= 1")
        if refill_s <= 0:
            raise ValueError("write budget refill_s must be > 0")
        if reserve < 0:
            raise ValueError("budget reserve must be >= 0")
        if reserve >= capacity:
            # A reserve that swallows the bucket would starve the ROUTINE tier
            # forever — the mirror of the defect the reserve exists to fix.
            raise ValueError(
                f"budget reserve {reserve} must be < capacity {capacity}"
            )
        return cls(
            clock=clock,
            capacity=capacity,
            refill_s=refill_s,
            _state=_BudgetState(tokens=capacity, last_refill=clock.now().timestamp()),
            reserve=reserve,
        )

    @property
    def rate_per_s(self) -> float:
        """Sustained tokens per second this bucket admits."""
        return self.capacity / self.refill_s

    def _refill(self) -> None:
        now = self.clock.now().timestamp()
        elapsed = now - self._state.last_refill
        if elapsed <= 0.0:
            return
        gained = int(self.capacity * elapsed / self.refill_s)
        if gained <= 0:
            return
        self._state.tokens = min(self.capacity, self._state.tokens + gained)
        if self._state.tokens >= self.capacity:
            # Full: no credit accrues while idle beyond the cap.
            self._state.last_refill = now
        else:
            # Advance by EXACTLY the time we credited, so sub-token gaps still
            # accumulate instead of being rounded away on every call.
            self._state.last_refill += gained * self.refill_s / self.capacity

    def _floor(self, critical: bool) -> int:
        """Tokens this tier may NOT spend below. CRITICAL callers see 0 (the
        whole bucket); ROUTINE callers see ``reserve``."""
        return 0 if critical else self.reserve

    def try_spend(self, cost: int = 1, *, critical: bool = False) -> bool:
        """Consume ``cost`` tokens; True if they were available. Refills first.

        ``cost`` defaults to 1 — the historical, request-counting behaviour the
        supervisor's reserved cancel allowance is sized in. Callers pacing
        against the exchange's real bucket pass the endpoint's DOCUMENTED cost
        (``exchange.rest.DELETE_QUOTE_TOKEN_COST``).

        ``critical=True`` may draw the ``reserve``; the default ROUTINE tier
        may not (it yields at the floor). With ``reserve=0`` both tiers are
        identical, which is every existing caller."""
        if cost < 1:
            raise ValueError(f"cost must be >= 1, got {cost}")
        self._refill()
        state = self._state
        # Floor INLINED, not ``self._floor(critical)``: this is the write/cancel
        # and metadata-pacing hot path, and a Python method call here measured
        # +8.8% per call against HEAD where the subtraction measures +0.5%
        # (bench in the report). "Throughput never regresses" applies to the
        # primitive too.
        available = state.tokens if critical else state.tokens - self.reserve
        if available >= cost:
            state.tokens -= cost
            return True
        return False

    def seconds_until(self, cost: int = 1, *, critical: bool = False) -> float:
        """Wall seconds until ``cost`` tokens are available to this TIER (0.0
        when they are now, ``inf`` when ``cost`` exceeds what the tier could
        ever hold and no amount of waiting can satisfy it).

        A caller that must WAIT for budget derives its sleep from the bucket's
        own rate here instead of inventing a poll interval."""
        floor = self._floor(critical)
        if cost > self.capacity - floor:
            return float("inf")
        self._refill()
        available = self._state.tokens - floor
        if available >= cost:
            return 0.0
        needed = cost - available
        return needed * self.refill_s / self.capacity

    @property
    def tokens(self) -> int:
        self._refill()
        return self._state.tokens


# The bucket itself is not write-specific — it counts the exchange's tokens, and
# Kalshi meters READS the same way against a SECOND, independent bucket (GET
# /account/limits returns both). The 2026-07-26 incident's 5,726 metadata 429s in
# 6 minutes were a READ-bucket breach, paced by nothing at all. Rather than
# clone this, the read side takes the same primitive under a name that does not
# lie about which bucket it guards; ``WriteBudget`` stays as the historical name
# for the withdrawal/kill paths.
#
# PRIORITY (2026-07-27): one bucket shared by a CONTINUOUS high-volume consumer
# (metadata refresh) and a LOW-VOLUME correctness-critical one (settled-leg
# resolution) is a priority inversion — the high-volume consumer wins every race
# for the last token and settlement resolution is what starves. ``reserve`` is
# the repair: a floor of tokens the ROUTINE tier may not touch, so the CRITICAL
# tier always has its own bounded pass available. The caller DERIVES the reserve
# from the critical consumer's own bounded per-pass claim × the live-verified
# per-call token cost — never a number picked by hand.
TokenBudget = WriteBudget
