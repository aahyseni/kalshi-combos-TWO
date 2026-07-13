"""Explicit schedule-feed cache — the precision tier between embedded-ET and the
estimate in the pregame start-time ladder (Phase 5, R3 Part B).

Sibling of ``MetadataCache``: a peek-only, in-memory, hot-path-safe lookup of a
game's SCHEDULED UTC start keyed by ``event_ticker``. It exists so a family
WITHOUT an embedded start token but WITH a reliable public schedule (soccer/NFL/
NBA fixture feeds) can be quoted right up to ~M_q before kickoff instead of
losing the last ~1.5h to the blunt ``expiry − 4.5h`` estimate.

SEAM ONLY this phase — INACTIVE by default (empty table). Rules that make it
safe (never loosened without a hard-rule-5-verified feed):

- The ``event_ticker → scheduled UTC start`` mapping is an EXPLICIT table
  (defense #2, same rule as the SGO mapping): NO fuzzy matching, NO parsing a
  start out of the ticker here (that is the embedded-ET path's job, and only for
  API-verified families).
- Fail-closed: a cache MISS returns None, so ``leg_start_time`` falls through to
  the estimate ⇒ UNKNOWN ⇒ decline. A miss is never a guess.
- Every entry MUST be tz-aware UTC. A naive datetime is rejected at insert (a
  naive time = no clock = would misgate) — fail-closed on construction, never at
  read time on the hot path.
- No I/O: the refresh that POPULATES the table runs off the hot path (like
  metadata), from a feed whose start times carry the same API cross-check the
  embedded-ET path got. That feed + its verification is DEFERRED (R3 §B ship
  order); this class is the interface it will fill.
"""

from __future__ import annotations

from datetime import UTC, datetime


class ScheduleCache:
    """In-memory ``event_ticker → scheduled UTC start`` table (peek-only).

    Constructed empty (INACTIVE). ``upsert`` adds an explicit, tz-aware UTC
    entry off the hot path; ``peek_start`` reads it on the hot path and returns
    None on a miss (fail-closed). There is deliberately no fuzzy/prefix lookup —
    the key is the exact ``event_ticker``.
    """

    def __init__(self, starts: dict[str, datetime] | None = None) -> None:
        self._starts: dict[str, datetime] = {}
        for event_ticker, start in (starts or {}).items():
            self.upsert(event_ticker, start)

    def upsert(self, event_ticker: str, scheduled_start: datetime) -> None:
        """Add/replace an explicit scheduled start. The start MUST be tz-aware;
        a naive datetime is rejected (no clock ⇒ would misgate — fail-closed at
        insert, never silently normalised to a guessed zone)."""
        if scheduled_start.tzinfo is None:
            raise ValueError(
                f"schedule start for {event_ticker!r} must be tz-aware "
                "(naive = no clock = fail-closed)"
            )
        self._starts[event_ticker] = scheduled_start.astimezone(UTC)

    def peek_start(self, event_ticker: str | None) -> datetime | None:
        """Scheduled UTC start for this event, or None on a miss (fail-closed).

        Hot-path safe: pure in-memory dict lookup, no network. A None event key
        (a leg with no event) can never match and returns None."""
        if event_ticker is None:
            return None
        return self._starts.get(event_ticker)

    @property
    def size(self) -> int:
        return len(self._starts)
