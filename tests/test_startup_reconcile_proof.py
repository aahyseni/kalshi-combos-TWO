"""THE STARTUP RECONCILE IS A PROOF, NOT A GESTURE (fail-open seam, 2026-07-27).

``QuoteApp._startup_reconcile`` is the re-proof that no quote of ours is still
resting on the exchange after a restart. Everything downstream — the
``needs_reconcile`` block, ``_book_reconciled``, prod preflight gate 5, and every
halt-consequence scope-down that leans on "a restart re-proves the book" — is
sound ONLY if that pass cannot declare success while a leftover quote is still
live.

It could. The pass swallowed a per-quote ``KalshiApiError`` with a warning
(``startup_cancel_failed``) and STILL logged ``startup_reconciled`` and greened
the gate, so a single 429 or 503 on one DELETE left a quote resting on the wire
while the bot resumed quoting against a book it believed was empty. That quote
can fill.

What these tests pin, in order:

1.  A leftover quote whose DELETE 429s leaves the book UNPROVEN: the marker
    stays, ``_book_reconciled`` stays False, the prod preflight goes RED, and the
    loud event NAMES the market that could not be proven.
2.  A 404 IS proof (the exchange has no such quote — it cannot fill), and only
    404: the narrowness is shared with the lifecycle's withdrawal path via ONE
    ``already_gone``.
3.  A clean boot still reconciles and still quotes — no throughput or behaviour
    change on the happy path.
4.  The unreachable-exchange path TERMINATES (bounded attempts, bounded wall) and
    never quotes against an unproven book — including on a transport error, which
    is not a ``KalshiApiError`` and used to propagate out of the pass entirely.
5.  The bounded retry is real: a transient 429 that clears on the next attempt is
    cured by the bot, not by a human.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog

from combomaker.exchange.quote_query import RETRIES as RECONCILE_RETRIES
from combomaker.exchange.rest import KalshiApiError, RateLimitedError
from combomaker.ops.preflight import PreflightError
from combomaker.ops.quote_app import QuoteApp
from tests.test_quote_app_phase6 import (
    _demo_app,
    _prod_app_for_preflight,
    _reservation,
)

CLETB = "KXMLBGAME-26JUL27CLETB-CLE"


class _LeftoverRest:
    """A REST double that reports leftover resting quotes and lets each DELETE
    outcome be scripted per attempt. ``deleted`` records every DELETE ACTUALLY
    ATTEMPTED — the test's window into how hard the pass tried and when it
    stopped."""

    def __init__(
        self,
        quotes: list[dict[str, Any]],
        *,
        delete_raises: list[BaseException | None] | BaseException | None = None,
        list_raises: BaseException | None = None,
    ) -> None:
        self._quotes = quotes
        self._list_raises = list_raises
        self._script = delete_raises
        self.deleted: list[str] = []
        self.list_calls = 0

    async def get_quotes(self, **params: Any) -> dict[str, Any]:
        self.list_calls += 1
        if self._list_raises is not None:
            raise self._list_raises
        return {"quotes": self._quotes, "cursor": ""}

    async def delete_quote(self, quote_id: str) -> dict[str, Any]:
        n = len(self.deleted)
        self.deleted.append(quote_id)
        script = self._script
        exc: BaseException | None
        if isinstance(script, list):
            exc = script[n] if n < len(script) else script[-1]
        else:
            exc = script
        if exc is not None:
            raise exc
        return {}

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        return {"market_positions": []}


class _JumpClock:
    """Monotonic clock that jumps ``step_s`` on every read. Used to drive the
    withdrawal pass's WALL deadline deterministically, with no sleeping."""

    def __init__(self, step_s: float) -> None:
        self._step_ns = int(step_s * 1e9)
        self._ns = 0

    def monotonic_ns(self) -> int:
        self._ns += self._step_ns
        return self._ns

    def now(self) -> datetime:
        return datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _app_with_marker(tmp_path: Path) -> QuoteApp:
    """A bot that a prior hard halt left with the reconcile marker in force —
    the state a restart actually boots into."""
    (tmp_path / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")
    return _demo_app(tmp_path)


def _quote(quote_id: str, ticker: str = CLETB) -> dict[str, Any]:
    return {"id": quote_id, "market_ticker": ticker, "status": "open"}


# --------------------------------------------------------------------------- #
# 1 — a 429'd DELETE leaves the book UNPROVEN and quoting BLOCKED
# --------------------------------------------------------------------------- #


async def test_a_429_on_a_leftover_delete_leaves_the_book_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE SEAM. One leftover quote, every DELETE refused by the exchange's token
    bucket. A 429 means the request never reached the book — the quote may still
    be RESTING and may still FILL. So it is not proof, the pass fails, the marker
    stays, and quoting stays blocked."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest(
        [_quote("q-left")], delete_raises=RateLimitedError(429, "rate", "slow down")
    )

    with structlog.testing.capture_logs() as cap:
        await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]

    assert app._book_reconciled is False                 # the gate never greens
    assert (tmp_path / "needs_reconcile").exists()       # the block stays in force
    assert app._metrics.counter("startup_reconcile.unproven_quotes") == 1

    # It TRIED — the bounded retry — and then stopped. It did not spin.
    assert len(rest.deleted) == RECONCILE_RETRIES
    assert set(rest.deleted) == {"q-left"}

    events = {e["event"] for e in cap}
    # The success line must NOT be emitted: that was the whole lie.
    assert "startup_reconciled" not in events
    assert "startup_reconcile_unproven_quotes" in events
    loud = next(e for e in cap if e["event"] == "startup_reconcile_unproven_quotes")
    assert loud["log_level"] == "error"
    assert loud["tickers"] == [CLETB]            # NAMES the market, not just an id
    assert loud["quote_ids"] == ["q-left"]

    # …and the consequence: prod preflight gate 5 is RED, which raises and exits.
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    prod = _prod_app_for_preflight(tmp_path, reconciled=app._book_reconciled)
    with pytest.raises(PreflightError, match="book_reconciled"):
        prod._run_prod_preflight()


