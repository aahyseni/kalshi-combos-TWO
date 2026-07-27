"""AUTO-RELIGHT for LIFECYCLE-class halts (design 2026-07-27, §10).

The feature restarts the bot for exactly ONE halt class — a market-scoped
lifecycle QUARANTINE we could not prove ENFORCED — and refuses everything else.
These tests are the verification the design owes, and they are fully offline:
no exchange, no credential, no socket, and no attach point on the quote hot
path (the receipt write is one atomic file write on a terminal path; the
evidence recording is dict assignment on the 15 s status loop).

The seven things proved here, in order:

1.  A lifecycle-class halt writes a receipt whose evidence RE-DERIVES the class,
    the relighter GRANTS on it, and the relaunched bot cannot reach quoting
    without the startup exchange reconcile actually running.
2.  A settlement/payoff halt does NOT relight — and neither does a reconcile
    mismatch, a rate-limit burst, a supervisor heartbeat kill, or a human KILL
    of unknown provenance.
3.  The crash loop is BOUNDED, twice over and independently (B2 novelty, B3
    amortization), and terminates in a human escalation.
4.  A FAILED reconcile keeps the marker, reds the preflight, exits 3, and is
    refused by G4 — it stays down.
5.  Lifecycle halts SEPARATED by productive work are not confused with flapping.
6.  Every grant is loud and attributable; escalation is a distinct terminal
    record.
7.  (in the sibling suites) nothing existing regresses.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.reasons import ReasonCode
from combomaker.ops.preflight import PreflightError
from combomaker.ops.quote_app import QuoteApp
from combomaker.ops.relight import (
    _RELIGHTABLE_HALT_CLASSES,
    HALT_RECEIPT_FILENAME,
    HALT_RECEIPT_SCHEMA_VERSION,
    RUN_ID_ENV,
    Relighter,
    RelightPolicy,
    RelightSession,
    build_child_argv,
)
from combomaker.risk.exposure import LegRef
from combomaker.risk.killswitch import HaltEvent
from tests.test_metadata_change_scope import (
    _CLETB_BEFORE,
    CLETB,
    _armed_app,
    _meta,
    _mutated,
)
from tests.test_quote_app_phase6 import (
    FakeFeed,
    FakeLifecycle,
    FakeMetadata,
    FakeRest,
    _book_with_quote_legs,
    _breakers,
    _prod_app_for_preflight,
    _reservation,
    _sample,
)

# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _QuarantineLifecycle(FakeLifecycle):
    """A QuoteLifecycle double for the ENFORCEMENT pass: it reports how many
    quotes it withdrew, how many it could not prove gone, and — the new datum —
    which failure MODES those were."""

    def __init__(
        self,
        *,
        deleted: int = 0,
        failures: int = 0,
        kinds: dict[str, int] | None = None,
        marginals: dict[str, float | None] | None = None,
    ) -> None:
        super().__init__(marginals or {})
        self._deleted = deleted
        self._failures = failures
        self._kinds = kinds or {}
        self.calls: list[set[str]] = []

    async def cancel_quotes_touching(
        self, tickers: set[str], reason: Any, *, budget_s: float | None = None
    ) -> tuple[int, int]:
        self.calls.append(set(tickers))
        return (self._deleted, self._failures)

    @property
    def last_withdraw_failure_kinds(self) -> Any:
        from collections import Counter

        return Counter(self._kinds)


def _legs() -> tuple[LegRef, ...]:
    return (LegRef(CLETB, "KXMLBTOTAL-26JUL261215CLETB", "yes"),)


def _seed(app: QuoteApp, raw: dict[str, Any]) -> Any:
    """One status-loop breaker sample against ``raw``."""
    return _sample(
        app,
        FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False),
        lifecycle=FakeLifecycle({CLETB: 0.02}),
        exposure=_book_with_quote_legs(_legs()),
        metadata=FakeMetadata({CLETB: _meta(raw)}),
    )


async def _unenforced_promotion(
    app: QuoteApp, *, kinds: dict[str, int]
) -> tuple[Any, Any]:
    """Drive the REAL three-tick sequence that produces the relightable halt:

    tick 1 — seed the baseline (no change).
    tick 2 — a pure LIFECYCLE move (status active->inactive on the real CLETB
             payload): scoped quarantine, no halt. Then the enforcement pass
             FAILS to prove our resting quotes came off it.
    tick 3 — the still-unenforced quarantine is PROMOTED to the whole-bot halt.

    Returns ``(third_sample, breaker_verdict)``.
    """
    _seed(app, _CLETB_BEFORE)
    second = _seed(app, _mutated(status="inactive"))
    assert second.changed_markets == ()  # lifecycle lane: scoped, not a halt
    assert app._market_quarantine.is_quarantined(CLETB) is True
    lifecycle = _QuarantineLifecycle(deleted=3, failures=len(kinds) and 4, kinds=kinds)
    await app._enforce_market_quarantine(lifecycle)  # type: ignore[arg-type]
    third = _seed(app, _mutated(status="inactive"))
    return third, _breakers(app).evaluate(third)


def _halt(app: QuoteApp, verdict: Any) -> dict[str, Any]:
    """Run the halt bookkeeping the way ``on_halt`` does — receipt FIRST, then
    the reconcile marker — and return the receipt as the relighter reads it."""
    event = HaltEvent(
        reason=verdict.reason,
        detail=str(verdict.detail),
        at_iso="2026-07-27T12:00:00+00:00",
    )
    app._write_halt_receipt(event)
    app.mark_reconcile_on_hard_halt(event)
    return json.loads(
        (app._config.data_dir / HALT_RECEIPT_FILENAME).read_text(encoding="utf-8")
    )


def _decide(
    receipt: Any,
    *,
    run_id: str = "RUNID",
    pid: int = 4242,
    stamp: bool = True,
    exit_code: int | None = 0,
    kill: bool = False,
    marker: bool = True,
    marker_body: str | None = "halt_metadata_change",
    session: RelightSession | None = None,
    cost_s: float | None = None,
    work_s: float | None = None,
) -> Any:
    """Ask the policy, with every gate satisfied by default so each test moves
    exactly ONE input and the named gate is the only thing that can refuse.

    ``stamp`` writes the expected nonce/pid INTO the receipt (so a receipt built
    by the real halt path, which carries whatever env it ran under, satisfies
    G2/G3). Set it False to exercise G2/G3 themselves."""
    if stamp and isinstance(receipt, dict):
        receipt = {**receipt, "run_id": run_id, "pid": pid, "ppid": pid}
    return RelightPolicy().decide(
        receipt=receipt,
        expected_run_id=run_id,
        expected_pid=pid,
        exit_code=exit_code,
        kill_present=kill,
        reconcile_marker_present=marker,
        reconcile_marker_body=marker_body,
        session=session or RelightSession(),
        cost_s=cost_s,
        work_s=work_s,
    )


@pytest.fixture
async def relightable(tmp_path: Path) -> dict[str, Any]:
    """The real receipt from the real halt path, for the real relightable class."""
    app = _armed_app(tmp_path)
    _, verdict = await _unenforced_promotion(app, kinds={"429": 3, "TimeoutError": 1})
    assert verdict.tripped is True
    return _halt(app, verdict)


# --------------------------------------------------------------------------- #
# TEST 1 — the lifecycle-class halt relights, and the reconcile RUNS first
# --------------------------------------------------------------------------- #


async def test_unenforced_lifecycle_quarantine_produces_a_relightable_receipt(
    tmp_path: Path,
) -> None:
    """The whole chain, from a real Kalshi payload to a GRANT.

    The halt is the ONE lifecycle-origin whole-bot halt left after the
    2026-07-26 rebuild: we saw a lifecycle move, quarantined the market, and
    could not PROVE our resting quotes came off it."""
    app = _armed_app(tmp_path)
    third, verdict = await _unenforced_promotion(
        app, kinds={"429": 3, "TimeoutError": 1}
    )
    assert third.changed_markets == (CLETB,)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_METADATA_CHANGE

    receipt = _halt(app, verdict)
    assert receipt["schema_version"] == HALT_RECEIPT_SCHEMA_VERSION
    assert receipt["reason"] == "halt_metadata_change"
    assert receipt["tripwire_hit"] is None
    assert receipt["halt_class"] == "lifecycle_quarantine_unenforced"
    assert receipt["root_cause_signature"] == "quarantine_unenforced:429,TimeoutError"

    # The evidence RE-DERIVES the claim: settlement fingerprint byte-identical,
    # not re-graded, a modelled LIFECYCLE status transition, quarantine armed.
    ev = receipt["evidence"][CLETB]
    assert ev["origin"] == "quarantine_unenforced"
    assert ev["settlement_fp_prior"] == ev["settlement_fp_new"]
    assert ev["status_prior"] == "active" and ev["status_new"] == "inactive"
    assert ev["status_class"] == "lifecycle"
    assert ev["regraded"] is False and ev["settlement_moved"] is False
    assert ev["quarantine_armed"] is True
    assert ev["withdraw_failure_kinds"] == {"429": 3, "TimeoutError": 1}

    # ...and the marker the relight will run INTO was dropped by the OTHER path.
    assert (tmp_path / "needs_reconcile").read_text(encoding="utf-8") == (
        "halt_metadata_change"
    )

    decision = _decide(receipt)
    assert decision.grant is True, decision.detail
    assert decision.halt_class == "lifecycle_quarantine_unenforced"
    assert decision.tickers == (CLETB,)


async def test_relight_runs_into_a_mandatory_exchange_reconcile(
    tmp_path: Path, relightable: dict[str, Any]
) -> None:
    """HAZARD 3. The relight cannot skip the cure, and the cure is not a no-op:
    with the marker present, the restarted bot ENUMERATES its open quotes from
    the exchange and DELETES every one before ``_book_reconciled`` is ever set.

    The relight does not *skip* the remedy — the relight IS the remedy."""
    assert _decide(relightable).grant is True
    marker = tmp_path / "needs_reconcile"
    assert marker.exists() is True  # G6's precondition, written by the halt

    class _RestWithLeftovers(FakeRest):
        async def get_quotes(self, **params: Any) -> dict[str, Any]:
            return {"quotes": [{"id": "leftover-1"}, {"id": "leftover-2"}]}

    # BEFORE the reconcile, the prod preflight refuses to quote at all: the
    # ``book_reconciled`` gate is red. This is the "before quoting resumes" half.
    app = _prod_app_for_preflight(tmp_path, reconciled=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    try:
        with pytest.raises(PreflightError, match="book_reconciled"):
            app._run_prod_preflight()

        # The reconcile itself is a real exchange round-trip, not a no-op: it
        # ENUMERATES the account's open quotes and DELETES every one.
        rest = _RestWithLeftovers()
        await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]
        assert rest.deleted == ["leftover-1", "leftover-2"]
        assert app._book_reconciled is True   # ...and only then is the gate green
        assert marker.exists() is False       # cleared by the PROOF, not by the relighter
    finally:
        monkeypatch.undo()


def test_the_relighter_can_never_suppress_the_reconcile(tmp_path: Path) -> None:
    """Structural, not procedural: the relighter relaunches the IDENTICAL argv
    (no flag, no env, no code path that could bypass
    ``_block_restart_until_reconciled``), and its module source contains no
    write to KILL or needs_reconcile."""
    argv = build_child_argv(
        env="prod", mode="quote", confirm_live=True, config="config/x.yaml"
    )
    assert argv[1:] == [
        "-m",
        "combomaker.ops.cli",
        "run",
        "--env",
        "prod",
        "--mode",
        "quote",
        "--confirm-live",
        "--config",
        "config/x.yaml",
    ]
    import combomaker.ops.relight as relight_mod

    source = Path(relight_mod.__file__).read_text(encoding="utf-8")
    # The relighter may READ the KILL file and the reconcile marker (G5/G6 are
    # read-only predicates); it must never create, write, or remove either.
    for forbidden in (
        "_marker.set",       # ReconcileMarker.set — dropping a marker
        "_marker.clear",     # ReconcileMarker.clear — clearing one
        "_kill_file.write",
        "_kill_file.unlink",
        "needs_reconcile\").write",
        "needs_reconcile\").unlink",
    ):
        assert forbidden not in source, f"relighter must not {forbidden}"
    # The only unlink CALL in the module is the receipt's (G1's premise).
    assert source.count(".unlink(") == 1
    assert "_receipt_path.unlink(" in source


def test_relighter_write_set_is_exactly_three_paths(tmp_path: Path) -> None:
    """§10.5 write-set test, measured rather than asserted from the source: run
    a full grant cycle against a fake child and diff the directory."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")
    before = {p.name for p in data.iterdir()}

    receipt_written: dict[str, Any] = {}

    class _Child:
        pid = 999

        def __init__(self, env: dict[str, str]) -> None:
            # A "bot" that halts on the relightable class the instant it starts.
            (data / HALT_RECEIPT_FILENAME).write_text(
                json.dumps({**receipt_written, "run_id": env[RUN_ID_ENV], "pid": 999, "ppid": 999}),
                encoding="utf-8",
            )
            self._polls = 0

        def poll(self) -> int | None:
            self._polls += 1
            return 0 if self._polls > 1 else None

    receipt_written.update(_synthetic_receipt())
    relighter = Relighter(
        data_dir=data,
        kill_file=tmp_path / "KILL",
        child_argv=["python", "-c", "pass"],
        wedge_timeout_s=30.0,
        spawn=lambda argv, env: _Child(env),
        sleep=lambda s: None,
    )
    # One grant, then the SAME signature ⇒ B2 refuses ⇒ terminal.
    assert relighter.run(poll_s=0.0) == 1
    after = {p.name for p in data.iterdir()}
    # The receipt is the BOT's write (the fake child above wrote it); the
    # relighter only ever unlinks it. Everything the RELIGHTER created is
    # exactly the ledger and the status file.
    assert after - before - {HALT_RECEIPT_FILENAME} == {
        "relight_ledger.jsonl",
        "relight_status.json",
    }
    # It left the last receipt in place for the human it just escalated to.
    assert HALT_RECEIPT_FILENAME in after
    assert "KILL" not in after
    assert (data / "needs_reconcile").read_text(encoding="utf-8") == (
        "halt_metadata_change"
    )  # never written, never cleared
    assert (tmp_path / "KILL").exists() is False


