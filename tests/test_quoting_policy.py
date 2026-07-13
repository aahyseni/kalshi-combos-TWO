"""Phase 5 lifecycle integration — skew dark-ship, widen-vs-decline shadow/
enabled, and pregame flow-loss (time_to_start) logging.

The DARK-SHIP guarantee is the headline: with the skew wired but disabled, the
emitted quote is BIT-IDENTICAL to the no-skew quote (zero live behaviour change).
Enabling it shades the NO bid; enabling the widen policy declines near a cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.risk.skew import SkewLimits, SkewParams, WidenPolicyParams
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, rfq
from tests.test_pricing_engine import seed_event

JsonDict = dict[str, Any]

# Delta cap tiny so a modest book saturates utilisation (the skew/widen bite).
SKEW_LIMITS = SkewLimits(
    max_event_delta_contracts=5.0,
    max_event_worst_case_loss_dollars=10.0,
    max_event_gross_notional_dollars=50.0,
)


class PolicyRig:
    def __init__(
        self,
        h: Harness,
        store: Store,
        *,
        skew_params: SkewParams | None = None,
        widen_params: WidenPolicyParams | None = None,
        filters: FiltersConfig | None = None,
    ) -> None:
        self.h = h
        self.sender = FakeSender()
        self.exposure = ExposureBook(TEST_CONVENTIONS)
        self.metrics = Metrics()
        engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=engine,
            rfq_filter=RfqFilter(
                (filters or FiltersConfig(min_time_to_close_s=0.0)).model_copy(
                    update={"allowed_leg_series_prefixes": None}
                ),
                h.feed, h.metadata, h.killswitch, h.clock,
            ),
            limits=LimitChecker(RiskLimits()),
            exposure=self.exposure,
            feed=h.feed,
            metadata=h.metadata,
            inplay=InPlayDetector(h.clock),
            killswitch=h.killswitch,
            conventions=TEST_CONVENTIONS,
            store=store,
            metrics=self.metrics,
            lastlook_policy=LastLookPolicy(),
            config=LifecycleConfig(quote_ttl_s=30.0, reprice_threshold_cc=100),
            skew_params=skew_params,
            skew_limits=SKEW_LIMITS if skew_params is not None else None,
            widen_params=widen_params,
        )


async def _harness() -> Harness:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    return h


async def _decisions(store: Store) -> list[tuple[str, list[str], JsonDict]]:
    rows: list[tuple[str, list[str], JsonDict]] = []
    async with store._db.execute(  # noqa: SLF001 (test read-back seam)
        "SELECT kind, reasons_json, context_json FROM decisions ORDER BY id"
    ) as cursor:
        async for kind, reasons_json, context_json in cursor:
            rows.append((kind, json.loads(reasons_json), json.loads(context_json)))
    return rows


# ---------------------------------------------------------------------------
# DARK SHIP: disabled skew ⇒ bit-identical quote to no-skew-at-all.
# ---------------------------------------------------------------------------


async def test_disabled_skew_quote_identical_to_baseline(tmp_path: Path) -> None:
    h = await _harness()
    store = await Store.open(tmp_path / "a.sqlite3", h.clock)
    baseline = PolicyRig(h, store)  # no skew wired at all
    await baseline.lifecycle.handle_rfq(rfq())
    base_quote = baseline.sender.created[0]

    h2 = await _harness()
    store2 = await Store.open(tmp_path / "b.sqlite3", h2.clock)
    dark = PolicyRig(h2, store2, skew_params=SkewParams(enabled=False))
    await dark.lifecycle.handle_rfq(rfq())
    dark_quote = dark.sender.created[0]

    assert dark_quote["yes"] == base_quote["yes"]
    assert dark_quote["no"] == base_quote["no"]  # zero live behaviour change


async def test_disabled_skew_quotes_despite_concentrated_book(tmp_path: Path) -> None:
    h = await _harness()
    store = await Store.open(tmp_path / "c.sqlite3", h.clock)
    dark = PolicyRig(h, store, skew_params=SkewParams(enabled=False))
    # A concentrated book makes the honest skew non-zero, but dark ⇒ applied 0,
    # so the quote still goes out unchanged (no widen policy wired either).
    dark.exposure.positions.update(_big_book_positions())
    await dark.lifecycle.handle_rfq(rfq())
    assert len(dark.sender.created) == 1


# ---------------------------------------------------------------------------
# ENABLED widen policy declines near a cap; SHADOW does not.
# ---------------------------------------------------------------------------


async def test_widen_shadow_still_quotes(tmp_path: Path) -> None:
    h = await _harness()
    store = await Store.open(tmp_path / "d.sqlite3", h.clock)
    rig = PolicyRig(
        h, store,
        skew_params=SkewParams(enabled=False),
        widen_params=WidenPolicyParams(enabled=False),
    )
    rig.exposure.positions.update(_big_book_positions())
    await rig.lifecycle.handle_rfq(rfq())
    assert len(rig.sender.created) == 1  # shadow: quote still goes out


async def test_widen_enabled_declines_near_cap(tmp_path: Path) -> None:
    h = await _harness()
    store = await Store.open(tmp_path / "e.sqlite3", h.clock)
    rig = PolicyRig(
        h, store,
        skew_params=SkewParams(enabled=True),
        widen_params=WidenPolicyParams(enabled=True),
    )
    rig.exposure.positions.update(_big_book_positions())
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []  # declined rather than posting a wide quote
    rows = await _decisions(store)
    assert any(
        str(ReasonCode.SKIP_WIDEN_AVOIDED) in reasons for _, reasons, _ in rows
    )


# ---------------------------------------------------------------------------
# Flow-loss: time_to_start logged on a pregame decline.
# ---------------------------------------------------------------------------


async def test_time_to_start_logged_on_pregame_decline(tmp_path: Path) -> None:
    h = await _harness()
    store = await Store.open(tmp_path / "f.sqlite3", h.clock)
    # Pregame gate ON (default), estimate says started: close 2h out − 4.5h < now.
    rig = PolicyRig(h, store, filters=FiltersConfig())
    h.with_meta("M1", close_in_s=7200.0)  # started by estimate
    h.with_meta("M2", close_in_s=7200.0)
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []
    rows = await _decisions(store)
    pregame_rows = [
        ctx
        for _, reasons, ctx in rows
        if str(ReasonCode.SKIP_INPLAY_LEG) in reasons
    ]
    assert pregame_rows
    assert "time_to_start_s" in pregame_rows[0]  # flow-loss datum recorded


# ---------------------------------------------------------------------------
# helper: seed a big book on games E1/E2 so the candidate (M1 yes E1, M2 no E2)
# concentrates into an already-loaded game (E1), saturating utilisation.
# ---------------------------------------------------------------------------


def _big_book_positions() -> dict[str, OpenPosition]:
    # Modest book: saturates the TINY SKEW_LIMITS (delta cap 5 contracts) so the
    # skew/widen policies bite, while staying comfortably under the ENFORCED
    # RiskLimits (event delta 500, gross notional $5,000) so the quote itself is
    # not blocked by a real cap first. 3 positions × 20 contracts on E1/E2 ⇒
    # game delta ~tens of contracts (>> 5, << 500), premium $30 (<< $5,000).
    positions: dict[str, OpenPosition] = {}
    for i in range(3):
        positions[f"big{i}"] = OpenPosition(
            position_id=f"big{i}",
            combo_ticker="COMBO",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(2_000),  # 20 contracts
            entry_price_cc=CentiCents(5_000),
            legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "yes")),
        )
    return positions