async def test_a_5xx_on_a_leftover_delete_is_also_not_proof(tmp_path: Path) -> None:
    """Same narrowness for the other unresolved modes: a 503 is not a confirmed
    withdrawal either."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest(
        [_quote("q-left")], delete_raises=KalshiApiError(503, "unavailable", "down")
    )
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists()


# --------------------------------------------------------------------------- #
# 2 — a 404 COUNTS as gone
# --------------------------------------------------------------------------- #


async def test_a_404_counts_as_gone(tmp_path: Path) -> None:
    """A 404 on a withdrawal is SUCCESS: the exchange has no such quote, so it is
    provably off the wire and can never fill. Fail-closed must not mean
    fail-stuck — a leftover that already expired must not brick the restart."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest(
        [_quote("q-expired")], delete_raises=KalshiApiError(404, "not_found", "gone")
    )

    with structlog.testing.capture_logs() as cap:
        await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]

    assert app._book_reconciled is True                    # proven ⇒ unblocked
    assert not (tmp_path / "needs_reconcile").exists()     # marker cleared
    assert rest.deleted == ["q-expired"]                   # asked exactly once
    assert "startup_reconciled" in {e["event"] for e in cap}


# --------------------------------------------------------------------------- #
# 3 — a clean boot still reconciles and still quotes
# --------------------------------------------------------------------------- #


async def test_clean_boot_reconciles_and_unblocks_quoting(tmp_path: Path) -> None:
    """No regression on the happy path: leftovers ACK, the book is proven, the
    marker clears, the gate greens."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest([_quote("q1"), _quote("q2", "KXWC-OTHER")])

    with structlog.testing.capture_logs() as cap:
        await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]

    assert app._book_reconciled is True
    assert not (tmp_path / "needs_reconcile").exists()
    assert rest.deleted == ["q1", "q2"]
    done = next(e for e in cap if e["event"] == "startup_reconciled")
    assert done["leftover_quotes"] == 2 and done["withdrawn"] == 2
    assert "book_reconciled" in {e["event"] for e in cap}


async def test_clean_boot_with_no_leftovers_reconciles(tmp_path: Path) -> None:
    """The common case — nothing was resting. Zero DELETEs, still proven."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest([])
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is True
    assert rest.deleted == []
    assert not (tmp_path / "needs_reconcile").exists()


