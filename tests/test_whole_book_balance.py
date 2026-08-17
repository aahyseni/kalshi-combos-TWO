"""WholeBookBalanceSource (operator constitutional ruling 2026-08-17):
shards are parts of ONE book/balance — the risk denominator must see total
cash AND total portfolio value across every exchange shard.

Live-verified facts pinned here: the exchange's ``balance`` is the
cross-shard total; ``portfolio_value`` is scoped to one shard per call
(idx0 $1,264.23 vs idx1 $4.06 measured 2026-08-17)."""
from __future__ import annotations

import pytest

from combomaker.ops.quote_app import WholeBookBalanceSource


class FakeRest:
    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.fail_on: int = -1  # -1 = never (None would collide with default)

    async def get_balance(self, exchange_index: int | None = None):
        self.calls.append(exchange_index)
        if exchange_index == self.fail_on:
            raise RuntimeError("shard fetch failed")
        if exchange_index is None:
            return {
                "balance": 388510,  # TOTAL cents across shards
                "portfolio_value": 126423,  # shard-0 scoped
                "balance_breakdown": [
                    {"balance": "1384.68", "exchange_index": 0},
                    {"balance": "2470.92", "exchange_index": 1},
                ],
            }
        return {"balance": 388510, "portfolio_value": 406}  # shard-1 PV


class TestWholeBook:
    @pytest.mark.asyncio
    async def test_pv_sums_across_shards_cash_stays_total(self):
        rest = FakeRest()
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["balance"] == 388510  # already total — untouched
        assert merged["portfolio_value"] == 126423 + 406  # SUMMED
        assert rest.calls == [None, 1]  # default + each extra shard once

    @pytest.mark.asyncio
    async def test_new_shard_in_breakdown_is_picked_up(self):
        rest = FakeRest()

        async def gb(exchange_index=None):
            rest.calls.append(exchange_index)
            if exchange_index is None:
                return {
                    "balance": 100,
                    "portfolio_value": 10,
                    "balance_breakdown": [
                        {"exchange_index": 0},
                        {"exchange_index": 1},
                        {"exchange_index": 2},  # a shard Kalshi adds later
                    ],
                }
            return {"portfolio_value": 5}

        rest.get_balance = gb  # type: ignore[assignment]
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["portfolio_value"] == 10 + 5 + 5
        assert rest.calls == [None, 1, 2]

    @pytest.mark.asyncio
    async def test_partial_read_never_masquerades_as_whole_book(self):
        # A failed shard fetch RAISES — the tracker's stale fail-closed then
        # governs; a shard-0-only reading must never be returned as total.
        rest = FakeRest()
        rest.fail_on = 1
        with pytest.raises(RuntimeError):
            await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
