"""Regression tests for the confirmed findings of the final adversarial review."""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.reasons import ReasonCode
from combomaker.ops.persistence import Store
from combomaker.risk.limits import RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import JsonDict, TEST_CONVENTIONS, Rig, accepted_msg, rfq
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event


@pytest.fixture()
async def rig(tmp_path: Path) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return Rig(h, store)


def target_cost_rfq(dollars: str = "5000.00") -> object:
    return combo(CROSS_EVENT_LEGS, contracts_fp=None, target_cost_dollars=dollars)


# --- Finding 1 (critical): target-cost RFQs must enter risk at FULL size ----


async def test_target_cost_rfq_risk_sized_from_target_not_one_contract(rig: Rig) -> None:
    # $15 stays under the default 100-contract per-quote cap at a ~20c bid.
    await rig.lifecycle.handle_rfq(target_cost_rfq("15.00"))
    assert len(rig.sender.created) == 1
    quote_risk = rig.exposure.open_quotes["q1"]
    # $15 at the cheapest quoted side (< $1) must be > 15 contracts,
    # and enormously more than the old 1-contract placeholder.
    assert int(quote_risk.contracts) > 15 * 100


async def test_oversized_target_cost_rfq_blocked_by_limits(rig: Rig) -> None:
    # $5,000 target cost converts to thousands of contracts at a ~25c bid —
    # far beyond max_contracts_per_quote (100) — must never reach the wire.
    await rig.lifecycle.handle_rfq(target_cost_rfq("5000.00"))
    assert rig.sender.created == []


async def test_mass_acceptance_sees_target_cost_size(rig: Rig) -> None:
    # One $20 target-cost quote ≈ 95 contracts; its worst side (the expensive
    # NO bid) is ~$65 of notional — under a $100 gross limit the FIRST passes
    # and the SECOND must be refused by mass acceptance.
    rig.lifecycle._limits._limits = RiskLimits(  # noqa: SLF001 (test seam)
        max_contracts_per_quote=1_000.0,
        max_gross_notional_dollars=100.0,
    )
    await rig.lifecycle.handle_rfq(target_cost_rfq("20.00"))
    assert len(rig.sender.created) == 1
    second = combo(
        CROSS_EVENT_LEGS, id="rfq_2", contracts_fp=None, target_cost_dollars="20.00"
    )
    await rig.lifecycle.handle_rfq(second)
    assert len(rig.sender.created) == 1  # blocked by mass-acceptance gross notional


# --- Finding 2: unknown accepted size must lapse, never guess --------------

# Every wire field that can carry the accepted contract count (see
# lifecycle._accepted_qty). Tests strip ALL of them to simulate "no size".
_SIZE_FIELDS = ("contracts_accepted_fp", "no_contracts_offered_fp",
                "yes_contracts_offered_fp")


def _strip_size(msg: JsonDict) -> JsonDict:
    for k in _SIZE_FIELDS:
        msg.pop(k, None)
    return msg


async def test_missing_accepted_fp_on_target_cost_lapses(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(target_cost_rfq("15.00"))
    msg = _strip_size(accepted_msg("q1", "yes"))
    await rig.lifecycle.on_quote_accepted(msg)
    assert rig.sender.confirmed == []
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_SIZE_UNKNOWN}") == 1
    )