# --------------------------------------------------------------------------- #
# 4 — the unreachable exchange TERMINATES and never quotes
# --------------------------------------------------------------------------- #


async def test_unreachable_exchange_terminates_and_never_quotes(
    tmp_path: Path,
) -> None:
    """The enumeration itself fails (5xx). ``list_open_quotes`` retries a BOUNDED
    number of times and then raises; the pass catches, fails closed, and RETURNS
    — no spin, nothing assumed gone, quoting blocked."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest([], list_raises=KalshiApiError(503, "unavailable", "down"))

    with structlog.testing.capture_logs() as cap:
        await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]

    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists()
    assert rest.deleted == []                       # NOTHING was assumed gone
    assert rest.list_calls == RECONCILE_RETRIES     # bounded, then it stopped
    failed = next(e for e in cap if e["event"] == "startup_reconcile_failed")
    assert failed["phase"] == "enumerate" and failed["log_level"] == "error"


async def test_a_transport_error_at_boot_fails_closed_instead_of_propagating(
    tmp_path: Path,
) -> None:
    """A hung socket / DNS failure is NOT a ``KalshiApiError`` — it used to
    escape the pass entirely. It must be handled the same way an unreachable
    exchange is: unproven, blocked, and RETURNED (not raised) so the boot reaches
    the preflight that refuses."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest([], list_raises=TimeoutError("connect timed out"))
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists()


async def test_a_transport_error_on_the_delete_is_not_proof(tmp_path: Path) -> None:
    """Same on the write half: a timeout carries no HTTP status at all, so it can
    never be mistaken for a 404."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest([_quote("q-left")], delete_raises=TimeoutError("no answer"))
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists()
    assert len(rest.deleted) == RECONCILE_RETRIES


async def test_the_positions_read_failing_is_also_unproven(tmp_path: Path) -> None:
    """The quotes came off, but the exposure book cannot be rehydrated. Quoting
    on top of positions we cannot see is exactly what the block exists to
    prevent."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest([_quote("q1")])

    async def _boom(**params: Any) -> dict[str, Any]:
        raise KalshiApiError(500, "server_error", "boom")

    rest.get_positions = _boom  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as cap:
        await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists()
    failed = next(e for e in cap if e["event"] == "startup_reconcile_failed")
    assert failed["phase"] == "positions"


async def test_the_withdrawal_is_wall_bounded_not_quote_count_bounded(
    tmp_path: Path,
) -> None:
    """TERMINATION, third bound. Many leftovers against a hung exchange must cost
    ONE wall deadline, not ``quotes x retries x timeout``. With a clock that jumps
    5 s per read, the 4 x 10 s deadline is reached long before 40 quotes x 4
    attempts = 160 DELETEs — and everything it never got to ask about is
    not-provably-gone, which fails closed."""
    app = _app_with_marker(tmp_path)
    app._clock = _JumpClock(5.0)  # type: ignore[assignment]
    rest = _LeftoverRest(
        [_quote(f"q{i}") for i in range(40)],
        delete_raises=KalshiApiError(503, "unavailable", "down"),
    )
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]

    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists()
    # It stopped on the WALL, nowhere near the per-quote worst case.
    assert 0 < len(rest.deleted) < 40 * RECONCILE_RETRIES


# --------------------------------------------------------------------------- #
# 5 — the bounded retry is real (proportionality: no human for one 429)
# --------------------------------------------------------------------------- #


async def test_a_transient_429_is_cured_by_the_bot_not_a_human(
    tmp_path: Path,
) -> None:
    """Fail-closed must be the TERMINAL state, not the first reaction. One 429
    that clears on the retry ends with the book PROVEN and the bot quoting — the
    whole point of bounding the consequence to the actual failure."""
    app = _app_with_marker(tmp_path)
    rest = _LeftoverRest(
        [_quote("q-left")],
        delete_raises=[RateLimitedError(429, "rate", "slow down"), None],
    )
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is True
    assert not (tmp_path / "needs_reconcile").exists()
    assert rest.deleted == ["q-left", "q-left"]     # asked twice, proven on the 2nd
