"""The ONE observed fee schedule every FeeModel in the process shares
(2026-09-04 fee-seam repair): loaded from ``data_dir/fee_schedule_observed.json``
before the engine is built, handed to the pricing engine AND the lifecycle
ledger/waiver, and refit in place by the lifecycle's fee-observer sweep.

Two builders used to hold two frozen copies of a yaml number the exchange had
already changed; now there is one live object and the yaml carries no maker
coefficient at all (``FeeConfig``). Operator overrides are LOGGED here at
boot so a stale override cannot hide.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from combomaker.ops.config import FeeConfig
from combomaker.ops.logging import get_logger
from combomaker.pricing.fee_observer import FeeScheduleSummary, ObservedFeeSchedule

log = get_logger(__name__)

FEE_SCHEDULE_FILENAME = "fee_schedule_observed.json"


def fee_schedule_path(data_dir: Path) -> Path:
    return Path(data_dir) / FEE_SCHEDULE_FILENAME


def load_observed_fee_schedule(fee_cfg: FeeConfig, data_dir: Path) -> ObservedFeeSchedule:
    """Load the persisted measurement (a relight prices with the measured
    schedule before its first REST poll); a missing/corrupt file is a COLD
    schedule (taker-conservative bootstrap). Overrides are logged."""
    seed = ObservedFeeSchedule.from_config_values(
        taker_coef=fee_cfg.taker_coef,
        maker_coef_override=fee_cfg.maker_coef_override,
        override_prefixes=fee_cfg.maker_fee_active_prefixes,
    )
    sched = ObservedFeeSchedule.load(
        fee_schedule_path(data_dir),
        taker_coef=seed.taker_coef,
        maker_coef_override=seed.maker_coef_override,
        override_prefixes=seed.override_prefixes,
    )
    if fee_cfg.maker_coef_override is not None or fee_cfg.maker_fee_active_prefixes:
        log.warning(
            "fee_schedule_override",
            maker_coef_override=fee_cfg.maker_coef_override,
            maker_fee_active_prefixes=list(fee_cfg.maker_fee_active_prefixes),
            in_force=sched.maker_coef_source == "override",
            detail="operator OVERRIDE of the measured fee schedule — a fitted "
            "measurement always outranks the coefficient override; prefixes "
            "force the maker fee type regardless of observation (tech debt to "
            "dissolve once the observer has covered every collection)",
        )
    log.info(
        "fee_schedule_loaded",
        path=str(fee_schedule_path(data_dir)),
        mode=fee_cfg.mode,
        **asdict(FeeScheduleSummary.of(sched)),
    )
    return sched
