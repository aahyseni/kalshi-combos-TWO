"""F1 monotone pre-pricing gate (throughput synthesis 2026-07-16).

Prototype-first per hard rule 8: the gate logic was validated in
``tools/proto_pre_pricing_gate.py`` (5,000-case monotonicity fuzz against the
live checker — 0 violations; constructed counterexamples for every excluded
reason; live-tape replay; part-D port parity). Pinned here:

- config wiring: default OFF everywhere = today's behaviour byte-identical;
  the YAML knob reaches the LifecycleConfig;
- gate ON + already-breached monotone cap: SAME reason code, earlier exit —
  pricing never runs, the "pre_pricing" stage rides the decision context;
- gate OFF on the same book: prices first, then declines with the same reason
  (the same-decline-different-stage invariant, both directions);
- cent parity: with headroom, the gate-ON quote is identical to the cent;
- cache: a book mutation (generation bump) invalidates — no stale false skip;
- adversarial: SHADOW breaches and non-allowlisted reasons can never
  pre-decline (the pure filter is pinned directly).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import RiskConfig
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import build_lifecycle_config
from combomaker.rfq.lifecycle import LifecycleConfig
from combomaker.risk.limits import (
    PRE_PRICING_MONOTONE_REASONS,
    Breach,
    RiskLimits,
    monotone_pre_quote_breaches,
)
from tests.test_filters import Harness
from tests.test_lifecycle import Rig, rfq
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

GATE_ON = LifecycleConfig(
    quote_ttl_s=30.0, reprice_threshold_cc=100, pre_pricing_gate_enabled=True
)


async def make_rig(
    tmp_path: Path,
    *,
    name: str = "t.sqlite3",
    risk_limits: RiskLimits | None = None,
    lifecycle_config: LifecycleConfig | None = None,
) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / name, h.clock)
    return Rig(h, store, risk_limits=risk_limits, lifecycle_config=lifecycle_config)


def count_pricing(rig: Rig) -> dict[str, int]:
    calls = {"n": 0}
    orig = rig.lifecycle._price_async  # noqa: SLF001

    async def counting(rfq_: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return await orig(rfq_, **kw)

    rig.lifecycle._price_async = counting  # type: ignore[method-assign]  # noqa: SLF001
    return calls


# --------------------------------------------------------------- config wiring


def test_flag_defaults_off_everywhere() -> None:
    assert RiskConfig().pre_pricing_gate_enabled is False
    assert LifecycleConfig().pre_pricing_gate_enabled is False
    assert build_lifecycle_config(RiskConfig()).pre_pricing_gate_enabled is False


def test_yaml_knob_reaches_lifecycle_config() -> None:
    cfg = build_lifecycle_config(RiskConfig(pre_pricing_gate_enabled=True))
    assert cfg.pre_pricing_gate_enabled is True


# ------------------------------------------------------------ gate behaviour


async def test_gate_off_default_never_consults_the_gate(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.lifecycle.handle_rfq(rfq())
    assert len(rig.sender.created) == 1
    assert rig.metrics.counter("pre_gate.check") == 0
    assert rig.metrics.counter("pre_gate.declined") == 0


async def test_gate_on_pre_declines_same_reason_before_pricing(
    tmp_path: Path,
) -> None:
    # max_open_quotes=0 breaches candidate-free with adding_quote=True on an
    # EMPTY book — the top-2 measured decline reason, now caught pre-pricing.
    rig = await make_rig(
        tmp_path,
        risk_limits=RiskLimits(max_open_quotes=0),
        lifecycle_config=GATE_ON,
    )
    calls = count_pricing(rig)
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []
    assert calls["n"] == 0                    # pricing work never spent
    assert rig.metrics.counter("pre_gate.declined") == 1
    store = rig.lifecycle._store  # noqa: SLF001
    counts = await store.decision_reason_counts()
    assert counts.get(str(ReasonCode.SKIP_MAX_OPEN_QUOTES)) == 1  # SAME code


async def test_gate_off_same_book_prices_then_declines_same_reason(
    tmp_path: Path,
) -> None:
    # The other half of the same-decline-different-stage invariant: gate OFF on
    # the identical book still declines with the identical reason — but only
    # AFTER paying for the pricing.
    rig = await make_rig(tmp_path, risk_limits=RiskLimits(max_open_quotes=0))
    calls = count_pricing(rig)
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []
    assert calls["n"] == 1                    # priced first (today's waste)
    store = rig.lifecycle._store  # noqa: SLF001
    counts = await store.decision_reason_counts()
    assert counts.get(str(ReasonCode.SKIP_MAX_OPEN_QUOTES)) == 1


async def test_gate_on_with_headroom_quote_cent_identical_to_gate_off(
    tmp_path: Path,
) -> None:
    rig_off = await make_rig(tmp_path, name="off.sqlite3")
    rig_on = await make_rig(tmp_path, name="on.sqlite3", lifecycle_config=GATE_ON)
    await rig_off.lifecycle.handle_rfq(rfq())
    await rig_on.lifecycle.handle_rfq(rfq())
    assert len(rig_off.sender.created) == len(rig_on.sender.created) == 1
    assert rig_off.sender.created[0]["yes"] == rig_on.sender.created[0]["yes"]
    assert rig_off.sender.created[0]["no"] == rig_on.sender.created[0]["no"]
    assert rig_on.metrics.counter("pre_gate.check") == 1  # gate ran, passed


async def test_cache_invalidated_by_book_mutation_no_stale_false_skip(
    tmp_path: Path,
) -> None:
    # Cap 1: first RFQ quotes (gate passes on the empty book). A second RFQ
    # pre-declines (1 open + 1 > 1). After cancel-all (generation bump) a third
    # RFQ must quote again — a stale cached verdict may never keep declining.
    rig = await make_rig(
        tmp_path, risk_limits=RiskLimits(max_open_quotes=1), lifecycle_config=GATE_ON
    )
    await rig.lifecycle.handle_rfq(rfq())
    assert len(rig.sender.created) == 1
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_2"))
    assert len(rig.sender.created) == 1       # pre-declined at the cap
    assert rig.metrics.counter("pre_gate.declined") == 1
    await rig.lifecycle.cancel_all("test_free_slot")
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_3"))
    assert len(rig.sender.created) == 2       # slot freed ⇒ quoted again


async def test_cache_hit_within_ttl_and_same_generation(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path, risk_limits=RiskLimits(max_open_quotes=0), lifecycle_config=GATE_ON
    )
    await rig.lifecycle.handle_rfq(rfq())
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_2"))
    # Same generation (nothing mutated the book) + inside the 0.5s bound (the
    # test clock does not advance): one real check, one cache hit, two declines.
    assert rig.metrics.counter("pre_gate.check") == 1
    assert rig.metrics.counter("pre_gate.cache_hit") == 1
    assert rig.metrics.counter("pre_gate.declined") == 2


# ------------------------------------------------- adversarial: the pure filter


def test_adversarial_shadow_and_non_allowlisted_never_pre_decline() -> None:
    # A SHADOW breach can never pre-decline even on an allowlisted reason (the
    # shadow guarantee), and an ENFORCED breach on a non-monotone reason
    # (directional / slate / mass-acceptance / CVaR) can never pre-decline
    # either — the exact false-skip vectors the prototype's counterexamples
    # constructed (tools/proto_pre_pricing_gate.py part B).
    shadow_allowlisted = Breach(
        ReasonCode.SKIP_GAME_LOSS_CAP, "shadow game cap", shadow=True
    )
    enforced_excluded = [
        Breach(ReasonCode.SKIP_DIRECTIONAL_CAP, "hedgeable"),
        Breach(ReasonCode.SKIP_SLATE_CAP, "re-bucketable"),
        Breach(ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH, "delta axis"),
        Breach(ReasonCode.SKIP_PORTFOLIO_CVAR, "credit path"),
        Breach(ReasonCode.SKIP_PER_COMBO_LOSS_CAP, "candidate-only"),
        Breach(ReasonCode.HALT_DAILY_LOSS, "maintenance-owned"),
    ]
    keep = Breach(ReasonCode.SKIP_MAX_OPEN_QUOTES, "at cap")
    out = monotone_pre_quote_breaches(
        [shadow_allowlisted, *enforced_excluded, keep]
    )
    assert out == [keep]
    # And the allowlist itself is exactly the prototype-validated set.
    assert PRE_PRICING_MONOTONE_REASONS == frozenset(
        {
            ReasonCode.SKIP_MAX_OPEN_QUOTES,
            ReasonCode.SKIP_GAME_LOSS_CAP,
            ReasonCode.SKIP_UTILIZATION_BACKSTOP,
            ReasonCode.SKIP_BANKROLL_UNAVAILABLE,
        }
    )