def _synthetic_receipt(**over: Any) -> dict[str, Any]:
    """A minimal well-formed relightable receipt, for tests that need to move
    one field at a time without driving the whole metadata lane."""
    base: dict[str, Any] = {
        "schema_version": HALT_RECEIPT_SCHEMA_VERSION,
        "run_id": "RUNID",
        "pid": 4242,
        "ppid": 4242,
        "written_at": "2026-07-27T12:00:00+00:00",
        "reason": "halt_metadata_change",
        "verdict_detail": f"settlement-relevant metadata changed: {CLETB}",
        "tripwire_hit": None,
        "halt_class": "lifecycle_quarantine_unenforced",
        "evidence": {
            CLETB: {
                "origin": "quarantine_unenforced",
                "quarantine_detail": "lifecycle change active->inactive",
                "settlement_fp_prior": "FP",
                "settlement_fp_new": "FP",
                "lifecycle_fp_prior": "L1",
                "lifecycle_fp_new": "L2",
                "status_prior": "active",
                "status_new": "inactive",
                "status_class": "lifecycle",
                "regraded": False,
                "settlement_moved": False,
                "quarantine_armed": True,
                "withdraw_failure_kinds": {"429": 3},
            }
        },
        "root_cause_signature": "quarantine_unenforced:429",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# TEST 2 — everything else is refused, and the KILL is left for a human
# --------------------------------------------------------------------------- #


async def test_settlement_class_halt_does_not_relight(tmp_path: Path) -> None:
    """A PAYOFF change (total 5.5 -> 6.5 with the rule rewritten to match) is
    the single worst thing to auto-relight through. It carries the SAME
    ReasonCode as the lifecycle promotion, so only the re-derivation can tell
    them apart — and it does, on the evidence, not on the label."""
    app = _armed_app(tmp_path)
    _seed(app, _CLETB_BEFORE)
    second = _seed(
        app,
        _mutated(
            floor_strike=6.5,
            rules_primary=_CLETB_BEFORE["rules_primary"].replace("5.5", "6.5"),
        ),
    )
    verdict = _breakers(app).evaluate(second)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_METADATA_CHANGE  # same code!

    receipt = _halt(app, verdict)
    assert receipt["halt_class"] == "settlement_metadata_change"
    ev = receipt["evidence"][CLETB]
    assert ev["origin"] == "settlement"
    assert ev["settlement_fp_prior"] != ev["settlement_fp_new"]

    decision = _decide(receipt)
    assert decision.grant is False
    assert decision.gate == "G7"
    assert "requires 'quarantine_unenforced'" in decision.detail

    # ...and the stronger property: a receipt that LIES — claiming the lifecycle
    # class and the lifecycle origin while still carrying a MOVED settlement
    # fingerprint — is rejected by its own contents. The reader never trusts
    # ``halt_class``; it re-derives the verdict from the evidence.
    liar = copy.deepcopy(receipt)
    liar["halt_class"] = "lifecycle_quarantine_unenforced"
    liar["evidence"][CLETB]["origin"] = "quarantine_unenforced"
    liar["evidence"][CLETB]["status_class"] = "lifecycle"
    liar["evidence"][CLETB]["settlement_moved"] = False
    caught = _decide(liar)
    assert caught.grant is False
    assert caught.gate == "G7"
    assert "SETTLEMENT fingerprint moved" in caught.detail


async def test_regrade_and_dispute_do_not_relight(tmp_path: Path) -> None:
    """The other two settlement-lane entries: a graded result being REPLACED,
    and a status entering ``disputed`` (the grade contested). Both share the
    ReasonCode; both are refused on their own evidence."""
    for label, before, after in (
        (
            "regrade",
            _mutated(result="no"),
            _mutated(result="yes"),
        ),
        (
            "disputed",
            _CLETB_BEFORE,
            _mutated(status="disputed"),
        ),
    ):
        app = _armed_app(tmp_path / label)
        _seed(app, before)
        verdict = _breakers(app).evaluate(_seed(app, after))
        assert verdict.tripped is True, label
        receipt = _halt(app, verdict)
        decision = _decide(receipt)
        assert decision.grant is False, label
        assert decision.gate == "G7", (label, decision.detail)


def test_taxonomy_tripwire_does_not_relight() -> None:
    """The THIRD user of HALT_METADATA_CHANGE: a pinned exchange-impossible
    shape became constructible, i.e. the validator changed under us."""
    receipt = _synthetic_receipt(
        tripwire_hit=["two_leg_same_market", "both sides of one market priced"],
        halt_class="taxonomy_tripwire",
    )
    decision = _decide(receipt)
    assert decision.grant is False
    assert decision.gate == "G7"
    assert "taxonomy tripwire" in decision.detail


@pytest.mark.parametrize(
    "reason",
    [
        "halt_reconciliation_mismatch",
        "halt_rate_limit_burst",
        "halt_drawdown",
        "halt_fill_velocity",
        "halt_hard_trip",
        "halt_data_stale",
        "halt_unmapped_game",
    ],
)
def test_other_hard_halts_do_not_relight(reason: str) -> None:
    """Every other in-process HARD halt writes a receipt too — with its own
    reason — and is refused at G7 before any policy lookup happens."""
    receipt = _synthetic_receipt(reason=reason, halt_class=reason)
    decision = _decide(receipt, marker_body=reason)
    assert decision.grant is False
    assert decision.gate in {"G6", "G7"}


def test_supervisor_heartbeat_kill_does_not_relight() -> None:
    """The supervisor's stall/wedge kill writes a KILL FILE. G5 refuses on the
    KILL alone — no attribution, no parsing, no way to get it wrong. (This is
    the class that actually fired three times in 30 h: ``supervisor kill: loop
    stalled: maintenance age=30.9s > 30.5s``.)"""
    decision = _decide(_synthetic_receipt(), kill=True)
    assert decision.grant is False
    assert decision.gate == "G5"
    assert "a human owns it" in decision.detail


def test_human_kill_of_unknown_provenance_does_not_relight() -> None:
    """A hand-written KILL, or ``combomaker halt``, carries only free text —
    unattributable by design. It does not need to be attributed: its mere
    PRESENCE is an unconditional refusal, and the relighter never deletes it."""
    for body in ("halt requested via CLI\n", "", "no idea who wrote this"):
        decision = _decide(_synthetic_receipt(), kill=True)
        assert decision.grant is False, body
        assert decision.gate == "G5", body


def test_stop_bot_and_crash_leave_no_receipt() -> None:
    """STOP_BOT force-kills the process and a crash never reaches ``on_halt``:
    either way there is no receipt (G1) and/or a non-zero exit (G4)."""
    assert _decide(None).gate == "G1"                     # killed: no receipt
    assert _decide(_synthetic_receipt(), exit_code=1).gate == "G4"   # crash
    assert _decide(_synthetic_receipt(), exit_code=3).gate == "G4"   # red preflight
    assert _decide(_synthetic_receipt(), exit_code=2).gate == "G4"   # credentials
    assert _decide(_synthetic_receipt(), exit_code=None).gate == "G4"


def test_stale_or_foreign_receipts_are_refused() -> None:
    """G2/G3 — integrity against CONFUSION (a stale file, a second stack, a
    receipt from the previous run), which is the correct threat model: anyone
    who can write the receipt can already write KILL."""
    # A receipt from the PREVIOUS run carries the previous nonce.
    stale = _synthetic_receipt(run_id="PREVIOUS")
    assert _decide(stale, run_id="CURRENT", stamp=False).gate == "G2"
    # A hand-written receipt carries no nonce at all.
    assert _decide(_synthetic_receipt(run_id=""), stamp=False).gate == "G2"
    # A receipt written by a SECOND stack: right shape, unrelated process.
    other_stack = _synthetic_receipt(run_id="RUNID", pid=111, ppid=112)
    assert _decide(other_stack, run_id="RUNID", pid=222, stamp=False).gate == "G3"
    # And an empty expected nonce (the relighter did not mint one) refuses too,
    # so a bot started by hand can never be relit off someone else's receipt.
    assert _decide(_synthetic_receipt(run_id=""), run_id="", stamp=False).gate == "G2"


def test_g3_accepts_the_venv_launcher_shim_level() -> None:
    """REGRESSION (found 2026-07-27 by running the relighter against REAL OS
    processes, not fakes — the fake-child tests all passed).

    On Windows ``.venv\\Scripts\\python.exe`` is a LAUNCHER SHIM that re-spawns
    the real interpreter as a child with an identical command line. This is the
    same measured topology ``start_all.ps1`` compensates for when it counts bot
    ROOTS ("one sleeper launch = 2 processes"). So the pid the relighter spawns
    is the halting bot's PARENT, and a G3 that compared only ``os.getpid()``
    refused EVERY real receipt — the whole feature would have been a silent
    no-op live, passing every test.

    Observed in the smoke run: receipt ``pid=24696 ppid=36808``, spawned 36808.
    """
    spawned = 36808
    # The real shape: we are the shim's child, so the spawned pid is our PARENT.
    via_parent = _synthetic_receipt(run_id="RUNID", pid=24696, ppid=spawned)
    assert _decide(via_parent, run_id="RUNID", pid=spawned, stamp=False).grant is True
    # No shim (POSIX, or a direct interpreter): we ARE the spawned process.
    direct = _synthetic_receipt(run_id="RUNID", pid=spawned, ppid=999)
    assert _decide(direct, run_id="RUNID", pid=spawned, stamp=False).grant is True
    # Exactly ONE level is accepted — an unrelated process still refuses, so the
    # gate is loosened by a measured fact, not blunted.
    stranger = _synthetic_receipt(run_id="RUNID", pid=1, ppid=2)
    assert _decide(stranger, run_id="RUNID", pid=spawned, stamp=False).gate == "G3"
    # A receipt with no ppid at all (an older/hand-written one) refuses.
    legacy = _synthetic_receipt(run_id="RUNID", pid=24696)
    legacy.pop("ppid")
    assert _decide(legacy, run_id="RUNID", pid=spawned, stamp=False).gate == "G3"


async def test_the_bot_records_both_pids(tmp_path: Path) -> None:
    """The other half of the same regression: the receipt the REAL halt path
    writes must carry both identities, or G3 has nothing to match on."""
    import os

    app = _armed_app(tmp_path)
    _, verdict = await _unenforced_promotion(app, kinds={"429": 1})
    receipt = _halt(app, verdict)
    assert receipt["pid"] == os.getpid()
    assert receipt["ppid"] == os.getppid()


def test_missing_reconcile_marker_is_refused() -> None:
    """G6, both halves. The marker must be PRESENT (so the relight can only
    ever run into a mandatory reconcile) and its body — written by
    ``mark_reconcile_on_hard_halt``, a DIFFERENT code path than the receipt —
    must agree with the receipt's reason."""
    assert _decide(_synthetic_receipt(), marker=False).gate == "G6"
    assert _decide(_synthetic_receipt(), marker_body="halt_drawdown").gate == "G6"
    assert _decide(_synthetic_receipt(), marker_body=None).gate == "G6"


def test_unarmed_quarantine_and_unmodelled_status_stay_human() -> None:
    """§3 / decision D2. ``unarmed`` is a WIRING BUG (a restart reproduces it)
    and an unmodelled status string is an exchange-SEMANTICS change (a restart
    cannot know more than we did). Neither is relightable, and each is caught by
    its own evidence field rather than by its label."""
    unarmed = _synthetic_receipt()
    unarmed["evidence"][CLETB] = {
        **unarmed["evidence"][CLETB],
        "origin": "unarmed",
        "quarantine_armed": False,
    }
    got = _decide(unarmed)
    assert got.grant is False and got.gate == "G7"

    unmodelled = _synthetic_receipt()
    unmodelled["evidence"][CLETB] = {
        **unmodelled["evidence"][CLETB],
        "origin": "settlement",
        "status_class": "settlement",
        "status_new": "some_new_kalshi_enum",
    }
    got2 = _decide(unmodelled)
    assert got2.grant is False and got2.gate == "G7"


def test_policy_is_a_code_constant_not_config() -> None:
    """G8 exists so a NEW halt class cannot become relightable by default. The
    set is a frozenset in code — reviewable in a diff, not tunable in YAML."""
    assert _RELIGHTABLE_HALT_CLASSES == frozenset({"lifecycle_quarantine_unenforced"})
    assert isinstance(_RELIGHTABLE_HALT_CLASSES, frozenset)
    # A receipt whose evidence passes G7 but whose class is not in the set is
    # still refused — the two are independent gates.
    sneaky = _synthetic_receipt(halt_class="lifecycle_quarantine_unenforced_v2")
    assert _decide(sneaky).gate == "G8"


def test_malformed_receipts_default_to_deny() -> None:
    """Default deny: anything missing, mistyped, or from a future schema."""
    assert _decide("not a dict").gate == "G1"
    assert _decide({}).gate == "G1"
    assert _decide(_synthetic_receipt(schema_version=2)).gate == "G1"
    assert _decide(_synthetic_receipt(evidence={})).gate == "G7"
    assert _decide(_synthetic_receipt(verdict_detail="no colon here")).gate == "G7"
    # A halted market with NO evidence entry (the detail names a ticker the
    # evidence does not cover) is refused, not waved through.
    assert _decide(
        _synthetic_receipt(
            verdict_detail=f"settlement-relevant metadata changed: {CLETB}, OTHER-MKT"
        )
    ).gate == "G7"
    assert _decide(_synthetic_receipt(root_cause_signature="")).gate == "G7"


async def test_the_five_historical_halts_all_refuse(tmp_path: Path) -> None:
    """§10.2. All five real metadata halts on record (7/25 18:50, 7/25 20:06,
    7/26 00:30, 7/26 14:15, 7/26 18:15) were SETTLEMENT-lane — their details
    name game/total markets whose payoff terms moved. Replayed as receipts,
    every one refuses."""
    details = [
        "settlement-relevant metadata changed: KXMLBGAME-26JUL252005DETKC-DET",
        "settlement-relevant metadata changed: KXMLBTOTAL-26JUL252005DETKC-8",
        "settlement-relevant metadata changed: KXMLBTOTAL-26JUL261215CLETB-6",
        (
            "settlement-relevant metadata changed: KXMLBGAME-26JUL252005DETKC-DET, "
            "KXMLBTOTAL-26JUL252005DETKC-8"
        ),
        "taxonomy tripwire two_leg_same_market: pinned shape became constructible",
    ]
    for i, detail in enumerate(details):
        tickers = (
            []
            if detail.startswith("taxonomy")
            else [t.strip() for t in detail.split(":", 1)[1].split(",")]
        )
        receipt = _synthetic_receipt(
            verdict_detail=detail,
            halt_class="settlement_metadata_change",
            tripwire_hit=(
                ["two_leg_same_market", "pinned shape"] if not tickers else None
            ),
            evidence={
                t: {
                    "origin": "settlement",
                    "settlement_fp_prior": "A",
                    "settlement_fp_new": "B",   # the payoff MOVED
                    "status_class": "settlement",
                    "regraded": False,
                    "settlement_moved": True,
                    "quarantine_armed": True,
                    "withdraw_failure_kinds": {},
                }
                for t in tickers
            },
        )
        decision = _decide(receipt)
        assert decision.grant is False, (i, detail)
        assert decision.gate == "G7", (i, detail, decision.detail)


# --------------------------------------------------------------------------- #
# TEST 3 — the crash loop is BOUNDED (B2 and B3, independently)
# --------------------------------------------------------------------------- #


def test_b2_one_relight_per_distinct_root_cause_per_session() -> None:
    """B2 — NOVELTY. The signature is computed at the FAILURE SITE (the set of
    distinct withdrawal failure kinds), never chosen by policy. A repeat of the
    same root cause means the bot has PROVEN it cannot self-cure that mode.

    The session total is therefore bounded by the number of distinct exchange
    failure modes actually observed — each granted exactly one attempt."""
    session = RelightSession()
    first = _decide(_synthetic_receipt(), session=session)
    assert first.grant is True
    session.granted_signatures.add(first.signature)
    session.relights += 1

    # Same root cause, DIFFERENT market: still refused. The bound is on the
    # failure mode, not on the ticker.
    repeat = _synthetic_receipt(
        verdict_detail="settlement-relevant metadata changed: KXMLBGAME-26JUL27ABCDEF-ABC"
    )
    repeat["evidence"] = {
        "KXMLBGAME-26JUL27ABCDEF-ABC": repeat["evidence"][CLETB]
    }
    got = _decide(repeat, session=session, cost_s=36.0, work_s=600.0)
    assert got.grant is False
    assert got.gate == "B2"
    assert "cannot self-cure" in got.detail

    # A genuinely DIFFERENT exchange failure mode is a different root cause and
    # gets its own single attempt.
    novel = _synthetic_receipt(root_cause_signature="quarantine_unenforced:503")
    assert _decide(novel, session=session, cost_s=36.0, work_s=600.0).grant is True


def test_b3_amortization_refuses_the_first_unpaid_repeat() -> None:
    """B3 — AMORTIZATION. "A relight must be paid for by observed productive
    work" has exactly ONE scale-free breakeven: work_i >= cost_i. Any multiple
    (1.5x, 3x) would be a number a human must move.

    Consequence: a true crash loop has work -> 0 while cost stays at the
    measured boot time, so it is refused on the FIRST repeat. The mechanism can
    emit at most ONE unpaid relight, ever, and the duty cycle can never fall
    below 50%."""
    session = RelightSession(granted_signatures=set(), relights=1)
    novel = _synthetic_receipt(root_cause_signature="quarantine_unenforced:503")
    # Measured boot cost on the live run (05:32:32 ET start -> startup_reconciled
    # 09:33:06.96Z -> prod_preflight_green 09:33:08.07Z) is ~36 s.
    cost = 36.0
    # Never productive at all — the pure crash loop.
    got = _decide(novel, session=session, cost_s=cost, work_s=None)
    assert got.grant is False and got.gate == "B3"
    assert "0.0s of proven work" in got.detail
    # Productive, but for LESS than it cost to get there.
    got2 = _decide(novel, session=session, cost_s=cost, work_s=35.9)
    assert got2.grant is False and got2.gate == "B3"
    # Exactly break-even is PAID.
    assert _decide(novel, session=session, cost_s=cost, work_s=36.0).grant is True


def test_crash_loop_terminates_in_human_escalation(tmp_path: Path) -> None:
    """§10.4 — the whole mechanism against a child that re-halts IMMEDIATELY,
    every time, with a fake clock. Exactly ONE relight is emitted, then the
    process EXITS NON-ZERO into a human escalation. It never loops."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")

    launches: list[str] = []

    class _InstantHalter:
        """Boots (costing 2 fake seconds), becomes productive, then halts 1 fake
        second later with the SAME root cause — work_i (1s) < cost_i (2s)."""

        def __init__(self, env: dict[str, str]) -> None:
            self.pid = 1000 + len(launches)
            launches.append(env[RUN_ID_ENV])
            self._n = 0
            self._env = env

        def poll(self) -> int | None:
            self._n += 1
            if self._n <= 3:
                return None
            (data / HALT_RECEIPT_FILENAME).write_text(
                json.dumps(
                    {
                        **_synthetic_receipt(),
                        "run_id": self._env[RUN_ID_ENV],
                        "pid": self.pid,
                        "ppid": self.pid,
                    }
                ),
                encoding="utf-8",
            )
            return 0

    class _FakeClock:
        """Monotonic ticks 1 fake second per read; wall is irrelevant here."""

        def __init__(self) -> None:
            self._ns = 0

        def monotonic_ns(self) -> int:
            self._ns += 1_000_000_000
            return self._ns

        def now(self) -> Any:
            from datetime import UTC, datetime

            return datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    relighter = Relighter(
        data_dir=data,
        kill_file=tmp_path / "KILL",
        child_argv=["python", "-c", "pass"],
        wedge_timeout_s=30.0,
        clock=_FakeClock(),
        spawn=lambda argv, env: _InstantHalter(env),
        sleep=lambda s: None,
    )
    # The marker is present the whole time, so `is_productive()` reads False and
    # no run ever becomes productive: work is always None.
    exit_code = relighter.run(poll_s=0.0)

    assert exit_code == 1                       # TERMINAL, not a loop
    assert len(launches) == 2                   # one original + exactly ONE relight
    assert relighter._session.relights == 1     # the single unpaid relight, ever

    ledger = [
        json.loads(line)
        for line in (data / "relight_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["grant"] for r in ledger] == [True, False]
    assert ledger[1]["gate"] == "B2"            # same root cause ⇒ novelty bound
    status = json.loads((data / "relight_status.json").read_text(encoding="utf-8"))
    assert status["grant"] is False and status["gate"] == "B2"


# --------------------------------------------------------------------------- #
# TEST 4 — a FAILED reconcile stays down
# --------------------------------------------------------------------------- #


async def test_failed_reconcile_stays_down_and_escalates(tmp_path: Path) -> None:
    """HAZARD 3, the fail-closed leg. If the reconcile round-trip FAILS the
    marker STAYS, ``_book_reconciled`` stays False, the prod preflight goes RED,
    ``cli.main`` returns 3 — and G4 refuses a second relight. A bot that cannot
    reach the exchange is never relit twice."""
    (tmp_path / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")
    app = _armed_app(tmp_path)
    rest = FakeRest(fail=True)  # exchange unreachable
    await app._block_restart_until_reconciled(rest, _reservation())  # type: ignore[arg-type]

    assert app._book_reconciled is False               # never unblocked
    assert (tmp_path / "needs_reconcile").exists()     # marker stays in force
    assert rest.deleted == []                          # nothing was assumed gone

    # The preflight that gate turns red exits 3 (cli.py PreflightError) — and 3
    # is not a clean halt-stop, so the relighter refuses.
    refused = _decide(_synthetic_receipt(), exit_code=3)
    assert refused.grant is False and refused.gate == "G4"

    # And even on a clean exit, a still-present marker whose body is the halt
    # reason is REQUIRED — the relight can only run into a mandatory reconcile.
    assert _decide(_synthetic_receipt(), marker=False).gate == "G6"


async def test_kill_file_outranks_a_successful_reconcile(tmp_path: Path) -> None:
    """Defense in depth already in the tree: even if the reconcile succeeds,
    a KILL on disk means the marker is NOT cleared and the book is NOT marked
    reconciled — so the relighter's G5 and the bot's own gate agree."""
    (tmp_path / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")
    (tmp_path / "KILL").write_text("supervisor kill: loop stalled", encoding="utf-8")
    app = _armed_app(tmp_path)
    await app._block_restart_until_reconciled(FakeRest(), _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is False
    assert (tmp_path / "needs_reconcile").exists() is True
    assert _decide(_synthetic_receipt(), kill=True).gate == "G5"


# --------------------------------------------------------------------------- #
# TEST 5 — flapping vs a normal slate with several end-of-game waves
# --------------------------------------------------------------------------- #


def test_normal_slate_waves_are_not_flapping() -> None:
    """The distinction the design must get right.

    FLAPPING  = the same root cause, immediately, with no work in between.
    NORMAL    = end-of-game lifecycle waves across a 15-game MLB night, each
                separated by long productive quoting, each with its own
                exchange failure mode.

    B2 keys on the ROOT CAUSE (the set of distinct withdrawal failure kinds),
    not on the count of halts, and B3 keys on OBSERVED PRODUCTIVE WORK, not on
    elapsed time. So a normal slate spends budget only when the exchange
    genuinely fails a NEW way, and never exhausts it by having many games."""
    session = RelightSession()
    boot_cost = 36.0          # measured on the live run
    inning_of_work = 1800.0   # half an hour of proven quoting between waves

    # Wave 1: the exchange 429s during the end-of-game withdrawal burst.
    w1 = _synthetic_receipt(root_cause_signature="quarantine_unenforced:429")
    d1 = _decide(w1, session=session, cost_s=None, work_s=None)
    assert d1.grant is True
    session.granted_signatures.add(d1.signature)
    session.relights += 1

    # Wave 2, another game, 30 min later: the exchange TIMES OUT this time — a
    # different failure mode, amply amortized.
    w2 = _synthetic_receipt(root_cause_signature="quarantine_unenforced:TimeoutError")
    d2 = _decide(w2, session=session, cost_s=boot_cost, work_s=inning_of_work)
    assert d2.grant is True, d2.detail
    session.granted_signatures.add(d2.signature)
    session.relights += 1

    # Wave 3: 5xx. Still novel, still paid for.
    w3 = _synthetic_receipt(root_cause_signature="quarantine_unenforced:503")
    d3 = _decide(w3, session=session, cost_s=boot_cost, work_s=inning_of_work)
    assert d3.grant is True
    session.granted_signatures.add(d3.signature)
    session.relights += 1
    assert session.relights == 3  # three waves, three grants, budget intact

    # ...but the 429 mode RECURRING — even after a full half-hour of work — is
    # refused: the bot already proved once that it cannot self-cure that one.
    assert _decide(w1, session=session, cost_s=boot_cost, work_s=inning_of_work).gate == "B2"

    # And a novel mode that arrives with NO work behind it is flapping, refused
    # by the other bound entirely.
    w4 = _synthetic_receipt(root_cause_signature="quarantine_unenforced:budget_deferred")
    assert _decide(w4, session=session, cost_s=boot_cost, work_s=0.5).gate == "B3"


def test_duty_cycle_can_never_fall_below_half() -> None:
    """The algebraic consequence of B3, stated as a property: every granted
    relight is preceded by a run whose productive span was at least its own
    boot cost, so cycle length >= 2*cost and duty >= 50%."""
    session = RelightSession()
    for cost, work in ((36.0, 36.0), (36.0, 100.0), (5.0, 5.0), (120.0, 121.0)):
        sig = f"quarantine_unenforced:{cost}-{work}"
        got = _decide(
            _synthetic_receipt(root_cause_signature=sig),
            session=session,
            cost_s=cost,
            work_s=work,
        )
        assert got.grant is True
        assert work / (cost + work) >= 0.5
        session.granted_signatures.add(sig)


def test_productive_requires_the_reconcile_to_have_cleared(tmp_path: Path) -> None:
    """"Productive" is DEFINED as marker-absent, so ``work`` cannot start
    accruing until the exchange reconcile has provably succeeded. Amortization
    and reconcile-proof are the SAME measurement."""
    from datetime import UTC, datetime

    data = tmp_path / "data"
    data.mkdir()

    class _Clock:
        def monotonic_ns(self) -> int:
            return 0

        def now(self) -> Any:
            return datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    relighter = Relighter(
        data_dir=data,
        kill_file=tmp_path / "KILL",
        child_argv=["python"],
        wedge_timeout_s=30.0,
        clock=_Clock(),
        spawn=lambda argv, env: None,
        sleep=lambda s: None,
    )
    (data / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")
    (data / "heartbeat.txt").write_text(
        datetime(2026, 7, 27, 12, 0, tzinfo=UTC).isoformat(), encoding="utf-8"
    )
    assert relighter.is_productive() is False  # marker present ⇒ not yet proven
    (data / "needs_reconcile").unlink()
    assert relighter.is_productive() is True   # reconcile proven ⇒ work accrues
    # A wedged heartbeat is not productive either, marker or no marker.
    (data / "heartbeat.txt").write_text(
        datetime(2026, 7, 27, 11, 0, tzinfo=UTC).isoformat(), encoding="utf-8"
    )
    assert relighter.is_productive() is False


# --------------------------------------------------------------------------- #
# TEST 6 — observability
# --------------------------------------------------------------------------- #


def test_grant_and_escalation_emit_distinct_attributable_records(
    tmp_path: Path,
) -> None:
    """HAZARD 4. Every grant is ERROR-level and names the halt class, the
    markets, the root cause, the timings and the session count; every refusal
    emits BOTH ``relight_refused`` (with the full receipt echoed) and a DISTINCT
    ``relight_terminal_escalation`` telling a human exactly what to check.

    "Quietly relights 20 times overnight" is structurally impossible: 20 grants
    would require 20 DISTINCT exchange failure signatures, each paid for by
    proven productive quoting, each emitting one of these lines."""
    from structlog.testing import capture_logs

    data = tmp_path / "data"
    data.mkdir()
    (data / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")

    class _Child:
        def __init__(self, env: dict[str, str]) -> None:
            self.pid = 777
            self._n = 0
            self._env = env

        def poll(self) -> int | None:
            self._n += 1
            if self._n <= 1:
                return None
            (data / HALT_RECEIPT_FILENAME).write_text(
                json.dumps(
                    {
                        **_synthetic_receipt(),
                        "run_id": self._env[RUN_ID_ENV],
                        "pid": 777,
                        "ppid": 777,
                    }
                ),
                encoding="utf-8",
            )
            return 0

    relighter = Relighter(
        data_dir=data,
        kill_file=tmp_path / "KILL",
        child_argv=["python", "-c", "pass"],
        wedge_timeout_s=30.0,
        spawn=lambda argv, env: _Child(env),
        sleep=lambda s: None,
    )
    with capture_logs() as logs:
        assert relighter.run(poll_s=0.0) == 1
    by_event: dict[str, list[dict[str, Any]]] = {}
    for entry in logs:
        by_event.setdefault(str(entry.get("event")), []).append(entry)

    # The grant: ERROR level (an auto-restart is never routine) and fully
    # attributable — class, markets, root cause, timings, session count.
    grant = by_event["relight_granted"][0]
    assert grant["log_level"] == "error"
    assert grant["halt_class"] == "lifecycle_quarantine_unenforced"
    assert grant["tickers"] == [CLETB]
    assert grant["signature"] == "quarantine_unenforced:429"
    assert grant["session_relights"] == 1
    assert "relight_session_summary" in by_event

    # The escalation: a DISTINCT record, also ERROR, naming the gate and the
    # human's checklist — never the same line as the grant.
    refused = by_event["relight_refused"][0]
    assert refused["log_level"] == "error"
    assert refused["refusal_gate"] == "B2"
    assert refused["receipt"]["halt_class"] == "lifecycle_quarantine_unenforced"
    esc = by_event["relight_terminal_escalation"][0]
    assert esc["log_level"] == "error"
    assert esc["refusal_gate"] == "B2"
    assert "needs_reconcile" in esc["human_must_check"]
    assert "halt_receipt.json" in esc["human_must_check"]

    # Launch/exit bookends for every attempt.
    assert len(by_event["relight_child_launched"]) == 2
    assert len(by_event["relight_child_exited"]) == 2
    assert {e["log_level"] for e in by_event["relight_child_exited"]} == {"warning"}

    # The ledger is append-only, one record per DECISION, carrying every input —
    # so any decision is reconstructible offline without the log.
    ledger = [
        json.loads(line)
        for line in (data / "relight_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(ledger) == 2
    for record in ledger:
        assert set(record) >= {
            "at", "run_id", "pid", "attempt_index", "exit_code", "grant", "gate",
            "detail", "signature", "halt_class", "tickers", "kill_present",
            "reconcile_marker_present", "receipt",
        }
    assert ledger[0]["grant"] is True and ledger[0]["tickers"] == [CLETB]
    assert ledger[1]["grant"] is False


async def test_receipt_never_blocks_the_cure(tmp_path: Path) -> None:
    """The receipt is written FIRST in ``on_halt`` but is fully wrapped: a
    receipt failure must never stop ``cancel_all``. A missing receipt just means
    the relighter refuses, which is the default anyway."""
    app = _armed_app(tmp_path)
    app._halt_receipt_path = tmp_path / "nonexistent-dir-that-is-a-file" / "x.json"
    (tmp_path / "nonexistent-dir-that-is-a-file").write_text("blocker", encoding="utf-8")
    # Does not raise, even though the write cannot possibly succeed.
    app._write_halt_receipt(
        HaltEvent(ReasonCode.HALT_METADATA_CHANGE, "detail", "2026-07-27T12:00:00+00:00")
    )
    assert _decide(None).gate == "G1"


async def test_withdraw_failure_kinds_are_measured_not_configured(
    tmp_path: Path,
) -> None:
    """The signature's only new input. Kinds come from the exception itself —
    an HTTP status names itself, anything else names its class, and a
    budget-deferred quote (never asked at all) is its own mode."""
    from combomaker.exchange.rest import KalshiApiError, RateLimitedError
    from combomaker.rfq.lifecycle import _withdraw_failure_kind

    assert _withdraw_failure_kind(RateLimitedError(429, "rate", "slow down")) == "429"
    assert _withdraw_failure_kind(KalshiApiError(503, "unavail", "down")) == "503"
    assert _withdraw_failure_kind(TimeoutError()) == "TimeoutError"
    assert _withdraw_failure_kind(ConnectionResetError()) == "ConnectionResetError"
    assert _withdraw_failure_kind(None) == "budget_deferred"

    # ...and they reach the receipt through the enforcement pass, per market.
    app = _armed_app(tmp_path)
    _, verdict = await _unenforced_promotion(app, kinds={"503": 2, "budget_deferred": 9})
    receipt = _halt(app, verdict)
    assert receipt["root_cause_signature"] == (
        "quarantine_unenforced:503,budget_deferred"
    )
    assert receipt["evidence"][CLETB]["withdraw_failure_kinds"] == {
        "503": 2,
        "budget_deferred": 9,
    }


async def test_enforced_quarantine_leaves_no_halt_and_no_receipt_class(
    tmp_path: Path,
) -> None:
    """The happy path is unchanged: a quarantine we CAN enforce never promotes,
    so there is no halt to relight from at all."""
    app = _armed_app(tmp_path)
    third, verdict = await _unenforced_promotion(app, kinds={})
    assert third.changed_markets == ()      # enforced ⇒ no promotion
    assert verdict.tripped is False
    assert app._market_quarantine.unenforced() == ()


# --------------------------------------------------------------------------- #
# TEST 8 — the BOOT WINDOW. A healthy boot is longer than the wedge anchor.
# --------------------------------------------------------------------------- #


class _StepClock:
    """A clock both readers share, advanced explicitly by the fake child."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic_ns(self) -> int:
        return int(self.t * 1e9)

    def now(self) -> Any:
        from datetime import UTC, datetime, timedelta

        return datetime(2026, 7, 27, 12, 0, tzinfo=UTC) + timedelta(seconds=self.t)


def _boot_shaped_child(
    data: Path, clock: _StepClock, *, first_beat_s: float, reconcile_s: float
) -> Any:
    """A child with the MEASURED production boot shape: it cannot beat until its
    exchange reconcile has returned, because the bot's first
    ``heartbeat.beat()`` runs after ``_startup_book_risk_snapshot``."""

    class _Child:
        pid = 4242

        def poll(self) -> int | None:
            clock.t += 1.0
            if clock.t >= reconcile_s:
                (data / "needs_reconcile").unlink(missing_ok=True)
            if clock.t >= first_beat_s:
                (data / "heartbeat.txt").write_text(
                    clock.now().isoformat(), encoding="utf-8"
                )
                (data / "loop_progress.json").write_text(
                    json.dumps(
                        {
                            "written_at": clock.now().isoformat(),
                            "loops": {
                                "maintenance": {"age_s": 0.1, "stall_after_s": 30.5}
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            if clock.t >= first_beat_s + 120.0:
                (data / HALT_RECEIPT_FILENAME).write_text(
                    json.dumps(_synthetic_receipt(run_id="R", pid=4242, ppid=4242)),
                    encoding="utf-8",
                )
                return 0
            return None

    return _Child()


@pytest.mark.parametrize(
    ("label", "corpse_age_s"),
    [
        # A plain START_BOT: start_all.ps1 DELETES heartbeat.txt, so there is no
        # stamp at all and an absent heartbeat reads as WEDGED (fail-closed).
        ("start_bot_deletes_the_heartbeat", None),
        # An AUTO-RELIGHT: the dead bot beat right up to its halt, so its
        # leftover file is only seconds old and reads as NOT wedged. A latch
        # keyed on freshness would latch on this CORPSE and then fire the bound
        # the moment it aged out — abandoning the child even earlier.
        ("relight_inherits_a_still_warm_corpse", 10.0),
        # ...and the same corpse once it is already stale.
        ("relight_inherits_a_stale_corpse", 45.0),
    ],
)
def test_a_healthy_boot_is_never_abandoned(
    tmp_path: Path, label: str, corpse_age_s: float | None
) -> None:
    """REGRESSION (2026-07-27). The pre-beat bound used to run from LAUNCH
    against the operator's 30 s wedge anchor. But across ALL 26 recorded boots
    (``data/live_2026072*.log``) launch -> first beat takes 34.2 s - 46.1 s —
    not one is inside 30 s — because the first beat is emitted only after the
    startup exchange reconcile.

    So the relighter abandoned a HEALTHY child at t=30 s, refused at G1 citing
    "after child exit" while the child had NOT exited, exited 1, and left the
    bot running ORPHANED. Measured end to end, for BOTH launch shapes below.

    The bound must therefore be consulted only once THIS child has provably
    beaten — a stamp different from the one on disk when it was spawned.
    """
    from datetime import timedelta

    data = tmp_path / "data"
    data.mkdir()
    clock = _StepClock()
    (data / "needs_reconcile").write_text("halt_metadata_change", encoding="utf-8")
    if corpse_age_s is not None:
        (data / "heartbeat.txt").write_text(
            (clock.now() - timedelta(seconds=corpse_age_s)).isoformat(),
            encoding="utf-8",
        )

    relighter = Relighter(
        data_dir=data,
        kill_file=tmp_path / "KILL",
        child_argv=["python"],
        wedge_timeout_s=30.0,                       # the live operator anchor
        clock=clock,                                # type: ignore[arg-type]
        spawn=lambda argv, env: _boot_shaped_child(
            data, clock, first_beat_s=35.0, reconcile_s=33.8   # measured today
        ),
        sleep=lambda s: None,
    )
    exit_code, cost_s, work_s = relighter._watch(
        relighter._spawn([], {}), 0.0, relighter._heartbeat_stamp()
    )

    # The child ran to its own clean halt-stop; it was never abandoned.
    assert exit_code == 0, f"{label}: relighter abandoned a healthy boot"
    assert cost_s is not None and work_s is not None, f"{label}: never went productive"
    # Productive only AFTER the reconcile cleared AND this child beat — i.e.
    # past the 30 s anchor, which is exactly what used to be impossible.
    assert cost_s >= 35.0, f"{label}: cost_s={cost_s}"
