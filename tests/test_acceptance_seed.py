"""Acceptance-tape boot seed (2026-08-13 â€” the 8/1 empty-tape defect).

Covers: additive/validated seeding on AcceptanceCounters; the extracted pure
sizing helpers' parity with the lifecycle arithmetic; the store round trip
(decisions + rfqs â†’ per-bucket counts through the REAL reader); fail-safe
behavior; and the arming property â€” live-shaped seeded counts flip
``discriminating()`` TRUE with every bucket's CP-lower > 0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.acceptance_seed import SeedResult, seed_counts_from_store
from combomaker.ops.persistence import Store
from combomaker.rfq.eviction_value import (
    N_BUCKETS,
    AcceptanceCounters,
    det_consumed_cc,
    risk_qty_from_terms,
    size_bucket,
)
from combomaker.rfq.lifecycle import QuoteLifecycle
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import LegRef, OpenQuoteRisk

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

# The live 8/13 24h probe table (238k sent / 82 matched accepts).
LIVE_QUOTED = [24_979, 41_377, 89_822, 69_086, 12_776]
LIVE_ACCEPTED = [22, 17, 23, 17, 3]


# --- AcceptanceCounters.seed_counts ------------------------------------------------


class TestSeedCounts:
    def test_additive_and_validating(self) -> None:
        tape = AcceptanceCounters()
        tape.record_quoted(10_000)   # bucket 0
        tape.record_accepted(10_000)
        tape.seed_counts([1] * N_BUCKETS, [1] * N_BUCKETS)
        b0 = tape.bounds(0, 0.02)
        assert b0 is not None
        assert b0.quoted == 2 and b0.accepted == 2  # added, never reset
        with pytest.raises(ValueError):
            tape.seed_counts([1] * (N_BUCKETS - 1), [1] * N_BUCKETS)
        with pytest.raises(ValueError):
            tape.seed_counts([1] * N_BUCKETS, [-1] * N_BUCKETS)

    def test_accepted_clamped_to_quoted(self) -> None:
        tape = AcceptanceCounters()
        tape.seed_counts([1] * N_BUCKETS, [5] * N_BUCKETS)
        for i in range(N_BUCKETS):
            b = tape.bounds(i, 0.02)
            assert b is not None and b.accepted <= b.quoted

    def test_live_shaped_seed_arms_the_measured_credit(self) -> None:
        # The arming property: the seeded tape discriminates from boot and
        # every bucket carries a positive measured CP-lower â€” the marginal
        # gates' P(accept) credit is non-zero on day one.
        tape = AcceptanceCounters()
        tape.seed_counts(LIVE_QUOTED, LIVE_ACCEPTED)
        assert tape.discriminating(0.02) is True
        for i in range(N_BUCKETS):
            b = tape.bounds(i, 0.02)
            assert b is not None
            assert b.lower > 0.0
            assert b.lower <= b.p_hat <= b.upper  # exact-regime sanity (G3)


# --- pure-helper parity with the lifecycle arithmetic ------------------------------


class TestSizingParity:
    def test_det_consumed_matches_lifecycle_static(self) -> None:
        for contracts, yes, no in (
            (10_000, 2_000, 8_000),
            (27_933, 0, 8_210),
            (100, 9_900, 0),
            (5_000, 0, 0),
        ):
            quote = OpenQuoteRisk(
                quote_id="q",
                rfq_id="r",
                combo_ticker="C",
                collection=None,
                yes_bid_cc=CentiCents(yes),
                no_bid_cc=CentiCents(no),
                contracts=CentiContracts(contracts),
                legs=(LegRef("A", "E-G", "yes"),),
            )
            assert QuoteLifecycle._quote_det_consumed_cc(quote) == det_consumed_cc(
                contracts, yes, no
            )

    def test_risk_qty_terms_pin_the_audited_forms(self) -> None:
        # contracts-mode passes through
        assert risk_qty_from_terms(12_345, None, 100, 200, award_sizing=True) == 12_345
        # award form: $50 @ no_bid 82.10c -> 279.33ct (the audited example)
        assert risk_qty_from_terms(None, 500_000, 0, 8_210, award_sizing=True) == 27_933
        # award form unresolvable at bid >= 99c (denominator < 100)
        assert risk_qty_from_terms(None, 500_000, 0, 9_950, award_sizing=True) is None
        # legacy form: cheapest side's own bid, floored at 1c
        assert risk_qty_from_terms(None, 500_000, 2_000, 8_000, award_sizing=False) == -(
            -500_000 * 100 // 2_000
        )
        # zero-bid sides filtered; no sides -> None
        assert risk_qty_from_terms(None, 500_000, 0, 0, award_sizing=False) is None


# --- store round trip ---------------------------------------------------------------


def rfq_msg(rid: str, *, contracts: str | None, target: str | None) -> dict:
    msg: dict = {
        "id": rid,
        "market_ticker": f"KXMVE-{rid}",
        "created_ts": "2026-08-13T11:00:00Z",
        "mve_collection_ticker": "KXMVE",
        "mve_selected_legs": [
            {"market_ticker": "A", "side": "yes", "event_ticker": "E-G"}
        ],
    }
    if contracts is not None:
        msg["contracts_fp"] = contracts
    if target is not None:
        msg["target_cost_dollars"] = target
    return msg


async def seeded_store(tmp_path: Path) -> Path:
    clock = FakeClock(start=NOW)
    db = tmp_path / "t.sqlite3"
    store = await Store.open(db, clock)
    # rfq r1: contracts-mode 100ct; rfq r2: target-cost $50
    await store.record_rfq(Rfq.from_ws(rfq_msg("r1", contracts="100.00", target=None)), source="test")
    await store.record_rfq(Rfq.from_ws(rfq_msg("r2", contracts=None, target="50.00")), source="test")
    await store.record_decision(
        "quote_sent", "r1",
        ["quote_sent"],
        {"quote_id": "q1", "yes_bid_cc": 2_000, "no_bid_cc": 2_000, "fair_cc": 0},
    )
    await store.record_decision(
        "quote_sent", "r2",
        ["quote_sent"],
        {"quote_id": "q2", "yes_bid_cc": 0, "no_bid_cc": 8_210, "fair_cc": 0},
    )
    # malformed context: counted unjoinable, never guessed
    await store.record_decision("quote_sent", "r1", ["quote_sent"], {"quote_id": "q3"})
    # missing rfqs row: counted unjoinable
    await store.record_decision(
        "quote_sent", "r-missing",
        ["quote_sent"],
        {"quote_id": "q4", "yes_bid_cc": 100, "no_bid_cc": 100, "fair_cc": 0},
    )
    # accepts: q1 confirmed, q2 last-look-declined (BOTH count), one unmatched
    await store.record_decision("confirm", "r1", ["ok"], {"quote_id": "q1"})
    await store.record_decision("decline", "r2", ["lastlook"], {"quote_id": "q2"})
    await store.record_decision("confirm", "rX", ["ok"], {"quote_id": "q-neverseen"})
    await store.close()
    return db


async def test_store_round_trip_buckets_and_diagnostics(tmp_path: Path) -> None:
    db = await seeded_store(tmp_path)
    res = seed_counts_from_store(db, now_utc=NOW, award_sizing=True)
    assert isinstance(res, SeedResult)
    # r1: 100ct at 20c/20c -> det 200,000cc -> bucket 2
    b1 = size_bucket(det_consumed_cc(10_000, 2_000, 2_000))
    # r2: award 279.33ct @ 82.10c -> det 2,293,299cc -> top bucket
    q2 = risk_qty_from_terms(None, 500_000, 0, 8_210, award_sizing=True)
    assert q2 is not None
    b2 = size_bucket(det_consumed_cc(q2, 0, 8_210))
    expect_quoted = [0] * N_BUCKETS
    expect_quoted[b1] += 1
    expect_quoted[b2] += 1
    assert res.quoted == expect_quoted
    expect_accepted = [0] * N_BUCKETS
    expect_accepted[b1] += 1
    expect_accepted[b2] += 1
    assert res.accepted == expect_accepted
    assert res.unjoinable == 2          # malformed ctx + missing rfqs row
    assert res.accepts_unmatched == 1   # q-neverseen
    assert res.rows_scanned == 4


async def test_window_excludes_old_rows(tmp_path: Path) -> None:
    db = await seeded_store(tmp_path)
    # Pretend the boot happens 3 days later: every row is outside the 24h
    # window â‡’ empty (honest) seed, not an error.
    later = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    res = seed_counts_from_store(db, now_utc=later, award_sizing=True)
    assert isinstance(res, SeedResult)
    assert res.quoted == [0] * N_BUCKETS
    assert res.accepted == [0] * N_BUCKETS


def test_missing_db_fails_safe_to_none(tmp_path: Path) -> None:
    assert (
        seed_counts_from_store(
            tmp_path / "nope.sqlite3", now_utc=NOW, award_sizing=True
        )
        is None
    )
