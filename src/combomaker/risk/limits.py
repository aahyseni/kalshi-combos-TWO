"""Risk limits: all config, all enforced pre-quote AND pre-confirm.

``check`` returns EVERY breach, not the first — breach patterns are tuning
data. The mass-acceptance worst case is part of the standard check: if the
book-plus-all-open-quotes portfolio would breach, we stop issuing quotes even
though nothing has filled yet. Unknown marginals anywhere in the decomposition
count as a breach (UNKNOWN is never safe).

R2 CAP HIERARCHY + SLATE CAP (Phase 2 — SHADOW by default).
The existing hard-dollar caps above KEEP their enforced behaviour. Phase 2 ADDS
a %-of-bankroll cap layer that runs in PARALLEL: each cap derives its threshold
AT CHECK TIME from the live risk bankroll (BalanceTracker.risk_bankroll_cc):

    thr_cc = frac.numerator * bankroll_cc // frac.denominator   (integer-exact)

so caps track the bankroll without ever touching a binary float for money. When
``caps_shadow_mode`` is True (the Phase 2 default) every new-layer breach is
emitted with ``Breach.shadow=True`` — the consumer LOGS it but MUST NOT let it
block a quote/confirm or trigger a halt. Only ``shadow=False`` breaches affect
behaviour. The operator flips ``caps_shadow_mode`` to False to enforce, after
comparing would-be breaches vs current behaviour on real tape.

Two money axes, NEVER summed (R1/R2 invariant #2). Every new %-cap binds on the
LOSS axis (premium at risk: ``max_loss_cc`` / ``worst_case_loss_by_game_cc``)
EXCEPT the absolute-notional utilization backstop, the ONLY new cap on the
gross-settlement-notional axis. The backstop is a loose multiple of bankroll
(``multiple × bankroll``), a ceiling ABOVE the % caps on capital utilization.

Fail-closed (hard rule 6): when the live bankroll is unavailable (stale balance
⇒ caller passes ``risk_bankroll_cc=None``) OR non-positive, NO %-cap can be
computed, so we emit a single ``SKIP_BANKROLL_UNAVAILABLE`` — in shadow mode
log-only, enforced later a real block that stops new quoting entirely (a
stricter backstop than any loose multiple: nothing runs away while the poll is
dark). UNKNOWN bankroll is never a convenient default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from zoneinfo import ZoneInfo

from combomaker.core.money import CC_PER_DOLLAR
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import (
    ExposureBook,
    MarginalProvider,
    OpenPosition,
)

# Slate bucketing timezone. A "slate" = all unresolved games whose start falls on
# the SAME US/Eastern CALENDAR DAY (deterministic; groups an evening's slate and
# avoids the boundary ambiguity of a rolling 2-3h window). TUNABLE: swap for a
# rolling window if the desk prefers, but the ET-day key is the simplest thing
# that captures "one evening's games settle together" — the hole the slate cap
# closes (a daily-loss halt only fires AFTER losses, and many games settle in one
# window). See the Phase 2 report + RISK_BUILD_PLAN Phase 2.
_SLATE_TZ = ZoneInfo("America/New_York")

# The pooled bucket for games whose start time is UNKNOWN. Fail-closed (hard rule
# 6 / quiet-failure defense #2): an unknown-start game is NOT dropped from the
# slate check (which would let unknown-start concentration hide) — it pools into
# ONE conservative bucket that is itself capped, so unknown-start games hit the
# slate cap together.
UNKNOWN_SLATE_KEY = "UNKNOWN"

StartTimeProvider = Callable[[str], datetime | None]
"""market_ticker -> that leg's game start (tz-aware), or None when UNKNOWN.
Wired to ``PregameGate.leg_start_time`` in the app."""


def threshold_cc(frac: Fraction, bankroll_cc: int) -> int:
    """A %-of-bankroll threshold in integer centi-cents, EXACT (no float money).

    ``frac.numerator * bankroll_cc // frac.denominator`` — the established
    integer pattern (BalanceTracker's haircut, FeeModel's coefficients). Floats
    are banned for money/thresholds; a Fraction percentage keeps it exact.
    """
    return frac.numerator * bankroll_cc // frac.denominator


def slate_key_for_start(start: datetime | None) -> str:
    """Slate bucket for a game start: its US/Eastern calendar date, or the pooled
    UNKNOWN bucket when the start is unknown (fail-closed)."""
    if start is None:
        return UNKNOWN_SLATE_KEY
    return start.astimezone(_SLATE_TZ).date().isoformat()


@dataclass(frozen=True, slots=True)
class RiskLimits:
    # --- existing ENFORCED hard-dollar caps (unchanged behaviour) ---
    max_contracts_per_quote: float = 100.0
    max_notional_per_quote_dollars: float = 500.0
    max_market_delta_contracts: float = 300.0
    max_event_delta_contracts: float = 500.0
    max_gross_notional_dollars: float = 5_000.0
    max_open_quotes: int = 20
    max_daily_loss_dollars: float = 500.0
    max_event_worst_case_loss_dollars: float = 1_000.0

    # --- R2 %-of-bankroll cap layer (Phase 2). Percentages are exact Fractions;
    # thresholds are computed at check time from the live risk bankroll. Defaults
    # are the researched $2,000 START values (docs/research/CAP_recommendation_
    # 2000.md); the axis each binds on is documented at its check site. ---
    caps_shadow_mode: bool = True  # Phase 2 default: new caps are LOG-ONLY.
    # %-of-GAME correlated LOSS, on worst_case_loss_by_game_cc (LOSS axis). 8%.
    game_loss_frac: Fraction = Fraction(8, 100)
    # Per-COMBO max LOSS, on a single candidate position's max_loss_cc (LOSS axis
    # — NOT the $1 notional). 1%.
    per_combo_loss_frac: Fraction = Fraction(1, 100)
    # One-directional / theme: net directional exposure to one leg outcome across
    # games (LOSS-equivalent; see the check site for the interpretation). 10%.
    directional_frac: Fraction = Fraction(10, 100)
    # SLATE / time-window pre-trade cap: Σ worst_case_loss_by_game over all games
    # in ONE slate (LOSS axis). Start = same as the game cap. 8%.
    slate_loss_frac: Fraction = Fraction(8, 100)
    # Soft daily-loss halt (realized+unrealized from day start). 6%. Distinct
    # from the enforced hard-dollar max_daily_loss_dollars above.
    daily_loss_frac: Fraction = Fraction(6, 100)
    # Peak-drawdown halt: give-back from intraday peak equity. 10%.
    drawdown_frac: Fraction = Fraction(10, 100)
    # Hard-trip KILL: deeper give-back → human-only clear. 12%.
    hard_trip_frac: Fraction = Fraction(12, 100)
    # Absolute-$ utilization backstop: gross_settlement_notional (utilization
    # axis), whole book, as a MULTIPLE of bankroll. Loose backstop ABOVE the %
    # caps; binds even when the bankroll poll is stale. 3×.
    absolute_notional_multiple: int = 3
    # Fill-velocity (committed notional per rolling window). Operator-set rate
    # (not tape-derivable); soft 5%/2s, hard 10%/2s, plus a fills-count cap.
    fill_velocity_window_s: float = 2.0
    fill_velocity_soft_frac: Fraction = Fraction(5, 100)
    fill_velocity_hard_frac: Fraction = Fraction(10, 100)
    fill_velocity_max_fills: int = 8


@dataclass(frozen=True, slots=True)
class Breach:
    reason: ReasonCode
    detail: str
    # SHADOW breaches are LOG-ONLY: the consumer records them but MUST NOT let
    # them block a quote/confirm or trigger a halt. Only shadow=False breaches
    # affect behaviour. The R2 %-cap layer sets this from caps_shadow_mode.
    shadow: bool = False


@dataclass(frozen=True, slots=True)
class DailyPnl:
    realized_cc: int = 0
    unrealized_cc: int = 0

    @property
    def total_cc(self) -> int:
        return self.realized_cc + self.unrealized_cc


class StarvationWatchdog:
    """Watches for a mis-set cap (or a stuck/zero bankroll) silently declining
    everything: N CONSECUTIVE risk-driven declines with zero successful quotes
    in between → a structured WARNING and a ``starved`` flag the ops loop reads.

    In shadow mode it observes the SHADOW decisions (a would-be decline the new
    caps produced) so a mis-set new cap is caught BEFORE it is enforced — still
    no enforcement of its own. Deterministic + clock-free (a pure counter): a
    risk decline increments, any successful quote resets to zero.
    """

    def __init__(self, *, threshold: int) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._threshold = threshold
        self._consecutive = 0
        self._warned = False

    def record_risk_decline(self) -> bool:
        """A quote was declined for a risk reason (real OR shadow). Returns True
        exactly on the transition into the starved state (so the caller logs the
        warning once per starvation episode, not every decline)."""
        self._consecutive += 1
        if self._consecutive >= self._threshold and not self._warned:
            self._warned = True
            return True
        return False

    def record_quote_issued(self) -> None:
        """A quote was successfully issued — the book is not starved. Resets."""
        self._consecutive = 0
        self._warned = False

    @property
    def consecutive_declines(self) -> int:
        return self._consecutive

    @property
    def starved(self) -> bool:
        """True once ``threshold`` consecutive risk declines have occurred with no
        successful quote in between (a flag the ops loop can read)."""
        return self._warned


@dataclass(slots=True)
class HaltInputs:
    """Give-back inputs for the drawdown / hard-trip halts. Provided by the
    caller from the BalanceTracker when a fresh reading is available; ALL fields
    optional so the caps degrade gracefully (a missing input simply skips that
    halt's evaluation — the halt cannot be computed without a peak, and inventing
    a give-back would be a convenient default).

    ``peak_equity_cc`` = highest exchange equity seen intraday;
    ``current_equity_cc`` = current exchange equity. Give-back = peak − current.
    """

    peak_equity_cc: int | None = None
    current_equity_cc: int | None = None


class LimitChecker:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def check(
        self,
        book: ExposureBook,
        marginals: MarginalProvider,
        daily_pnl: DailyPnl,
        *,
        candidate_positions: list[OpenPosition] | None = None,
        adding_quote: bool = False,
        risk_bankroll_cc: int | None = None,
        start_time_provider: StartTimeProvider | None = None,
        halt_inputs: HaltInputs | None = None,
    ) -> list[Breach]:
        """All current breaches, mass-acceptance included.

        ``candidate_positions``: hypothetical fills being contemplated (last
        look passes the accepted side here). ``adding_quote``: pre-quote check
        counts one more open quote.

        R2 layer (Phase 2): ``risk_bankroll_cc`` is the live risk-capital
        denominator in cc (BalanceTracker.risk_bankroll_cc), or None when stale
        (caller catches StaleBalanceError). ``start_time_provider`` maps a leg's
        market ticker to its game start for the slate bucket. ``halt_inputs``
        carries the intraday peak/current equity for the give-back halts. All R2
        breaches carry ``shadow=caps_shadow_mode`` (default True = log-only).
        """
        limits = self._limits
        breaches: list[Breach] = []
        candidates = candidate_positions or []

        for position in candidates:
            contracts = int(position.contracts) / 100
            if contracts > limits.max_contracts_per_quote:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_SIZE_ABOVE_MAX,
                        f"candidate {contracts:.2f} contracts > "
                        f"{limits.max_contracts_per_quote}",
                    )
                )
            # LOSS axis (premium at risk = what we PAY to open), NOT the $1
            # settlement notional. Named *_loss_* per R2 invariant #2.
            candidate_loss_dollars = position.max_loss_cc / 10_000
            if candidate_loss_dollars > limits.max_notional_per_quote_dollars:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_SIZE_ABOVE_MAX,
                        f"candidate loss ${candidate_loss_dollars:.2f} > "
                        f"${limits.max_notional_per_quote_dollars}",
                    )
                )

        open_quotes = book.snapshot(marginals, mass_acceptance=False).open_quote_count
        if adding_quote and open_quotes + 1 > limits.max_open_quotes:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_MAX_OPEN_QUOTES,
                    f"{open_quotes} open quotes at cap {limits.max_open_quotes}",
                )
            )

        snapshot = book.snapshot(
            marginals, mass_acceptance=True, extra_positions=candidates
        )
        if snapshot.unknown_marginals:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_CLASSIFIER_UNKNOWN,
                    "exposure decomposition has unknown marginals",
                )
            )
        for ticker, delta in snapshot.delta_by_market.items():
            if abs(delta) > limits.max_market_delta_contracts:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"market {ticker} delta {delta:.1f} > "
                        f"{limits.max_market_delta_contracts}",
                    )
                )
        for game, delta in snapshot.delta_by_game.items():
            if abs(delta) > limits.max_event_delta_contracts:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"game {game} delta {delta:.1f} > "
                        f"{limits.max_event_delta_contracts}",
                    )
                )
        if snapshot.gross_notional_cc / 10_000 > limits.max_gross_notional_dollars:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                    f"gross notional ${snapshot.gross_notional_cc / 10_000:.2f} > "
                    f"${limits.max_gross_notional_dollars}",
                )
            )
        # Loss axis (premium at risk), per GAME cluster — the ENFORCED hard-dollar
        # event-worst-case cap. The R2 %-of-GAME cap (below) binds on the SAME
        # game-keyed loss aggregate but scales from the live bankroll. NEITHER
        # ever binds on gross_settlement_notional_by_game_cc (utilization axis) —
        # R1/R2 correctness invariant #2.
        for game, loss_cc in snapshot.worst_case_loss_by_game_cc.items():
            if loss_cc / 10_000 > limits.max_event_worst_case_loss_dollars:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"game {game} worst-case loss ${loss_cc / 10_000:.2f} > "
                        f"${limits.max_event_worst_case_loss_dollars}",
                    )
                )

        if -daily_pnl.total_cc / 10_000 >= limits.max_daily_loss_dollars:
            breaches.append(
                Breach(
                    ReasonCode.HALT_DAILY_LOSS,
                    f"daily P&L ${daily_pnl.total_cc / 10_000:.2f} at loss limit "
                    f"${limits.max_daily_loss_dollars}",
                )
            )

        # --- R2 %-of-bankroll cap layer (Phase 2; shadow by default) ----------
        breaches.extend(
            self._r2_breaches(
                book,
                snapshot,
                candidates,
                daily_pnl,
                risk_bankroll_cc=risk_bankroll_cc,
                start_time_provider=start_time_provider,
                halt_inputs=halt_inputs,
            )
        )
        return breaches

    # ------------------------------------------------------------------ R2 layer

    def _r2_breaches(
        self,
        book: ExposureBook,
        snapshot: object,
        candidates: list[OpenPosition],
        daily_pnl: DailyPnl,
        *,
        risk_bankroll_cc: int | None,
        start_time_provider: StartTimeProvider | None,
        halt_inputs: HaltInputs | None,
    ) -> list[Breach]:
        """The additive %-of-bankroll caps. Every breach carries
        ``shadow=caps_shadow_mode`` so Phase 2 is log-only. Kept in its own method
        so the enforced-cap logic above is untouched and independently testable."""
        limits = self._limits
        shadow = limits.caps_shadow_mode
        out: list[Breach] = []
        # Narrow the snapshot for the type checker without importing at module
        # scope (avoids a cycle); ExposureSnapshot is the concrete type.
        from combomaker.risk.exposure import ExposureSnapshot

        assert isinstance(snapshot, ExposureSnapshot)

        # Fail-closed FIRST (hard rule 6): a missing (stale ⇒ None) OR non-positive
        # bankroll means the whole risk-capital denominator is UNKNOWN/broken — we
        # CANNOT compute any %-cap, and a zero denominator would collapse every
        # threshold to 0 (a wall of spurious breaches). Emit ONE
        # SKIP_BANKROLL_UNAVAILABLE (shadow in Phase 2) and stop — never invent a
        # bankroll, never a convenient default.
        if risk_bankroll_cc is None:
            out.append(
                Breach(
                    ReasonCode.SKIP_BANKROLL_UNAVAILABLE,
                    "live risk bankroll unavailable (stale balance) — %-of-bankroll "
                    "caps fail closed",
                    shadow=shadow,
                )
            )
            return out
        if risk_bankroll_cc <= 0:
            out.append(
                Breach(
                    ReasonCode.SKIP_BANKROLL_UNAVAILABLE,
                    f"risk bankroll {risk_bankroll_cc}cc <= 0 — %-of-bankroll caps "
                    f"fail closed",
                    shadow=shadow,
                )
            )
            return out

        bankroll = risk_bankroll_cc

        # (1) Absolute-$ utilization backstop — the ONLY new cap on the
        # gross-settlement-notional (utilization) axis. A loose backstop ABOVE the
        # % caps: `multiple × bankroll`. NOTE the stale-poll note: with a fresh
        # bankroll it scales; when the bankroll is STALE the caller passes None and
        # the fail-closed branch above stands in (a stale poll blocks new quoting
        # entirely once enforced, which is a stricter backstop than a loose
        # multiple — so nothing runs away in the dark).
        backstop_cc = limits.absolute_notional_multiple * bankroll
        total_notional_cc = sum(snapshot.gross_settlement_notional_by_game_cc.values())
        if total_notional_cc > backstop_cc:
            out.append(
                Breach(
                    ReasonCode.SKIP_UTILIZATION_BACKSTOP,
                    f"gross settlement notional {total_notional_cc}cc > "
                    f"{limits.absolute_notional_multiple}x bankroll "
                    f"{bankroll}cc = {backstop_cc}cc",
                    shadow=shadow,
                )
            )

        # (2) %-of-GAME correlated LOSS — worst_case_loss_by_game_cc (LOSS axis).
        game_thr = threshold_cc(limits.game_loss_frac, bankroll)
        for game, loss_cc in snapshot.worst_case_loss_by_game_cc.items():
            if loss_cc > game_thr:
                out.append(
                    Breach(
                        ReasonCode.SKIP_GAME_LOSS_CAP,
                        f"game {game} loss {loss_cc}cc > {limits.game_loss_frac} "
                        f"bankroll = {game_thr}cc",
                        shadow=shadow,
                    )
                )

        # (3) Per-COMBO max LOSS — a single candidate position's max_loss_cc
        # (LOSS axis, premium at risk — never the $1 notional). Slices whale RFQs.
        combo_thr = threshold_cc(limits.per_combo_loss_frac, bankroll)
        for position in candidates:
            if position.max_loss_cc > combo_thr:
                out.append(
                    Breach(
                        ReasonCode.SKIP_PER_COMBO_LOSS_CAP,
                        f"combo {position.combo_ticker} loss {position.max_loss_cc}cc > "
                        f"{limits.per_combo_loss_frac} bankroll = {combo_thr}cc",
                        shadow=shadow,
                    )
                )

        # (4) One-directional / theme cap. INTERPRETATION: the largest net
        # directional exposure to a single leg outcome, aggregated per GAME
        # (delta_by_game — the signed contracts-equivalent, worst-side under mass
        # acceptance). |delta| is in CONTRACTS-equivalent; the loss-equivalent
        # ceiling of that directional bet is |delta| × $1 (a full adverse
        # resolution moves the position by $1/contract), so convert to cc via
        # ×CC_PER_DOLLAR and compare against the LOSS-axis threshold. This holds
        # near the game cap (a theme ≈ same-game-correlated on a losing night).
        directional_thr = threshold_cc(limits.directional_frac, bankroll)
        for game, delta in snapshot.delta_by_game.items():
            directional_cc = int(abs(delta) * CC_PER_DOLLAR)
            if directional_cc > directional_thr:
                out.append(
                    Breach(
                        ReasonCode.SKIP_DIRECTIONAL_CAP,
                        f"game {game} directional {directional_cc}cc "
                        f"(|delta| {abs(delta):.1f} ct) > {limits.directional_frac} "
                        f"bankroll = {directional_thr}cc",
                        shadow=shadow,
                    )
                )

        # (5) SLATE cap — Σ worst_case_loss_by_game over all games in ONE slate.
        # Slate key = US/Eastern calendar day of the game's earliest known leg
        # start (start_time_provider); UNKNOWN start ⇒ pooled UNKNOWN bucket
        # (fail-closed, itself capped). Roll the game-keyed loss up per slate.
        slate_thr = threshold_cc(limits.slate_loss_frac, bankroll)
        slate_loss = self._slate_rollup(book, snapshot, candidates, start_time_provider)
        for slate, loss_cc in slate_loss.items():
            if loss_cc > slate_thr:
                out.append(
                    Breach(
                        ReasonCode.SKIP_SLATE_CAP,
                        f"slate {slate} loss {loss_cc}cc > {limits.slate_loss_frac} "
                        f"bankroll = {slate_thr}cc",
                        shadow=shadow,
                    )
                )

        # (6) Soft daily-loss halt (6% of bankroll), on realized+unrealized from
        # day start (LOSS axis). Distinct from the enforced hard-dollar daily cap.
        daily_thr = threshold_cc(limits.daily_loss_frac, bankroll)
        if -daily_pnl.total_cc >= daily_thr:
            out.append(
                Breach(
                    ReasonCode.HALT_DAILY_LOSS,
                    f"daily P&L {daily_pnl.total_cc}cc at {limits.daily_loss_frac} "
                    f"bankroll loss limit = -{daily_thr}cc",
                    shadow=shadow,
                )
            )

        # (7) Give-back halts: drawdown (10%) and hard-trip KILL (12%), on
        # give-back = intraday peak equity − current equity. Only evaluated when
        # the caller supplies both equity readings (no peak ⇒ no give-back to
        # measure — inventing one would be a convenient default).
        if halt_inputs is not None and (
            halt_inputs.peak_equity_cc is not None
            and halt_inputs.current_equity_cc is not None
        ):
            give_back_cc = halt_inputs.peak_equity_cc - halt_inputs.current_equity_cc
            hard_thr = threshold_cc(limits.hard_trip_frac, bankroll)
            draw_thr = threshold_cc(limits.drawdown_frac, bankroll)
            # Hard-trip is the deeper give-back; report it distinctly (KILL, not a
            # soft drawdown). Both can fire — the consumer escalates to the KILL.
            if give_back_cc >= hard_thr:
                out.append(
                    Breach(
                        ReasonCode.HALT_HARD_TRIP,
                        f"give-back {give_back_cc}cc >= {limits.hard_trip_frac} "
                        f"bankroll = {hard_thr}cc (KILL, human-only clear)",
                        shadow=shadow,
                    )
                )
            if give_back_cc >= draw_thr:
                out.append(
                    Breach(
                        ReasonCode.HALT_DRAWDOWN,
                        f"give-back {give_back_cc}cc >= {limits.drawdown_frac} "
                        f"bankroll = {draw_thr}cc",
                        shadow=shadow,
                    )
                )

        return out

    def _slate_rollup(
        self,
        book: ExposureBook,
        snapshot: object,
        candidates: list[OpenPosition],
        start_time_provider: StartTimeProvider | None,
    ) -> dict[str, int]:
        """Sum ``worst_case_loss_by_game_cc`` into per-slate buckets.

        The slate bucket of a game is the US/Eastern calendar day of the EARLIEST
        known leg start among positions touching that game (an earlier start is
        the conservative pick — it can only pool a game into an earlier evening's
        slate, never split it out). A game with no known leg start (no provider,
        or every leg returns None) pools into ``UNKNOWN_SLATE_KEY`` — capped, not
        dropped. Exposure.py stays the source of the game aggregation (it drops
        the per-leg tickers the start lookup needs, so we re-walk the legs here);
        the slate roll-up lives in the checker (no schema change there).
        """
        from combomaker.pricing.grouping import game_key
        from combomaker.risk.exposure import ExposureSnapshot

        assert isinstance(snapshot, ExposureSnapshot)

        # Earliest known start per game, walking the legs of every book position
        # AND every candidate (candidates are the hypothetical fills the snapshot
        # already folded into worst_case_loss_by_game_cc under mass acceptance;
        # open QUOTES are folded into the loss aggregate too, so include their
        # legs to bucket a quote-driven game correctly).
        source_positions: list[OpenPosition] = list(book.positions.values()) + candidates
        leg_sources: list[tuple[str, str | None]] = [
            (leg.market_ticker, leg.event_ticker)
            for position in source_positions
            for leg in position.legs
        ]
        for quote in book.open_quotes.values():
            leg_sources.extend(
                (leg.market_ticker, leg.event_ticker) for leg in quote.legs
            )

        earliest_start: dict[str, datetime | None] = {}
        if start_time_provider is not None:
            for market_ticker, event_ticker in leg_sources:
                if not event_ticker:
                    continue
                game = game_key(event_ticker)
                start = start_time_provider(market_ticker)
                if start is None:
                    earliest_start.setdefault(game, None)
                    continue
                prior = earliest_start.get(game)
                if game not in earliest_start or prior is None or start < prior:
                    earliest_start[game] = start

        slate_loss: dict[str, int] = {}
        for game, loss_cc in snapshot.worst_case_loss_by_game_cc.items():
            start = earliest_start.get(game)  # None or absent ⇒ UNKNOWN bucket
            slate = slate_key_for_start(start)
            slate_loss[slate] = slate_loss.get(slate, 0) + loss_cc
        return slate_loss
