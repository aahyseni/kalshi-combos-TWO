"""RESTART GATE 2 — prove BY EXECUTION that the post-fix tree will QUOTE tonight.

Operator standing rule (2026-07-23): "a risk cap must be proven to produce a
non-zero quote against real trade sizes BEFORE going live".

READ-ONLY BY CONSTRUCTION:
  * the live store is opened ``mode=ro`` (aiosqlite URI) — the rehydrator's
    ``ensure_open_position_row`` write therefore RAISES and is swallowed by its
    own best-effort try/except (exactly the live fail-safe path).
  * the lifecycle's decision tape is pointed at a THROWAWAY scratch DB.
  * the quote sender is ``PaperSender`` — nothing is ever POSTed.
  * exchange calls are GETs only (positions / markets / balance / rfqs) plus a
    read-only orderbook WS subscription.

Hard rule 8: every number comes from the LIVE modules, never a reimplementation.
The rehydration is literally ``QuoteApp._rehydrate_exposure_book`` and
``QuoteApp._arm_rehydrated_legs`` called against a minimal shim self.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from combomaker.core.clock import SystemClock
from combomaker.core.conventions import load_conventions
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.exchange.ws import WsManager
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.marketdata.settled import SettledMarginalResolver
from combomaker.ops.config import load_config
from combomaker.ops.dotenv import load_dotenv
from combomaker.ops.logging import configure_logging, get_logger
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import PaperSender, QuoteApp
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.grouping import game_key
from combomaker.pricing.legtypes import set_pricing_aliases
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import QuoteLifecycle
from combomaker.rfq.models import Rfq
from combomaker.rfq.schedule import ScheduleCache
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, StarvationWatchdog, threshold_cc
from combomaker.risk.quarantine import MarketQuarantine
from combomaker.risk.reservation import RiskReservationService
from combomaker.risk.skew import SkewLimits, SkewParams, WidenPolicyParams
from combomaker.sim.structural_book import StructuralConfigView
from combomaker.sim.within_game_rho import sgp_within_game_rho_provider

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
LIVE_DB = Path("data/combomaker-prod-live-wc.sqlite3")
CFG = Path("config/prod-live-wc.local.yaml")


def d(cc: float | int | None) -> str:
    if cc is None:
        return "n/a"
    return f"${float(cc) / 10_000.0:,.2f}"


class _Shim:
    """The only ``self`` state ``_rehydrate_exposure_book`` /
    ``_arm_rehydrated_legs`` / ``_ensure_watched`` touch."""

    def __init__(self, cfg: Any = None) -> None:
        self._watched: set[str] = set()
        self._collection_grid: dict[str, Any] = {}
        self._config = cfg


@dataclass
class Wired:
    cfg: Any
    clock: Any
    rest: KalshiRestClient
    feed: OrderbookFeed
    metadata: MetadataCache
    exposure: ExposureBook
    limits: LimitChecker
    lifecycle: QuoteLifecycle
    reservation: RiskReservationService
    balance: BalanceTracker
    book_ws: WsManager
    scratch: Store


async def wire(scratch_dir: Path) -> Wired:
    load_dotenv()
    cfg = load_config(CFG, env="prod", mode="quote", confirm_live=True)
    clock = SystemClock()
    conventions = load_conventions()
    conventions.require_verified()
    set_pricing_aliases(dict(cfg.pricing.leg_pricing_aliases))
    metrics = Metrics(clock)

    signer = RequestSigner(Credentials.for_env("prod"), clock)
    rest = KalshiRestClient(cfg.endpoints.rest_base_url, signer)
    await rest.__aenter__()

    book_ws = WsManager(cfg.endpoints.ws_url, signer, clock, metrics)
    feed = OrderbookFeed(book_ws, clock, metrics)
    book_ws.start()

    metadata = MetadataCache(rest, clock)
    metadata.load_persisted(str(cfg.data_dir / "metadata_cache.json"))

    engine = PricingEngine(feed, metadata, conventions, cfg.pricing)
    exposure = ExposureBook(conventions, is_me_event=metadata.event_mutually_exclusive)
    risk_cfg = cfg.risk
    base_limits = risk_cfg.to_risk_limits()
    limits = LimitChecker(base_limits)  # adaptive_caps_mode=shadow ⇒ no swap (live)

    balance = BalanceTracker(conventions, clock, stale_after_s=600.0)
    await balance.refresh(rest)

    killswitch = KillSwitch(clock, kill_file=cfg.kill_file)
    schedule = ScheduleCache(
        {
            ev: datetime.fromisoformat(raw)
            for ev, raw in cfg.filters.pregame_scheduled_starts.items()
        }
    )
    rfq_filter = RfqFilter(
        cfg.filters, feed, metadata, killswitch, clock, schedule, MarketQuarantine()
    )

    skew_cfg = cfg.pricing.skew
    widen_cfg = cfg.pricing.widen
    skew_params = SkewParams(
        w_conc=skew_cfg.w_conc,
        w_off=skew_cfg.w_off,
        gamma=skew_cfg.gamma,
        skew_max_widen_cc=skew_cfg.skew_max_widen_cc,
        skew_max_tighten_cc=skew_cfg.skew_max_tighten_cc,
        enabled=skew_cfg.enabled,
        peak_enabled=skew_cfg.peak_enabled,
        peak_widen_max_cc=skew_cfg.peak_widen_max_cc,
        peak_tighten_max_cc=skew_cfg.peak_tighten_max_cc,
        pbook_enabled=skew_cfg.pbook_enabled,
        pbook_armed=skew_cfg.pbook_armed,
        leg_axis_enabled=skew_cfg.leg_axis_enabled,
        leg_axis_armed=skew_cfg.leg_axis_armed,
    )
    skew_limits = SkewLimits(
        max_event_delta_contracts=risk_cfg.max_event_delta_contracts,
        max_event_worst_case_loss_dollars=risk_cfg.max_event_worst_case_loss_dollars,
        max_event_gross_notional_dollars=risk_cfg.max_gross_notional_dollars,
    )
    widen_params = WidenPolicyParams(
        enabled=widen_cfg.enabled, util_threshold=widen_cfg.util_threshold
    )
    fee_cfg = cfg.pricing.fee
    fee_model = FeeModel(
        FeeSchedule.from_strings(fee_cfg.taker_coef, fee_cfg.maker_coef), conventions
    )
    _sc = cfg.pricing.structural
    structural_cfg = StructuralConfigView(
        dc_rho=_sc.dc_rho,
        et_factor=_sc.et_factor,
        pens_win_a=_sc.pens_win_prob,
        half_share=_sc.half_share,
        max_goals=_sc.max_goals,
        knockout_series=tuple(_sc.knockout_series),
        enabled=_sc.enabled,
        corners_et_loading=_sc.corners_et_loading,
    )
    from combomaker.ops.quote_app import build_lifecycle_config, build_settled_resolver

    settled_marginals: SettledMarginalResolver | None = build_settled_resolver(
        risk_cfg, rest, clock
    )

    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch = await Store.open(scratch_dir / "gate2_scratch.sqlite3", clock)

    lifecycle = QuoteLifecycle(
        clock=clock,
        sender=PaperSender(),
        engine=engine,
        rfq_filter=rfq_filter,
        limits=limits,
        exposure=exposure,
        feed=feed,
        metadata=metadata,
        inplay=__import__(
            "combomaker.risk.inplay", fromlist=["InPlayDetector"]
        ).InPlayDetector(clock),
        killswitch=killswitch,
        conventions=conventions,
        store=scratch,
        metrics=metrics,
        lastlook_policy=LastLookPolicy(
            leg_move_tolerance_cc=risk_cfg.leg_move_tolerance_cc,
            joint_move_tolerance_cc=risk_cfg.joint_move_tolerance_cc,
            max_leg_age_s=risk_cfg.max_leg_age_s,
        ),
        config=build_lifecycle_config(
            risk_cfg,
            peak_topk_states=skew_cfg.peak_topk_states,
            peak_n_clusters=skew_cfg.peak_n_clusters,
            peak_cluster_min_frac=skew_cfg.peak_cluster_min_frac,
        ),
        balance_tracker=balance,
        start_time_provider=rfq_filter.leg_start_time,
        starvation_watchdog=StarvationWatchdog(threshold=risk_cfg.starvation_threshold),
        within_game_rho=sgp_within_game_rho_provider(engine.sgp_params),
        structural_cfg=structural_cfg,
        skew_params=skew_params,
        skew_limits=skew_limits,
        widen_params=widen_params,
        fee_model=fee_model,
        fee_type=FeeType.parse(fee_cfg.default_fee_type),
        fee_multiplier=Fraction(fee_cfg.default_multiplier),
        maker_fee_active_prefixes=tuple(fee_cfg.maker_fee_active_prefixes),
        joint_pool=None,          # inline pricing — identical $ output
        book_risk_pool=None,      # inline MC — identical numbers
        settled_marginals=settled_marginals,
    )
    reservation = RiskReservationService(
        exposure=exposure, limits=limits, breach_splitter=lifecycle.partition_breaches
    )
    lifecycle.attach_reservation(reservation)

    return Wired(
        cfg=cfg,
        clock=clock,
        rest=rest,
        feed=feed,
        metadata=metadata,
        exposure=exposure,
        limits=limits,
        lifecycle=lifecycle,
        reservation=reservation,
        balance=balance,
        book_ws=book_ws,
        scratch=scratch,
    )


async def rehydrate(w: Wired, shim: _Shim) -> None:
    """The EXACT live boot rehydration, against a mode=ro store."""
    db = await aiosqlite.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    ro_store = Store(db, w.clock)
    try:
        await QuoteApp._rehydrate_exposure_book(
            shim,
            w.rest,
            ro_store,
            w.exposure,
            w.cfg.filters.allowed_leg_series_prefixes,
            subaccount=w.cfg.safety.subaccount,
            metrics=None,
        )
        await QuoteApp._arm_rehydrated_legs(shim, w.exposure, w.feed, w.metadata)
    finally:
        await db.close()


def cap_table(w: Wired, bankroll_cc: int) -> list[tuple[str, str, str]]:
    lim = w.limits.limits
    rows = [
        ("portfolio_det_max_frac", str(lim.portfolio_det_max_frac),
         d(threshold_cc(lim.portfolio_det_max_frac, bankroll_cc))),
        ("portfolio_cvar_frac", str(lim.portfolio_cvar_frac),
         d(threshold_cc(lim.portfolio_cvar_frac, bankroll_cc))),
        ("directional_frac", str(lim.directional_frac),
         d(threshold_cc(lim.directional_frac, bankroll_cc))),
        ("game_loss_frac", str(lim.game_loss_frac),
         d(threshold_cc(lim.game_loss_frac, bankroll_cc))),
        ("slate_loss_frac", str(lim.slate_loss_frac),
         d(threshold_cc(lim.slate_loss_frac, bankroll_cc))),
        ("per_combo_loss_frac", str(lim.per_combo_loss_frac),
         d(threshold_cc(lim.per_combo_loss_frac, bankroll_cc))),
        ("entity_loss_frac", str(getattr(lim, "entity_loss_frac", "n/a")),
         d(threshold_cc(lim.entity_loss_frac, bankroll_cc))
         if hasattr(lim, "entity_loss_frac") else "n/a"),
    ]
    return rows


async def _await_books(w: Wired, tickers: list[str], *, timeout_s: float) -> None:
    """Wait for the read-only orderbook WS to deliver snapshots (the live feed)."""
    deadline = w.clock.monotonic_ns() + int(timeout_s * 1e9)
    while w.clock.monotonic_ns() < deadline:
        ok = sum(1 for t in tickers if w.feed.book(t).valid)
        if tickers and ok >= len(tickers):
            return
        await asyncio.sleep(0.5)


async def _settlement_decay(w: Wired, bankroll: int, det_thr: int) -> None:
    """Recompute the DETERMINISTIC axes on the book that survives after each
    game's markets settle off, in game-start order. Uses the SAME live
    build_book_model + compute_book_risk (the det axes are sampling-invariant,
    so a small sample budget is used for the curve)."""
    from combomaker.sim.book_model import build_book_model
    from combomaker.sim.book_risk import compute_book_risk

    positions = list(w.exposure.positions.values())
    # game -> earliest known leg start (the bot's own pregame ladder)
    starts: dict[str, datetime | None] = {}
    for p in positions:
        for l in p.legs:
            if not l.event_ticker:
                continue
            g = game_key(l.event_ticker)
            s = w.lifecycle._start_time_provider(l.market_ticker) if w.lifecycle._start_time_provider else None
            if g not in starts or (s is not None and (starts[g] is None or s < starts[g])):
                starts[g] = s
    order = sorted(
        starts.items(), key=lambda kv: (kv[1] is None, kv[1] or datetime.max.replace(tzinfo=UTC))
    )
    print("  game start ladder (bot's own leg_start_time provider):")
    for g, s in order:
        n = sum(1 for p in positions
                if any(l.event_ticker and game_key(l.event_ticker) == g for l in p.legs))
        prem = sum(int(p.contracts) * int(p.entry_price_cc) // 100 for p in positions
                   if any(l.event_ticker and game_key(l.event_ticker) == g for l in p.legs))
        st = s.astimezone(ET).strftime("%m-%d %H:%M ET") if s else "UNKNOWN"
        print(f"     {g:<30} start {st:<16} positions touching {n:>3}  premium {d(prem)}")

    def det_of(subset: list) -> tuple[int, int | None]:
        model = build_book_model(
            subset, marginals=w.lifecycle._marginals,
            within_game_rho=w.lifecycle._within_game_rho,
        )
        s = compute_book_risk(
            model, n_samples=2000, seed=w.lifecycle._config.book_risk_seed, band="high",
            bankroll_cc=bankroll, structural_cfg=w.lifecycle._structural_cfg,
        )
        m = None if s.mutex_aware_det_max_cc is None else int(s.mutex_aware_det_max_cc)
        return int(s.deterministic_max_loss_cc), m

    print("  cumulative settle-off curve (a position leaves when its LAST game ends):")
    print(f"     {'after':<28}{'det gate':>12}{'threshold':>12}{'headroom':>12}  {'%wall':>7}  npos")
    settled: set[str] = set()
    rows = [("NOW (nothing settled)", positions)]
    for g, _s in order:
        settled.add(g)
        surv = [
            p for p in positions
            if not {game_key(l.event_ticker) for l in p.legs if l.event_ticker} <= settled
            or not {game_key(l.event_ticker) for l in p.legs if l.event_ticker}
        ]
        rows.append((f"+ {g}", surv))
    for label, subset in rows:
        det, mdet = det_of(subset)
        gate = det if mdet is None or not w.limits.limits.portfolio_det_max_mutex_aware else min(det, mdet)
        head = det_thr - gate
        print(f"     {label:<28}{d(gate):>12}{d(det_thr):>12}{d(head):>12}  "
              f"{100.0*head/det_thr:+6.1f}%  {len(subset):>4}")


def _shape(rfq: Rfq) -> str:
    fams = sorted({t.split("-", 1)[0] for t in rfq.leg_tickers})
    return f"{len(rfq.legs)}leg:{'+'.join(f.replace('KXMLB','') for f in fams)}"


async def _quote_shapes(
    w: Wired, shim: _Shim, scratch: Path, min_start: int = 1830
) -> None:
    from combomaker.rfq.models import RfqParseError

    # ---- harvest REAL live RFQ payloads off the exchange (read-only GET)
    seen: dict[str, Rfq] = {}
    cursor = ""
    for _ in range(4):
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = await w.rest.get_rfqs(**params)
        for row in payload.get("rfqs") or []:
            try:
                r = Rfq.from_ws(row)
            except RfqParseError:
                continue
            if r.is_combo:
                seen[r.rfq_id] = r
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    print(f"  harvested {len(seen)} real combo RFQ payloads from the exchange tape")

    # SELECTION ONLY (no network, never a risk input): read the slate stamp out
    # of the bot's own game_key (26JUL26<HHMM><MATCHUP>) to find the RFQs whose
    # every leg is on a game starting at/after 18:30 ET tonight.
    import re as _re

    today = w.clock.now().astimezone(ET).strftime("%d%b%y").upper()  # 26JUL26

    def leg_slot(t: str) -> tuple[str, int] | None:
        m = _re.search(r"-(\d{2}[A-Z]{3}\d{2})(\d{4})", t)
        if not m:
            return None
        return m.group(1), int(m.group(2))

    def all_tonight(r: Rfq) -> bool:
        slots = [leg_slot(t) for t in r.leg_tickers]
        if any(s is None for s in slots):
            return False
        return all(s[0] == today and s[1] >= min_start for s in slots)  # type: ignore[index]

    tonight = [r for r in seen.values() if all_tonight(r)]
    print(f"  of those, ALL-LEGS-TONIGHT (start >= {min_start//100:02d}:"
          f"{min_start%100:02d} ET): {len(tonight)}")
    by_shape: dict[str, list[Rfq]] = {}
    for r in tonight:
        by_shape.setdefault(_shape(r), []).append(r)
    for s in sorted(by_shape, key=lambda k: -len(by_shape[k])):
        print(f"     {s:<38} {len(by_shape[s])}")

    # top concentrated entity keys in the CURRENT book (for the 4th shape)
    prof = w.lifecycle._leg_axis_profile_from(
        w.exposure.snapshot(w.lifecycle._marginals, mass_acceptance=True)
    )
    ent_cc = {k: s * prof.total_entity_cc for k, s in prof.shares_by_entity.items()}
    tops = sorted(ent_cc.items(), key=lambda kv: -kv[1])[:10]
    print("  top entity keys in the rehydrated book (premium-at-risk):")
    for k, v in tops:
        print(f"     {k:<44} {d(v)}")
    hot_entities = {k.split(":")[1] for k, _ in tops if k.count(":") >= 2}

    picks: list[tuple[str, Rfq]] = []

    def pick(label: str, pred) -> None:
        for r in tonight:
            if r.rfq_id in {x.rfq_id for _, x in picks}:
                continue
            if pred(r):
                picks.append((label, r))
                return

    pick("A. 2-leg", lambda r: len(r.legs) == 2)
    pick("B. 4-leg ML parlay",
         lambda r: len(r.legs) == 4 and all("KXMLBGAME" in t for t in r.leg_tickers))
    pick("B'. 4-leg (any family)", lambda r: len(r.legs) == 4)
    pick("C. 4-6 leg strikeout combo",
         lambda r: 4 <= len(r.legs) <= 6 and any("KXMLBKS" in t for t in r.leg_tickers))
    pick("C'. any KS-carrying combo", lambda r: any("KXMLBKS" in t for t in r.leg_tickers))
    pick("D. touches an already-concentrated book arm",
         lambda r: bool(hot_entities) and any(any(e in t for e in hot_entities)
                                              for t in r.leg_tickers))

    if not picks:
        print("  !! no tonight-slate RFQ shapes harvested — cannot drive the path")
        return

    for label, r in picks:
        await _drive(w, shim, label, r)

    # ---- D': CONCENTRATION LADDER. If the committed book carries nothing on a
    # TONIGHT arm (today's arms have all pitched), manufacture the concentration
    # the honest way: reserve successive real RFQs on the SAME (player x
    # direction) entity key WITHOUT releasing, so each check sees the prior
    # reservations folded in — the exact live accumulation path.
    arms: dict[str, list[Rfq]] = {}
    for r in tonight:
        for t in r.leg_tickers:
            if "KXMLBKS" in t:
                parts = t.split("-")
                if len(parts) >= 3:
                    arms.setdefault(parts[2], []).append(r)
    if arms:
        arm, rs = max(arms.items(), key=lambda kv: len(kv[1]))
        print("=" * 100)
        print(f"  D'. CONCENTRATION LADDER on ONE arm: {arm}  ({len(rs)} tonight RFQs)")
        held: list[str] = []
        for i, r in enumerate(rs[:8]):
            ok = await _drive(w, shim, f"     ladder #{i+1} ({arm})", r, keep=True)
            if ok:
                held.append(ok)
        for rid in held:
            w.reservation.release(rid)
        print(f"     ladder: {len(held)}/{min(8,len(rs))} reservations GRANTED "
              f"before the wall bit")


async def _drive(w: Wired, shim: _Shim, label: str, rfq: Rfq, keep: bool = False) -> str | None:
    lc = w.lifecycle
    print("-" * 100)
    size = (f"target_cost {d(rfq.target_cost_cc)}" if rfq.target_cost_cc
            else f"contracts {int(rfq.contracts or 0)/100:.2f}")
    print(f"  {label}  rfq={rfq.rfq_id[:8]}  {size}")
    for l in rfq.legs:
        print(f"      leg {l.side:>3}: {l.market_ticker}")
    # Warm metadata + the per-collection combo grid with BACKOFF (the harness
    # shares the account's read budget; a 429 here is a harness artifact, not a
    # bot decline — retry until the exchange answers).
    for attempt in range(8):
        await QuoteApp._ensure_watched(shim, rfq, w.feed, w.metadata)
        ok_meta = all(w.metadata.peek(t) is not None for t in rfq.leg_tickers)
        ok_grid = w.metadata.peek(rfq.market_ticker) is not None
        if ok_meta and ok_grid:
            break
        await asyncio.sleep(2.0 * (attempt + 1))
    await _await_books(w, list(rfq.leg_tickers), timeout_s=20.0)

    reasons: list[str] = []
    orig_record = lc._record_skip

    async def spy(r, rs, ctx=None):  # type: ignore[no-untyped-def]
        reasons.extend(str(x) for x in rs)
        if ctx:
            print(f"      ctx: {json.dumps(ctx, default=str)[:600]}")
        return await orig_record(r, rs, ctx)

    lc._record_skip = spy  # type: ignore[assignment]
    before = set(lc._open)
    try:
        await lc.handle_rfq(rfq)
    except Exception as exc:  # noqa: BLE001 — report, never mask
        print(f"      EXCEPTION: {exc!r}")
    finally:
        lc._record_skip = orig_record  # type: ignore[assignment]

    new = [q for q in lc._open if q not in before]
    if new:
        st = lc._open[new[0]]
        c = st.constructed
        print(f"      >>> QUOTE CONSTRUCTED  yes_bid {int(c.yes_bid_cc)/100:.2f}c  "
              f"no_bid {int(c.no_bid_cc)/100:.2f}c  fair {int(c.fair_cc)/100:.2f}c")
        print(f"      >>> RISK SIZE (full RFQ) {int(st.risk_qty)/100:.2f} contracts  "
              f"= premium {d(int(st.risk_qty)*int(c.no_bid_cc)//100)} at the NO bid")
        # exercise the CONFIRM-path reservation with the exact live function.
        # The hypothetical accept is the FULL RFQ size on the NO (seller) side —
        # exactly the candidate _quoting_policy / the limit check already build.
        from combomaker.core.conventions import Side as _Side

        st.pending_fill = (_Side.NO, c.no_bid_cc, st.risk_qty)
        res = lc._reserve_headroom(f"gate2:{new[0]}", new[0], st)
        rid: str | None = None
        if res is None:
            print("      >>> RESERVATION: no service wired")
        else:
            print(f"      >>> RESERVATION (confirm path): granted={res.granted} "
                  f"breaches={[str(b.reason) for b in res.breaches]}")
            for b in res.breaches:
                print(f"          {b.reason}: {b.detail[:220]}")
            if res.reservation is not None:
                rid = res.reservation.reservation_id
                if not keep:
                    w.reservation.release(rid)
                    rid = None
        # drop the paper quote so shapes don't contaminate each other
        lc._drop_quote(new[0])
        return rid
    print(f"      >>> DECLINED  reasons={sorted(set(reasons))}")
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default="")
    ap.add_argument("--stage", default="all")
    ap.add_argument("--skip-decay", action="store_true")
    ap.add_argument("--quiet-book", action="store_true")
    ap.add_argument("--min-start", type=int, default=1830,
                    help="earliest leg game-start HHMM ET a shape may touch")
    args = ap.parse_args()
    configure_logging(json_output=False, level="warning")
    scratch = Path(args.scratch or "data/_gate2_scratch")

    w = await wire(scratch)
    try:
        print("=" * 100)
        print("STAGE 0 — WIRED (paper sender; live store mode=ro; scratch tape)")
        bank = w.balance.risk_bankroll_cc_or_none()
        cash = w.balance.available_cash_cc_or_none()
        print(f"  risk bankroll  {d(bank)}")
        print(f"  available cash {d(cash)}")
        print(f"  equity(pnl)    {d(w.balance.pnl_equity_cc_or_none())}")

        print("=" * 100)
        print("STAGE 1 — BOOT REHYDRATION (QuoteApp._rehydrate_exposure_book, real payload)")
        shim = _Shim(w.cfg)
        await rehydrate(w, shim)
        pos = w.exposure.positions
        print(f"  positions rehydrated: {len(pos)}")
        modeled = [p for p in pos.values() if p.risk_modeled]
        reserved = [p for p in pos.values() if not p.risk_modeled]
        print(f"    risk_modeled={len(modeled)}  reserved(non-modeled)={len(reserved)}")
        prem = sum(int(p.contracts) * int(p.entry_price_cc) // 100 for p in pos.values())
        print(f"    sum premium at risk (entry basis): {d(prem)}")
        for p in sorted(pos.values(), key=lambda x: -(int(x.contracts) * int(x.entry_price_cc))):
            games = sorted({game_key(l.event_ticker) for l in p.legs if l.event_ticker})
            print(
                f"      {p.position_id[:14]:<14} {p.our_side.value:>3} "
                f"{int(p.contracts)/100:8.2f}ct @ {int(p.entry_price_cc)/100:6.2f}c "
                f"maxloss {d(int(p.contracts)*int(p.entry_price_cc)//100):>10} "
                f"legs={len(p.legs)} games={games}"
            )
        # dump for later stages
        (scratch / "book.json").write_text(
            json.dumps(
                [
                    {
                        "id": p.position_id,
                        "ticker": p.combo_ticker,
                        "side": p.our_side.value,
                        "ct": int(p.contracts),
                        "px": int(p.entry_price_cc),
                        "modeled": p.risk_modeled,
                        "legs": [
                            {"t": l.market_ticker, "e": l.event_ticker, "s": l.side}
                            for l in p.legs
                        ],
                    }
                    for p in pos.values()
                ],
                indent=1,
            )
        )
        print(f"  book dumped -> {scratch/'book.json'}")

        # ------------------------------------------------ STAGE 2: BOOK RISK
        print("=" * 100)
        print("STAGE 2 — FIRST BOOK-RISK SNAPSHOT (the live startup snapshot path)")
        legs = sorted(
            {l.market_ticker for p in pos.values() for l in p.legs
             if l.market_ticker != p.combo_ticker}
        )
        await _await_books(w, legs, timeout_s=45.0)
        have = sum(1 for t in legs if w.lifecycle._marginals(t) is not None)
        print(f"  committed legs: {len(legs)}  with a usable marginal: {have}")
        # settled-leg resolution (the maintenance-tick pass), then recompute
        for _ in range(6):
            w.lifecycle._maybe_resolve_settled_marginals()
            task = getattr(w.lifecycle, "_settled_task", None)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=25.0)
                except Exception:
                    pass
            have2 = sum(1 for t in legs if w.lifecycle._marginals(t) is not None)
            if have2 >= len(legs):
                break
        have = sum(1 for t in legs if w.lifecycle._marginals(t) is not None)
        print(f"  after settled-leg resolution: {have}/{len(legs)} legs priced")
        missing = [t for t in legs if w.lifecycle._marginals(t) is None]
        if missing:
            print(f"  UNRESOLVED legs ({len(missing)}): {missing[:12]}")

        w.lifecycle.recompute_book_risk()
        snap = w.lifecycle._book_risk
        risk = w.lifecycle._book_risk_for_check()
        bankroll = int(bank or 0)
        lim = w.limits.limits
        det_thr = threshold_cc(lim.portfolio_det_max_frac, bankroll)
        cvar_thr = threshold_cc(lim.portfolio_cvar_frac, bankroll)
        esnap = w.exposure.snapshot(w.lifecycle._marginals, mass_acceptance=True)
        gross = int(esnap.gross_notional_cc)
        direction = max((abs(v) for v in esnap.directional_by_game_cc.values()), default=0)
        print(f"  snapshot usable        : {None if snap is None else snap.usable}")
        if snap is not None and snap.usable:
            det = int(snap.deterministic_max_loss_cc)
            mdet = None if snap.mutex_aware_det_max_cc is None else int(snap.mutex_aware_det_max_cc)
            gate = det if mdet is None or not lim.portfolio_det_max_mutex_aware else min(det, mdet)
            print(f"  n_positions            : {snap.n_positions}")
            print(f"  deterministic_max_loss : {det:>12,d} cc  {d(det)}")
            print(f"  mutex_aware_det_max    : {(mdet if mdet is not None else -1):>12,d} cc  {d(mdet)}")
            print(f"  DET GATE VALUE (min)   : {gate:>12,d} cc  {d(gate)}")
            print(f"  det threshold {lim.portfolio_det_max_frac} x {d(bankroll)}"
                  f" = {det_thr:>12,d} cc  {d(det_thr)}")
            head = det_thr - gate
            print(f"  >>> DET-MAX HEADROOM   : {head:>12,d} cc  {d(head)}"
                  f"   ({100.0*head/det_thr:+.1f}% of the wall)")
            print(f"  gross (mass-accept)    : {gross:>12,d} cc  {d(gross)}")
            print(f"  gross vs det threshold : {d(gross)} vs {d(det_thr)}"
                  f"   headroom {d(det_thr - gross)}")
            print(f"  direction (worst game) : {direction:>12,d} cc  {d(direction)}")
            print(f"  governing ES99         : {int(snap.governing_model_es_99_cc):>12,d} cc  {d(snap.governing_model_es_99_cc)}"
                  f"   (cvar thr {d(cvar_thr)})")
            print(f"  production ES99        : {d(snap.production_es_99_cc)}   challenger {d(snap.challenger_es_99_cc)}"
                  f"   bridge {d(snap.bridge_es_99_cc)} active={snap.bridge_active}")
            print(f"  p_book {snap.p_profit:.4f}  p_night {snap.p_night:.4f}  ev {d(snap.ev_cc)}"
                  f"  p_ruin {snap.p_ruin:.4f} (upper {snap.p_ruin_upper:.4f})")
            print(f"  location_joint={snap.location_joint}@{snap.location_band}"
                  f"  tail_joint={snap.tail_joint}@{snap.band}")
            print("  per-game tail (top 6):")
            for tc in sorted(snap.per_game_tail_cc, key=lambda x: -x.loss_cc)[:6]:
                print(f"     {tc.key:<24} {d(tc.loss_cc)}")
        print(f"  risk view for check usable: {None if risk is None else risk.usable}")
        print("  ENFORCED CAP TABLE at the live bankroll:")
        for name, frac, dollars in cap_table(w, bankroll):
            print(f"     {name:<26} {frac:<10} {dollars}")

        # ------------------------------------------------ STAGE 3: SETTLEMENT
        print("=" * 100)
        print("STAGE 3 — DET-MAX HEADROOM AS TODAY'S GAMES SETTLE OFF")
        if args.skip_decay:
            print("  (skipped)")
        else:
            await _settlement_decay(w, bankroll, det_thr)

        # ------------------------------------------------ STAGE 4: QUOTES
        print("=" * 100)
        print("STAGE 4 — DRIVE THE REAL PRICING + RESERVATION PATH ON LIVE RFQ SHAPES")
        await _quote_shapes(w, shim, scratch, args.min_start)
    finally:
        await w.scratch.close()
        await w.book_ws.stop()
        await w.rest.close()


if __name__ == "__main__":
    asyncio.run(main())
