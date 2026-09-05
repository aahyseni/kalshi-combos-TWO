"""WholeBookBalanceSource (operator constitutional ruling 2026-08-17):
shards are parts of ONE book/balance — the risk denominator must see total
cash AND total portfolio value across every exchange shard.

Live-verified facts pinned here:
* the exchange's ``balance`` is the cross-shard total;
* ``portfolio_value`` WITH an explicit ``exchange_index`` is scoped to that
  shard (idx0 $1,264.23 vs idx1 $4.06 measured 2026-08-17);
* the BASE call's ``portfolio_value`` (no index) is NOT shard 0's — measured
  2026-09-05 22:22Z in the same minute: base $1,550.55, idx0 $0.00, idx1
  $1,551.38. The pre-repair merge (base + every non-zero index) therefore
  double-counted shard 1 and the bot's standing read $6,665.82 against the
  exchange's $5,061.34 at identical cash. The merge now sums the per-shard
  scoped reads over EVERY index in ``balance_breakdown`` and never adds the
  base PV."""
from __future__ import annotations

import pytest

from combomaker.ops.quote_app import WholeBookBalanceSource


class FakeRest:
    """Mirrors the 2026-09-05 live payloads (cents)."""

    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.fail_on: int = -1  # -1 = never (None would collide with default)
        self.per_shard_pv: dict[int, int] = {0: 0, 1: 155138, 2: 0, 3: 0}

    async def get_balance(self, exchange_index: int | None = None):
        self.calls.append(exchange_index)
        if exchange_index == self.fail_on:
            raise RuntimeError("shard fetch failed")
        if exchange_index is None:
            return {
                "balance": 351010,  # TOTAL cents across shards
                "portfolio_value": 155055,  # the WHOLE book already (not idx0)
                "balance_breakdown": [
                    {"balance": "302.0675", "exchange_index": 0},
                    {"balance": "3208.0358", "exchange_index": 1},
                    {"balance": "0.0000", "exchange_index": 2},
                    {"balance": "0.0000", "exchange_index": 3},
                ],
            }
        return {"balance": 351010, "portfolio_value": self.per_shard_pv[exchange_index]}


class TestWholeBook:
    @pytest.mark.asyncio
    async def test_pv_is_the_sum_of_per_shard_reads_cash_stays_total(self):
        rest = FakeRest()
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["balance"] == 351010  # already total — untouched
        assert merged["portfolio_value"] == 0 + 155138 + 0 + 0  # per-shard SUM
        assert rest.calls == [None, 0, 1, 2, 3]  # default + EVERY shard once

    @pytest.mark.asyncio
    async def test_base_pv_is_never_added_to_the_shard_sum(self):
        # The 2026-09-05 defect: base (whole book) + idx1 (whole book again).
        rest = FakeRest()
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["portfolio_value"] != 155055 + 155138
        assert abs(merged["portfolio_value"] - 155055) < 100  # ≈ the exchange's own total

    @pytest.mark.asyncio
    async def test_positions_on_shard_zero_only_still_sum_correctly(self):
        # The 2026-08-17 shape: idx0 carries the book, idx1 a sliver.
        rest = FakeRest()
        rest.per_shard_pv = {0: 126423, 1: 406, 2: 0, 3: 0}
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["portfolio_value"] == 126423 + 406

    @pytest.mark.asyncio
    async def test_new_shard_in_breakdown_is_picked_up(self):
        rest = FakeRest()

        async def gb(exchange_index=None):
            rest.calls.append(exchange_index)
            if exchange_index is None:
                return {
                    "balance": 100,
                    "portfolio_value": 15,
                    "balance_breakdown": [
                        {"exchange_index": 0},
                        {"exchange_index": 1},
                        {"exchange_index": 2},  # a shard Kalshi adds later
                    ],
                }
            return {"portfolio_value": 5}

        rest.get_balance = gb  # type: ignore[assignment]
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["portfolio_value"] == 5 + 5 + 5
        assert rest.calls == [None, 0, 1, 2]

    @pytest.mark.asyncio
    async def test_unsharded_account_keeps_the_base_pv(self):
        rest = FakeRest()

        async def gb(exchange_index=None):
            rest.calls.append(exchange_index)
            assert exchange_index is None
            return {"balance": 100, "portfolio_value": 42, "balance_breakdown": []}

        rest.get_balance = gb  # type: ignore[assignment]
        merged = await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
        assert merged["portfolio_value"] == 42
        assert rest.calls == [None]

    @pytest.mark.asyncio
    async def test_partial_read_never_masquerades_as_whole_book(self):
        # A failed shard fetch RAISES — the tracker's stale fail-closed then
        # governs; a partial reading must never be returned as total.
        rest = FakeRest()
        rest.fail_on = 1
        with pytest.raises(RuntimeError):
            await WholeBookBalanceSource(rest).get_balance()  # type: ignore[arg-type]
