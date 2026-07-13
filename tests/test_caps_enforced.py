"""ENFORCED R2 caps (wire-live 2026-07-13): with ``caps_shadow_mode`` now False
by default, the %-of-bankroll caps + the give-back KILL actually block/halt.

The three acceptance cases the wiring task requires, PLUS the two
fail-closed-without-bricking guarantees:

1. A normal small demo book quotes fine (a genuinely-under-cap book is not
   blocked by the now-enforced caps).
2. A genuinely over-8%-of-bankroll GAME book blocks (the game cap enforces).
3. The 12% give-back arms and HALTs on a real drawdown (peak→current latch →
   HaltInputs → maintenance_tick → killswitch).
4. FRESH DEMO START (no balance/positions) still quotes normally: the give-back
   halt SKIPS when peak/current equity is unavailable (no invented peak).
5. STALE bankroll fails the %-caps CLOSED to a no-quote (SKIP_BANKROLL_
   UNAVAILABLE, enforced) — NOT a permanent halt (the killswitch stays clear).

Driven through the real ``QuoteLifecycle`` hot path so it proves the DEFAULT
wiring enforces (these build ``RiskLimits()`` with no shadow override, exercising
the flipped default), not just the flag.
"""

from __future__ import annotations

from combomaker.core.reasons import ReasonCode
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import rfq
from tests.test_limits_caps import CONVENTIONS, MARG
from tests.test_risk_shadow_mode import _build_lifecycle, harness  # noqa: F401

# The rfq() helper quotes a 2-leg combo M1×M2 at ~1.00 contract; its NO premium
# is a few cents, so the per-position max_loss is small. Bankroll knobs below are
# chosen so exactly the intended cap binds.

# $2,000 bankroll: the default game cap (8%) = $160 = 1_600_000cc. The rfq()
# NO-fill max_loss is tiny (< $1), so a $2,000 book quotes cleanly.
BANKROLL_2K = 20_000_000
# A bankroll so tiny ($0.02 = 200cc) that even the rfq()'s few-cent NO premium
# blows past 8% of it → the enforced game cap blocks. (8% of 200cc = 16cc; the
# quote's mass-acceptance NO worst-case loss is far above 16cc.)
BANKROLL_TINY = 200


async def test_normal_small_demo_book_quotes_fine(
    harness: tuple[Harness, Store],  # noqa: F811
) -> None:
    # DEFAULT limits (caps ENFORCED) + an ample $2,000 bankroll: a normal small
    # book is comfortably under every cap, so the quote goes out. This is the
    # do-not-brick guarantee for a healthy book under the flipped default.
    h, store = harness
    assert RiskLimits().caps_shadow_mode is False  # the wire-live default
    limits = LimitChecker(RiskLimits())  # no shadow override → ENFORCED default
    lifecycle, sender, exposure, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=BANKROLL_2K
    )
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1
    assert lifecycle.open_quote_count == 1
    assert not h.killswitch.halted


async def test_over_8pct_game_book_blocks(
    harness: tuple[Harness, Store],  # noqa: F811
) -> None:
    # DEFAULT limits (game cap 8% ENFORCED) + a $0.02 bankroll: the quote's
    # same-game NO worst-case loss exceeds 8% of the bankroll, so the enforced
    # game/mass-acceptance %-cap blocks — nothing is sent.
    h, store = harness
    limits = LimitChecker(RiskLimits())  # ENFORCED default
    lifecycle, sender, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=BANKROLL_TINY
    )
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []
    assert lifecycle.open_quote_count == 0


async def test_12pct_give_back_arms_and_halts_on_real_drawdown(
    harness: tuple[Harness, Store],  # noqa: F811
) -> None:
    # peak $2,000 → current $1,700 = 15% give-back, over BOTH the 10% drawdown and
    # the 12% hard-trip KILL. DEFAULT limits (ENFORCED) → the give-back halt
    # escalates to the killswitch through maintenance_tick. Hard-dollar daily cap
    # left far away so ONLY the give-back could fire.
    h, store = harness
    limits = LimitChecker(RiskLimits(max_daily_loss_dollars=1_000_000.0))
    lifecycle, _, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=BANKROLL_2K,
        peak_cc=BANKROLL_2K, current_cc=17_000_000,
    )
    await lifecycle.maintenance_tick()
    assert h.killswitch.halted
    assert h.killswitch.halt_event is not None
    # The deeper 12% give-back is the hard-trip KILL (human-only clear); it is the
    # first halt-class breach maintenance_tick escalates.
    assert h.killswitch.halt_event.reason in (
        ReasonCode.HALT_HARD_TRIP,
        ReasonCode.HALT_DRAWDOWN,
    )


async def test_fresh_demo_no_equity_does_not_halt_give_back(
    harness: tuple[Harness, Store],  # noqa: F811
) -> None:
    # FRESH DEMO: bankroll known but NO peak/current equity yet (a brand-new poll
    # cycle). The give-back halts must SKIP — no invented peak — so a fresh start
    # never self-halts. maintenance_tick runs cleanly, killswitch stays clear.
    h, store = harness
    limits = LimitChecker(RiskLimits(max_daily_loss_dollars=1_000_000.0))
    lifecycle, _, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=BANKROLL_2K,
        peak_cc=None, current_cc=None,  # no equity readings ⇒ give-back skips
    )
    await lifecycle.maintenance_tick()
    assert not h.killswitch.halted


async def test_stale_bankroll_no_quotes_but_never_halts(
    harness: tuple[Harness, Store],  # noqa: F811
) -> None:
    # STALE bankroll (risk_bankroll_cc_or_none() → None): the %-caps CANNOT be
    # computed, so they fail CLOSED to a no-quote (SKIP_BANKROLL_UNAVAILABLE,
    # enforced) on handle_rfq. But this is a NO-QUOTE, never a permanent halt —
    # maintenance_tick must NOT escalate it to the killswitch (a stale poll is
    # transient; a halt would brick until human clear). Both invariants here.
    h, store = harness
    limits = LimitChecker(RiskLimits())  # ENFORCED default
    lifecycle, sender, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=None  # stale
    )
    await lifecycle.handle_rfq(rfq())
    # Fail-closed no-quote: nothing sent.
    assert sender.created == []
    # ...but NOT a halt — the stale-bankroll breach is not halt-class.
    await lifecycle.maintenance_tick()
    assert not h.killswitch.halted


def test_stale_bankroll_breach_is_enforced_and_not_halt_class() -> None:
    # Unit-level twin of the wiring test: risk_bankroll_cc=None yields exactly one
    # ENFORCED SKIP_BANKROLL_UNAVAILABLE breach (shadow=False under the flipped
    # default), and that reason is NOT in the halt-class set the maintenance loop
    # escalates — so a stale poll fails closed to a no-quote, never a KILL.
    limits = LimitChecker(RiskLimits())  # ENFORCED default
    breaches = limits.check(
        ExposureBook(CONVENTIONS), MARG, DailyPnl(), risk_bankroll_cc=None
    )
    fc = [b for b in breaches if b.reason is ReasonCode.SKIP_BANKROLL_UNAVAILABLE]
    assert len(fc) == 1
    assert fc[0].shadow is False  # enforced under the flipped default
    assert ReasonCode.SKIP_BANKROLL_UNAVAILABLE not in (
        ReasonCode.HALT_DAILY_LOSS,
        ReasonCode.HALT_DRAWDOWN,
        ReasonCode.HALT_HARD_TRIP,
    )