async def test_unparseable_accepted_fp_lapses_even_contracts_mode(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    msg = accepted_msg("q1", "yes")
    # Corrupt the accepted-count field: present-but-unparseable = lapse,
    # never fall through to another field and guess.
    msg["contracts_accepted_fp"] = "lots"
    await rig.lifecycle.on_quote_accepted(msg)
    assert rig.sender.confirmed == []


async def test_missing_accepted_fp_contracts_mode_falls_back_to_rfq_size(rig: Rig) -> None:
    # contracts-mode: the quote covers the RFQ's full size by wire contract —
    # that fallback is doc-anchored, not a guess.
    await rig.lifecycle.handle_rfq(rfq())
    msg = _strip_size(accepted_msg("q1", "yes"))
    await rig.lifecycle.on_quote_accepted(msg)
    assert rig.sender.confirmed == ["q1"]
    position = next(iter(rig.exposure.positions.values()))
    assert int(position.contracts) == 1_000  # the RFQ's 10.00 contracts


async def test_ground_truth_accept_fields_size_target_cost_rfq(rig: Rig) -> None:
    # REGRESSION for the 2026-07-14 fill-killer. On a TARGET-COST RFQ (95% of
    # live flow) the quote_accepted WS message has contracts_accepted_fp=null;
    # the accepted size is the contracts we OFFERED on the accepted side
    # (yes/no_contracts_offered_fp), verified against the live tape + the docs
    # (docs.kalshi.com/websockets/communications). The old code read only
    # contracts_accepted_fp and lapsed EVERY won auction. This replays the real
    # target-cost accept shape and asserts we size the fill from the offered
    # count and confirm it.
    await rig.lifecycle.handle_rfq(target_cost_rfq("15.00"))
    msg = {
        "quote_id": "q1",
        "rfq_id": "rfq_1",
        "accepted_side": "yes",
        "contracts_accepted_fp": None,          # null on a target-cost accept
        "yes_contracts_offered_fp": "51.00",    # our offered size = the fill
        "no_contracts_offered_fp": "0.00",
        "rfq_target_cost_dollars": "15.0000",
    }
    await rig.lifecycle.on_quote_accepted(msg)
    assert rig.sender.confirmed == ["q1"]
    position = next(iter(rig.exposure.positions.values()))
    assert int(position.contracts) == 5_100  # 51.00 contracts, from offered


# --- Findings 3+4: position booked at confirm; confirm failure parks state --


async def test_position_booked_at_confirm_not_at_execution(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    # BEFORE quote_executed arrives the fill is already visible to risk.
    assert "fill:q1" in rig.exposure.positions
    await rig.lifecycle.on_quote_executed({"quote_id": "q1"})
    assert len(rig.exposure.positions) == 1  # idempotent


async def test_failed_confirm_still_books_fill_if_execution_arrives(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    rig.sender.fail_confirm = True
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert "fill:q1" not in rig.exposure.positions  # not booked (may not have landed)
    # ...but if the exchange says it executed, the fill is booked, not lost.
    await rig.lifecycle.on_quote_executed({"quote_id": "q1"})
    assert "fill:q1" in rig.exposure.positions


async def test_repeated_confirm_failures_halt(rig: Rig) -> None:
    rig.sender.fail_confirm = True
    for i in range(3):
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"rfq_{i}"))
        await rig.lifecycle.on_quote_accepted(accepted_msg(f"q{i + 1}", "yes"))
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason == ReasonCode.HALT_CONFIRM_TIMEOUTS


# --- Finding 5: daily-loss limit actually binds ------------------------------


async def test_daily_loss_halts_via_maintenance(rig: Rig) -> None:
    rig.lifecycle._limits._limits = RiskLimits(max_daily_loss_dollars=1.0)  # noqa: SLF001
    rig.lifecycle.record_realized_pnl(-20_000)  # −$2 realized
    await rig.lifecycle.maintenance_tick()
    assert rig.killswitch.halted
    assert rig.killswitch.halt_event is not None
    assert rig.killswitch.halt_event.reason == ReasonCode.HALT_DAILY_LOSS


async def test_unrealized_mtm_feeds_daily_pnl(rig: Rig) -> None:
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    await rig.lifecycle.maintenance_tick()
    # marked at current mids: unrealized is now a real number (fair > bid at
    # quote time ⇒ positive mark right after the fill)
    assert rig.lifecycle.daily_pnl.unrealized_cc > 0


# --- Finding 7: NO-payout convention is consumed -----------------------------


async def test_no_side_accept_declined_when_complement_unverified(rig: Rig) -> None:
    unverified = Conventions(
        verified=True,
        source="test",
        maker_side_on_yes_accept=Side.YES,
        maker_side_on_no_accept=Side.NO,
        maker_pays_own_bid=True,
        maker_is_taker_on_fill=False,
        combo_no_pays_complement=None,  # ← Phase 2.5 hasn't measured it
    )
    rig.lifecycle._conventions = unverified  # noqa: SLF001 (test seam)
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "no"))
    assert rig.sender.confirmed == []
    assert (
        rig.metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CONVENTION_UNKNOWN}")
        == 1
    )
    # YES-side accepts are unaffected (payout convention for YES is the doc-
    # verified product itself)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_2"))
    await rig.lifecycle.on_quote_accepted(accepted_msg("q2", "yes"))
    assert rig.sender.confirmed == ["q2"]


def test_conventions_used_are_the_test_fixture() -> None:
    assert TEST_CONVENTIONS.combo_no_pays_complement is True
