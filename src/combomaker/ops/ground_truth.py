"""Phase 2.5 ground-truth harness: real RFQ round trips on DEMO, recorded raw.

Runs maker + requester as two credential sets against the demo exchange,
executes the scenarios below on a target market, and records EVERYTHING the
exchange said (quotes, fills, positions, balances, error bodies) into
``tests/fixtures/ground_truth/``. A derivation pass turns the recordings into
``conventions.json`` — the fixture ``core/conventions.py`` loads — plus a
human-readable report of the evidence. Raw recordings are the artifact;
derivation is best-effort and re-runnable offline.

Scenarios:
  A. quote → requester accepts YES → maker confirms → executed. Who is long
     what, at what price, what fee, is_taker flags, balance deltas.
  B. same with accepted NO.
  C. quote → accept → maker deliberately does NOT confirm → record terminal
     quote status after the window (+ the error body of a too-late confirm).
  D. quote → accept → maker attempts DELETE inside the window → record result.
  E. off-grid quote probe → record the exact 400 body.
  F. endpoint costs + API limits snapshot.

DEMO ONLY. The harness refuses to run against production endpoints.

Prereqs (human): two demo accounts. Maker creds in KALSHI_API_KEY_ID /
KALSHI_PRIVATE_KEY_PATH; requester creds in KALSHI_REQUESTER_API_KEY_ID /
KALSHI_REQUESTER_PRIVATE_KEY_PATH. Pick a liquid, open, STANDARD (non-HVM)
demo market for --market (30s confirm window makes orchestration reliable);
run again later with a combo market for HVM timing + product settlement.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from combomaker.core.clock import SystemClock
from combomaker.core.money import CC_PER_DOLLAR, CentiCents, cc_to_dollars_str
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiApiError, KalshiRestClient
from combomaker.marketdata.grid import GridError, PriceGrid
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

JsonDict = dict[str, Any]

EXECUTION_WAIT_S = 25.0  # standard-market execution timer 15s + margin
CONFIRM_WINDOW_LAPSE_WAIT_S = 40.0  # standard confirm window 30s + margin


class GroundTruthError(RuntimeError):
    pass


class Recorder:
    def __init__(self) -> None:
        self.entries: list[JsonDict] = []
        self._clock = SystemClock()

    def record(self, step: str, data: JsonDict) -> None:
        entry = {"at": self._clock.now().isoformat(), "step": step, "data": data}
        self.entries.append(entry)
        log.info("ground_truth_step", step=step)

    def record_error(self, step: str, exc: KalshiApiError) -> None:
        self.record(
            step,
            {
                "error": True,
                "status": exc.status,
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(e, default=str) for e in self.entries) + "\n",
            encoding="utf-8",
            newline="\n",
        )


class Party:
    """One authenticated participant (maker or requester)."""

    def __init__(self, label: str, rest: KalshiRestClient) -> None:
        self.label = label
        self.rest = rest

    async def snapshot(self, market_ticker: str) -> JsonDict:
        """Balance + positions + recent fills for the target market, raw."""
        out: JsonDict = {"label": self.label}
        out["balance"] = await self.rest.get_balance()
        try:
            out["positions"] = await self.rest.get_positions(ticker=market_ticker)
        except KalshiApiError as exc:
            out["positions_error"] = {"status": exc.status, "message": exc.message}
        try:
            out["fills"] = await self.rest.get_fills(ticker=market_ticker, limit=20)
        except KalshiApiError as exc:
            out["fills_error"] = {"status": exc.status, "message": exc.message}
        return out


async def _pick_quote_prices(
    maker: Party, market_ticker: str, recorder: Recorder
) -> tuple[CentiCents, CentiCents, PriceGrid | None]:
    """Choose safe on-grid two-sided prices around the current book/mid."""
    market_payload = await maker.rest.get_market(market_ticker)
    recorder.record("market_payload", market_payload)
    market = market_payload.get("market", market_payload)
    grid: PriceGrid | None
    try:
        grid = PriceGrid.from_market_payload(market)
    except GridError:
        grid = None

    mid_cc = 5_000
    try:
        book = await maker.rest.get_orderbook(market_ticker)
        recorder.record("orderbook", book)
        levels = book.get("orderbook_fp", {})
        yes_levels = levels.get("yes_dollars") or []
        no_levels = levels.get("no_dollars") or []
        if yes_levels and no_levels:
            from combomaker.core.money import cc_from_dollars_str

            best_yes = int(cc_from_dollars_str(str(yes_levels[-1][0])))
            best_no = int(cc_from_dollars_str(str(no_levels[-1][0])))
            mid_cc = (best_yes + (CC_PER_DOLLAR - best_no)) // 2
    except KalshiApiError as exc:
        recorder.record_error("orderbook_error", exc)

    # 4 cents of total spread around mid keeps us comfortably inside sum<=1.
    yes_target = max(100, min(9_700, mid_cc - 200))
    no_target = max(100, min(9_700, (CC_PER_DOLLAR - mid_cc) - 200))
    if grid is not None:
        yes_snapped = grid.snap_bid_down(CentiCents(yes_target))
        no_snapped = grid.snap_bid_down(CentiCents(no_target))
        if yes_snapped is None or no_snapped is None:
            raise GroundTruthError("could not place prices on the market grid")
        return yes_snapped, no_snapped, grid
    # No grid info: fall back to whole cents.
    return CentiCents(yes_target - yes_target % 100), CentiCents(no_target - no_target % 100), None


async def _quote_and_accept(
    maker: Party,
    requester: Party,
    market_ticker: str,
    contracts_fp: str,
    accepted_side: str,
    recorder: Recorder,
) -> str:
    """Create RFQ (requester) → quote (maker) → accept (requester). Returns quote_id."""
    rfq_resp = await requester.rest.create_rfq(
        market_ticker, contracts_fp=contracts_fp, rest_remainder=False, replace_existing=True
    )
    recorder.record("rfq_created", rfq_resp)
    rfq_id = str(rfq_resp["id"])

    yes_bid, no_bid, _ = await _pick_quote_prices(maker, market_ticker, recorder)
    quote_resp = await maker.rest.create_quote(
        rfq_id, yes_bid_cc=yes_bid, no_bid_cc=no_bid, rest_remainder=False
    )
    recorder.record(
        "quote_created",
        {
            "response": quote_resp,
            "yes_bid": cc_to_dollars_str(yes_bid),
            "no_bid": cc_to_dollars_str(no_bid),
        },
    )
    quote_id = str(quote_resp.get("id") or quote_resp.get("quote_id"))

    accept_resp = await requester.rest.accept_quote(quote_id, accepted_side=accepted_side)
    recorder.record("quote_accepted", {"response": accept_resp, "accepted_side": accepted_side})
    return quote_id


async def _await_terminal_quote(
    maker: Party, quote_id: str, recorder: Recorder, *, timeout_s: float = 60.0
) -> JsonDict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: JsonDict = {}
    while asyncio.get_event_loop().time() < deadline:
        try:
            payload = await maker.rest.get_quote(quote_id)
        except KalshiApiError as exc:
            recorder.record_error("get_quote_error", exc)
            break
        last = payload.get("quote", payload)
        status = str(last.get("status", ""))
        recorder.record("quote_status_poll", {"status": status})
        if status in ("executed", "cancelled"):
            break
        await asyncio.sleep(2.0)
    recorder.record("quote_terminal", last)
    return last


async def _scenario_execution(
    maker: Party,
    requester: Party,
    market_ticker: str,
    contracts_fp: str,
    accepted_side: str,
    out_dir: Path,
) -> Recorder:
    recorder = Recorder()
    recorder.record("scenario", {"name": f"accept_{accepted_side}_confirm"})
    recorder.record("before_maker", await maker.snapshot(market_ticker))
    recorder.record("before_requester", await requester.snapshot(market_ticker))

    quote_id = await _quote_and_accept(
        maker, requester, market_ticker, contracts_fp, accepted_side, recorder
    )
    confirm_resp = await maker.rest.confirm_quote(quote_id)
    recorder.record("quote_confirmed", confirm_resp)

    await asyncio.sleep(EXECUTION_WAIT_S)
    quote = await _await_terminal_quote(maker, quote_id, recorder)

    for order_key in ("creator_order_id", "rfq_creator_order_id"):
        order_id = quote.get(order_key)
        if order_id:
            try:
                fills = await maker.rest.get_fills(order_id=str(order_id))
                recorder.record(f"fills_by_{order_key}", fills)
            except KalshiApiError as exc:
                recorder.record_error(f"fills_by_{order_key}_error", exc)

    recorder.record("after_maker", await maker.snapshot(market_ticker))
    recorder.record("after_requester", await requester.snapshot(market_ticker))
    recorder.save(out_dir / f"scenario_accept_{accepted_side}.jsonl")
    return recorder


async def _scenario_lapse(
    maker: Party, requester: Party, market_ticker: str, contracts_fp: str, out_dir: Path
) -> Recorder:
    recorder = Recorder()
    recorder.record("scenario", {"name": "accept_then_lapse"})
    quote_id = await _quote_and_accept(
        maker, requester, market_ticker, contracts_fp, "yes", recorder
    )
    recorder.record("deliberately_not_confirming", {"wait_s": CONFIRM_WINDOW_LAPSE_WAIT_S})
    await asyncio.sleep(CONFIRM_WINDOW_LAPSE_WAIT_S)
    try:
        late = await maker.rest.confirm_quote(quote_id)
        recorder.record("late_confirm_unexpectedly_succeeded", late)
    except KalshiApiError as exc:
        recorder.record_error("late_confirm_error", exc)
    await _await_terminal_quote(maker, quote_id, recorder, timeout_s=20.0)
    recorder.save(out_dir / "scenario_lapse.jsonl")
    return recorder


async def _scenario_delete_after_accept(
    maker: Party, requester: Party, market_ticker: str, contracts_fp: str, out_dir: Path
) -> Recorder:
    recorder = Recorder()
    recorder.record("scenario", {"name": "delete_after_accept"})
    quote_id = await _quote_and_accept(
        maker, requester, market_ticker, contracts_fp, "yes", recorder
    )
    try:
        resp = await maker.rest.delete_quote(quote_id)
        recorder.record("delete_after_accept_result", resp)
    except KalshiApiError as exc:
        recorder.record_error("delete_after_accept_error", exc)
    await asyncio.sleep(CONFIRM_WINDOW_LAPSE_WAIT_S)
    await _await_terminal_quote(maker, quote_id, recorder, timeout_s=20.0)
    recorder.save(out_dir / "scenario_delete_after_accept.jsonl")
    return recorder


async def _scenario_off_grid(
    maker: Party, requester: Party, market_ticker: str, contracts_fp: str, out_dir: Path
) -> Recorder:
    recorder = Recorder()
    recorder.record("scenario", {"name": "off_grid_probe"})
    rfq_resp = await requester.rest.create_rfq(
        market_ticker, contracts_fp=contracts_fp, rest_remainder=False, replace_existing=True
    )
    recorder.record("rfq_created", rfq_resp)
    rfq_id = str(rfq_resp["id"])
    try:
        resp = await maker.rest.create_quote(
            rfq_id,
            yes_bid_cc=CentiCents(3_550),  # $0.3550 — off a 1-cent grid
            no_bid_cc=CentiCents(6_000),
            rest_remainder=False,
        )
        recorder.record("off_grid_quote_unexpectedly_accepted", resp)
    except KalshiApiError as exc:
        recorder.record_error("off_grid_quote_error", exc)
    try:
        await requester.rest.delete_rfq(rfq_id)
    except KalshiApiError as exc:
        recorder.record_error("cleanup_delete_rfq_error", exc)
    recorder.save(out_dir / "scenario_off_grid.jsonl")
    return recorder


async def _scenario_account_facts(maker: Party, out_dir: Path) -> Recorder:
    recorder = Recorder()
    recorder.record("scenario", {"name": "account_facts"})
    for step, call in (
        ("endpoint_costs", maker.rest.get_endpoint_costs),
        ("api_limits", maker.rest.get_api_limits),
        ("communications_id", maker.rest.get_communications_id),
    ):
        try:
            recorder.record(step, await call())
        except KalshiApiError as exc:
            recorder.record_error(f"{step}_error", exc)
    recorder.save(out_dir / "scenario_account_facts.jsonl")
    return recorder


def derive_conventions(
    accept_yes_entries: list[JsonDict], accept_no_entries: list[JsonDict]
) -> JsonDict:
    """Best-effort derivation of conventions.json from scenario recordings.

    Pure and re-runnable offline. Returns a dict with the Conventions fields
    plus an ``evidence`` block; fields it cannot establish are set to None —
    a human inspects the raw recordings and fills them in consciously.
    """

    def maker_fill(entries: list[JsonDict]) -> JsonDict | None:
        for entry in entries:
            if entry.get("step", "").startswith("fills_by_creator_order_id"):
                fills = entry.get("data", {}).get("fills") or []
                if fills:
                    return dict(fills[0])
        # fallback: after_maker snapshot's most recent fill
        for entry in entries:
            if entry.get("step") == "after_maker":
                fills = entry.get("data", {}).get("fills", {}).get("fills") or []
                if fills:
                    return dict(fills[0])
        return None

    yes_fill = maker_fill(accept_yes_entries)
    no_fill = maker_fill(accept_no_entries)

    out: JsonDict = {
        "maker_side_on_yes_accept": None,
        "maker_side_on_no_accept": None,
        "maker_pays_own_bid": None,
        "maker_is_taker_on_fill": None,
        "combo_no_pays_complement": None,
        "evidence": {"accept_yes_maker_fill": yes_fill, "accept_no_maker_fill": no_fill},
    }
    if yes_fill is not None:
        out["maker_side_on_yes_accept"] = yes_fill.get("outcome_side") or yes_fill.get("side")
        out["maker_is_taker_on_fill"] = yes_fill.get("is_taker")
    if no_fill is not None:
        out["maker_side_on_no_accept"] = no_fill.get("outcome_side") or no_fill.get("side")
    return out


async def run_ground_truth(
    *,
    rest_base_url: str,
    market_ticker: str,
    contracts_fp: str,
    out_dir: Path,
) -> Path:
    if ".kalshi.com" in rest_base_url:
        raise GroundTruthError("ground-truth harness runs on DEMO only")

    clock = SystemClock()
    maker_creds = Credentials.from_env()
    requester_creds = Credentials.from_env_names(
        "KALSHI_REQUESTER_API_KEY_ID",
        "KALSHI_REQUESTER_PRIVATE_KEY_PATH",
        "KALSHI_REQUESTER_PRIVATE_KEY_PEM",
    )
    if maker_creds.api_key_id == requester_creds.api_key_id:
        raise GroundTruthError(
            "maker and requester must be DIFFERENT demo accounts "
            "(self-quoting may be blocked; results would be meaningless)"
        )

    async with (
        KalshiRestClient(rest_base_url, RequestSigner(maker_creds, clock)) as maker_rest,
        KalshiRestClient(rest_base_url, RequestSigner(requester_creds, clock)) as requester_rest,
    ):
        maker = Party("maker", maker_rest)
        requester = Party("requester", requester_rest)

        yes_rec = await _scenario_execution(
            maker, requester, market_ticker, contracts_fp, "yes", out_dir
        )
        no_rec = await _scenario_execution(
            maker, requester, market_ticker, contracts_fp, "no", out_dir
        )
        await _scenario_lapse(maker, requester, market_ticker, contracts_fp, out_dir)
        await _scenario_delete_after_accept(
            maker, requester, market_ticker, contracts_fp, out_dir
        )
        await _scenario_off_grid(maker, requester, market_ticker, contracts_fp, out_dir)
        await _scenario_account_facts(maker, out_dir)

    derived = derive_conventions(yes_rec.entries, no_rec.entries)
    derived_path = out_dir / "conventions.derived.json"
    derived_path.write_text(
        json.dumps(derived, indent=2, default=str), encoding="utf-8", newline="\n"
    )
    log.info("ground_truth_complete", out_dir=str(out_dir))
    # Deliberately NOT auto-writing conventions.json: a human must review the
    # evidence and promote conventions.derived.json -> conventions.json.
    return derived_path
