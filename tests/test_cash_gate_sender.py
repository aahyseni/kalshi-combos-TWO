"""CashGateSender (2026-08-15 lever 4 — the cash-storm fix).

Pins: after an exchange insufficient_balance create refusal, further creates
are refused LOCALLY (CashGatedError, no HTTP) until the balance tracker
produces a NEW poll reading; one probe per fresh reading; success clears the
gate; delete/confirm/get are NEVER gated; non-balance errors never gate.
Measured incident this exists for: 225k insufficient_balance 400s/day at
7.2/s peak (2026-08-13), 181k on 2026-08-15 — discovery-by-erroring of a
fact the balance poll already knew.
"""
from __future__ import annotations

import pytest

from combomaker.core.money import CentiCents
from combomaker.exchange.rest import (
    INSUFFICIENT_BALANCE_CODE,
    CashGatedError,
    KalshiApiError,
)
from combomaker.ops.quote_app import CashGateSender

CC = CentiCents


class FakeBalance:
    def __init__(self) -> None:
        self.poll_ns: int | None = 1_000

    def last_poll_ns_or_none(self) -> int | None:
        return self.poll_ns


class FakeInner:
    """Scriptable inner sender: pop the next outcome per create call."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []
        self.create_calls = 0
        self.delete_calls = 0
        self.confirm_calls = 0

    async def create_quote(self, rfq_id, *, yes_bid_cc, no_bid_cc, rest_remainder=False):
        self.create_calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "no_cash":
            raise KalshiApiError(400, INSUFFICIENT_BALANCE_CODE, "insufficient balance")
        if outcome == "other":
            raise KalshiApiError(400, "invalid_parameters", "bad request")
        return {"quote": {"id": "q1"}}

    async def delete_quote(self, quote_id):
        self.delete_calls += 1
        return {}

    async def confirm_quote(self, quote_id):
        self.confirm_calls += 1
        return {}


def _mk() -> tuple[CashGateSender, FakeInner, FakeBalance]:
    inner = FakeInner()
    bal = FakeBalance()
    return CashGateSender(inner, bal), inner, bal  # type: ignore[arg-type]


async def _create(s: CashGateSender):
    return await s.create_quote("r1", yes_bid_cc=CC(0), no_bid_cc=CC(5000))


class TestCashGate:
    @pytest.mark.asyncio
    async def test_first_failure_passes_through_then_gates_locally(self):
        sender, inner, _bal = _mk()
        inner.outcomes = ["no_cash"]
        with pytest.raises(KalshiApiError) as e1:
            await _create(sender)
        assert e1.value.code == INSUFFICIENT_BALANCE_CODE  # the real 400
        # Same poll reading: locally refused, inner NOT called again.
        with pytest.raises(CashGatedError):
            await _create(sender)
        with pytest.raises(CashGatedError):
            await _create(sender)
        assert inner.create_calls == 1

    @pytest.mark.asyncio
    async def test_new_poll_reading_allows_exactly_one_probe(self):
        sender, inner, bal = _mk()
        inner.outcomes = ["no_cash", "no_cash"]
        with pytest.raises(KalshiApiError):
            await _create(sender)
        bal.poll_ns = 2_000  # fresh reading → one probe allowed
        with pytest.raises(KalshiApiError) as e:
            await _create(sender)
        assert e.value.code == INSUFFICIENT_BALANCE_CODE
        assert inner.create_calls == 2
        # Probe failed → re-gated on the NEW reading: local again.
        with pytest.raises(CashGatedError):
            await _create(sender)
        assert inner.create_calls == 2

    @pytest.mark.asyncio
    async def test_successful_probe_clears_the_gate(self):
        sender, inner, bal = _mk()
        inner.outcomes = ["no_cash"]  # then ok
        with pytest.raises(KalshiApiError):
            await _create(sender)
        bal.poll_ns = 2_000
        assert (await _create(sender))["quote"]["id"] == "q1"
        # Fully open: subsequent creates hit the exchange freely.
        await _create(sender)
        assert inner.create_calls == 3

    @pytest.mark.asyncio
    async def test_non_balance_errors_never_gate(self):
        sender, inner, _bal = _mk()
        inner.outcomes = ["other"]
        with pytest.raises(KalshiApiError) as e:
            await _create(sender)
        assert e.value.code == "invalid_parameters"
        await _create(sender)  # no gate — straight through
        assert inner.create_calls == 2

    @pytest.mark.asyncio
    async def test_delete_and_confirm_are_never_gated(self):
        sender, inner, _bal = _mk()
        inner.outcomes = ["no_cash"]
        with pytest.raises(KalshiApiError):
            await _create(sender)
        # Gated for creates — but risk-reducing/contractual paths flow.
        await sender.delete_quote("q1")
        await sender.confirm_quote("q1")
        assert inner.delete_calls == 1
        assert inner.confirm_calls == 1

    @pytest.mark.asyncio
    async def test_local_error_is_not_a_rate_limit(self):
        # CashGatedError must never feed the 429-burst breaker.
        from combomaker.exchange.rest import RateLimitedError

        err = CashGatedError()
        assert err.status == 0
        assert not isinstance(err, RateLimitedError)
