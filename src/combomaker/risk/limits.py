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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Protocol
from zoneinfo import ZoneInfo

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


def scaled_delta_cap_contracts(
    frac: Fraction | None, absolute_contracts: float, bankroll_cc: int | None
) -> tuple[float, str]:
    """The effective directional delta cap in WHOLE contracts + a detail suffix.

    Auto-scaling delta caps (operator directive 2026-07-19). When ``frac`` is
    armed AND a usable bankroll reading exists, the cap derives from the live
    bankroll, integer-exact in cc then converted to contracts at the $1-payout
    convention (1 contract ≈ $1 = 10_000 cc — the same convention the check
    site's ``loss_cc / 10_000`` dollar comparisons use):

        cap_contracts = threshold_cc(frac, bankroll_cc) / 10_000

    The suffix documents the derivation in the breach detail. When ``frac`` is
    None (default) or the bankroll is unavailable/non-positive, the ABSOLUTE
    knob governs with an empty suffix — the pre-existing breach detail string
    byte-identical. (A configured-but-stale bankroll source already blocks new
    quoting via the R2 layer's SKIP_BANKROLL_UNAVAILABLE, so the fallback can
    never let the book run away in the dark.) Recomputed per check from the
    live bankroll — exactly the loss budgets' no-caching pattern.
    """
    if frac is None or bankroll_cc is None or bankroll_cc <= 0:
        return absolute_contracts, ""
    return (
        threshold_cc(frac, bankroll_cc) / 10_000,
        f" ({frac} x bankroll {bankroll_cc}cc)",
    )


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
    # --- AUTO-SCALING DELTA CAPS (operator directive 2026-07-19: "I don't like
    # manually moving stuff like this, it should be automatic"). When set
    # (non-None), the two enforced directional delta caps above DERIVE their
    # CONTRACT threshold at CHECK TIME from the SAME live risk bankroll the
    # %-of-bankroll loss budgets use (``risk_bankroll_cc`` =
    # BalanceTracker.risk_bankroll_cc, passed per check — no caching):
    #     cap_contracts = threshold_cc(frac, bankroll_cc) / 10_000
    # (1 contract ≈ $1 max payout = 10_000 cc, so this is frac ×
    # bankroll-in-dollars in contract units; delta_by_market/delta_by_game are
    # float WHOLE contracts at this site). PRECEDENCE: a set frac WINS and the
    # absolute knob is IGNORED whenever a usable bankroll reading exists; with
    # no usable bankroll (None / <= 0) the absolute knob stands in as the
    # backstop — and when a bankroll SOURCE is configured, the R2 layer's
    # SKIP_BANKROLL_UNAVAILABLE is already blocking new quoting (fail-closed),
    # so the fallback never loosens anything. None (default) = the absolute
    # caps behave exactly as today, byte-identical. The derived-cap breaches
    # keep the delta family's existing shape: SKIP_MASS_ACCEPTANCE_BREACH,
    # shadow=False (always-enforced axis), game=None (never waivable).
    max_market_delta_frac: Fraction | None = None
    max_event_delta_frac: Fraction | None = None
    max_gross_notional_dollars: float = 5_000.0
    max_open_quotes: int = 20
    max_daily_loss_dollars: float = 500.0
    max_event_worst_case_loss_dollars: float = 1_000.0

    # --- R2 %-of-bankroll cap layer (Phase 2). Percentages are exact Fractions;
    # thresholds are computed at check time from the live risk bankroll. Defaults
    # are the researched $2,000 START values (docs/research/CAP_recommendation_
    # 2000.md); the axis each binds on is documented at its check site. ---
    # ENFORCED by default (wire-live 2026-07-13): the R2 caps + give-back KILL now
    # actually block/halt. Flip to True only to re-shadow a new cap for a tape
    # comparison before enforcing it. Fail-closed-without-bricking is preserved by
    # the check sites: a stale bankroll fails the %-caps closed (no-quote via
    # SKIP_BANKROLL_UNAVAILABLE, not a permanent halt), and the give-back halts
    # SKIP when peak/current equity is unavailable (no invented peak), so a fresh
    # demo start with no balance/positions still quotes normally.
    caps_shadow_mode: bool = False
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
    # Portfolio joint-tail cap (Phase 4 / M1 §5): the book's GOVERNING MODEL
    # ES_0.99 (max of production-copula ES at corr-high and challenger ES — the
    # worst SAMPLED CVaR) as a %-of-bankroll ceiling (LOSS axis — ES is a loss
    # magnitude). P0-3: SAMPLED tail ONLY; the deterministic all-hit maximum is a
    # SEPARATE cap below. 15% START: looser than the daily-loss halt because
    # ES_0.99 is a rare-tail figure the book is expected to sit well inside; it
    # bites only when the correlated joint tail (many shared games breaking
    # together) approaches a meaningful slice of bankroll. Read off the latest
    # BookRiskSnapshot (never re-run MC in check); a stale/UNKNOWN snapshot fails
    # closed.
    portfolio_cvar_frac: Fraction = Fraction(15, 100)
    # Portfolio DETERMINISTIC maximum-loss cap (P0-3): the exact all-hit
    # premium-at-risk (+ reserved holdings) as a %-of-bankroll ceiling. Gated
    # INDEPENDENTLY of the sampled-ES cap so the deterministic maximum is its own
    # premium-at-risk backstop rather than folded into (and dominating) the ES
    # axis. Defaults to the same 15% as the CVaR cap: this preserves the exact
    # deterministic enforcement the old operative-ES max provided (the all-hit
    # maximum normally dominated), while the model-ES axis now fires on its own.
    portfolio_det_max_frac: Fraction = Fraction(15, 100)
    # MUTEX/SCENARIO-AWARE det-max gating (operator directive 2026-07-18). The
    # comonotone all-hit number charges MUTUALLY EXCLUSIVE parlays (FRA-wins
    # and ENG-wins of one game; two champion outcomes) as if they could all hit
    # simultaneously — impossible — so the det-max cap taxed diversifying flow.
    # True (default): the portfolio det-max cap gates on the snapshot's
    # ``mutex_aware_det_max_cc`` (within-game exclusive branches max, across
    # games sum, comonotone for every unproven slice; always <= the comonotone
    # number — see sim/book_risk.mutex_aware_det_max_from_units). False: the
    # old comonotone gating, byte-identical. A snapshot that predates the field
    # (None) gates comonotone regardless (fail closed). The threshold and the
    # SKIP_PORTFOLIO_DET_MAX reason are unchanged; both bounds are logged in
    # the breach detail so monitoring can compare.
    portfolio_det_max_mutex_aware: bool = True
    # A2: max acceptable P(this settlement wave drops equity below the ruin floor).
    # Read off the structural-MC snapshot's ``p_ruin`` (floor set on the MC side,
    # -30% ⇒ equity < 0.70·bankroll). A probability budget, not a $ cap.
    portfolio_ruin_prob_budget: Fraction = Fraction(5, 100)
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
    # --- QUOTE-TIME resting-quote haircut (operator design 2026-07-17) ---
    # Weight on every resting (open) quote's contribution to the QUOTE-TIME
    # mass-acceptance folds (game-loss, slate, directional, delta/notional,
    # utilization — every cap reading open quotes at quote time), with a BURST
    # FLOOR: never less than the FULL (100%) contribution of the
    # ``resting_floor_count`` largest resting quotes per axis/bucket. Applied
    # ONLY when a call site passes ``apply_resting_haircut=True`` to ``check``
    # — the quote-time sites (handle_rfq + the F1 pre-gate). CONFIRM-TIME call
    # sites (reservation / last-look) never pass it, so they stay pinned at
    # the 100% fold (the exact enforcement is theirs; regression-tested
    # bit-identical armed vs not). DEFAULT 1.0 = today's behaviour byte-
    # identical; the operator arms 0.40 in the local YAML. See
    # risk/exposure.py's composition note + tools/proto_resting_haircut.py.
    resting_quote_weight: Fraction = Fraction(1)
    resting_floor_count: int = 3


@dataclass(frozen=True, slots=True)
class Breach:
    reason: ReasonCode
    detail: str
    # SHADOW breaches are LOG-ONLY: the consumer records them but MUST NOT let
    # them block a quote/confirm or trigger a halt. Only shadow=False breaches
    # affect behaviour. The R2 %-cap layer sets this from caps_shadow_mode.
    shadow: bool = False
    # The game key (pricing.grouping.game_key) a PER-GAME cap breach is keyed on,
    # or None for every non-per-game cap. Set ONLY by the game-loss and
    # mutex-directional cap sites so the confirm-path last-look MC waiver can
    # identify exactly which games it must certify — never parsed out of the
    # detail string. Purely additive metadata: no consumer branches on it except
    # the waiver.
    game: str | None = None


# --- F1 monotone pre-pricing gate (throughput synthesis 2026-07-16) ---------
# Breach reasons a CANDIDATE-FREE check may pre-decline an RFQ on, BEFORE the
# expensive joint pricing runs: each is provably candidate-MONOTONE ("already
# breached without the candidate ⇒ breached with ANY candidate"), so the gate
# can only ever produce the SAME decline earlier — never skip an RFQ today's
# full pipeline would have quoted. Validated prototype-first (hard rule 8) in
# tools/proto_pre_pricing_gate.py: 5,000-case fuzz against THIS checker (0
# violations) + constructed counterexamples for every exclusion + a live-tape
# replay (48.2% of the window's no-quotes carried an allowlisted reason).
#
# INCLUDED (and why each is monotone):
#   SKIP_MAX_OPEN_QUOTES      pure count with adding_quote=True — candidate-free
#                             and with-candidate checks read the SAME count.
#   SKIP_GAME_LOSS_CAP        the per-game loss fold (_mutex_game_worst_cc) is
#                             monotone in the entry set (E2 dominance): a
#                             candidate only ADDS entries to a game, and the
#                             ME-count fold-switch only moves TOWARD the larger
#                             comonotone sum.
#   SKIP_UTILIZATION_BACKSTOP Σ gross settlement notional — every candidate
#                             adds a non-negative notional.
#   SKIP_BANKROLL_UNAVAILABLE candidate-independent (bankroll reading only).
#
# EXCLUDED (deliberately — each exclusion is load-bearing):
#   SKIP_MASS_ACCEPTANCE_BREACH  spans the DELTA axes, where an opposite-side
#                                candidate can hedge |delta| back UNDER the cap
#                                (proto B1); the loss/notional instances are
#                                monotone but the reason alone cannot tell the
#                                axes apart and details are never parsed.
#   SKIP_SLATE_CAP               a candidate leg with a KNOWN start re-buckets a
#                                game out of the breached slate (proto B2 shows
#                                a full false-skip).
#   SKIP_DIRECTIONAL_CAP         plan-of-record conservatism: the P0-9 fold is
#                                documented monotone, but the lens-3 allowlist
#                                omitted it and its decline volume is marginal.
#   per-combo / per-quote size   candidate-only (a candidate-free check cannot
#                                emit them).
#   CVaR / det-max / ruin        synthesis: never the candidate-EV/CVaR paths.
#   halt-class breaches          escalation belongs to the maintenance tick.
PRE_PRICING_MONOTONE_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.SKIP_MAX_OPEN_QUOTES,
        ReasonCode.SKIP_GAME_LOSS_CAP,
        ReasonCode.SKIP_UTILIZATION_BACKSTOP,
        ReasonCode.SKIP_BANKROLL_UNAVAILABLE,
    }
)


def monotone_pre_quote_breaches(breaches: list[Breach]) -> list[Breach]:
    """Filter a candidate-free ``check`` result down to the breaches the F1
    pre-pricing gate may decline on: ENFORCED (never shadow — the shadow
    guarantee survives even if the caller forgot to partition first) AND on a
    candidate-monotone reason (PRE_PRICING_MONOTONE_REASONS above). Pure;
    parity-pinned against the validated prototype
    (tools/proto_pre_pricing_gate.py part D)."""
    return [
        b
        for b in breaches
        if not b.shadow and b.reason in PRE_PRICING_MONOTONE_REASONS
    ]


class WaiverCertificate(Protocol):
    """CONFIRM-PATH last-look waiver certificate for ONE game (structurally
    ``sim.state_worst_case.GameWorstCase`` — a Protocol so ``limits`` never
    imports ``sim``). ``worst_case_cc`` is the STATE-CONSISTENT worst case over
    the merged confirm-time book (committed + reservations + candidate netting
    fully; open quotes clamped at max(0, loss) per state), computed by EXACT
    enumeration over the Dixon-Coles scoreline grid. ``certified`` False means
    the game had no buildable structural plan (the certificate is void and the
    caps stand). The certificate is honoured ONLY when its worst case fits the
    game-loss budget — validated again at the check site (fail-closed: a bogus
    or stale certificate never skips a cap)."""

    @property
    def worst_case_cc(self) -> int: ...

    @property
    def certified(self) -> bool: ...


def _waiver_covers(
    waived_games: Mapping[str, WaiverCertificate] | None,
    game: str,
    game_thr_cc: int,
) -> bool:
    """Whether a confirm-path waiver certificate covers ``game``: present,
    CERTIFIED, and its state-consistent worst case within the game-loss budget
    (``game_thr_cc`` — the SAME threshold_cc(game_loss_frac, bankroll) budget the
    game-loss cap enforces, never a raised one). Re-validated HERE, at the point
    of enforcement, so a certificate built against a different bankroll can only
    ever be REJECTED by a tighter live budget (fail-closed), never honoured
    against a looser one."""
    if not waived_games:
        return False
    cert = waived_games.get(game)
    return cert is not None and cert.certified and cert.worst_case_cc <= game_thr_cc


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

    ``pending_settlement_credit_cc`` = the sum of settlement RECEIVABLES: gross
    credits for held positions whose outcome is KNOWN from exchange-graded facts
    but whose cash the balance poll has not yet observed (the exchange removes a
    settled position from ``portfolio_value`` before crediting ``balance``, so a
    settlement cascade transiently dips equity by exactly this in-flight value —
    the 2026-07-19 false-positive $430 give-back kill whose real losers were
    $29.51). The give-back halts measure ``max(0, peak − current − pending)``:
    receivables only ever REDUCE the measured give-back — they never inflate
    equity or the peak — and a LOSING position produces no receivable, so a real
    loss cascade is never shielded. Default 0 ⇒ every existing caller keeps the
    exact raw measurement.
    """

    peak_equity_cc: int | None = None
    current_equity_cc: int | None = None
    pending_settlement_credit_cc: int = 0


class PortfolioRisk(Protocol):
    """The subset of a ``sim.book_risk.BookRiskSnapshot`` the CVaR cap reads.

    Structural (a Protocol) so ``limits`` never imports ``sim.book_risk`` (which
    imports ``risk.exposure`` — a cycle). The caller passes the LATEST full-MC
    snapshot; ``check`` never re-runs MC (kept cheap + pure). ``usable`` False (an
    UNKNOWN/empty snapshot) ⇒ the CVaR cap fails closed."""

    @property
    def usable(self) -> bool: ...

    @property
    def governing_model_es_99_cc(self) -> float: ...

    @property
    def deterministic_max_loss_cc(self) -> float: ...

    @property
    def p_ruin(self) -> float: ...

    # P1-2: the one-sided Wilson upper confidence bound on ``p_ruin`` the ruin cap
    # gates on (== p_ruin at the default confidence z of 0). Read via ``getattr``
    # with a ``p_ruin`` fallback in ``check`` so a snapshot predating this field
    # degrades to the point estimate (never looser) instead of raising.
    @property
    def p_ruin_upper(self) -> float: ...

    # 2026-07-18: the MUTEX/SCENARIO-AWARE deterministic maximum (always <= the
    # comonotone ``deterministic_max_loss_cc``; None when uncomputed). Read via
    # ``getattr`` with a None fallback in ``check`` so a snapshot/fake predating
    # this field degrades to the comonotone number (fail closed, never looser).
    @property
    def mutex_aware_det_max_cc(self) -> float | None: ...


class LimitChecker:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> RiskLimits:
        """The immutable ``RiskLimits`` this checker enforces (read-only). Lets
        callers read the fill-velocity knobs / cap fractions without duplicating
        the config, so the fill-velocity governor derives its window + thresholds
        from the SAME limits the caps use."""
        return self._limits

    def set_limits(self, limits: RiskLimits) -> None:
        """Atomically swap the enforced ``RiskLimits``. The correlation-adaptive
        cap engine (`risk/derived_cap_engine.py`) calls this at the nightly
        refresh so the deploy/halt caps track measured vol + correlation instead
        of a static config. ``check`` reads ``self._limits`` per call, so the
        swap takes effect on the next check — a single reference assignment,
        atomic within the single-threaded event loop. The caller logs the diff."""
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
        bankroll_source_configured: bool = True,
        start_time_provider: StartTimeProvider | None = None,
        halt_inputs: HaltInputs | None = None,
        book_risk: PortfolioRisk | None = None,
        waived_games: Mapping[str, WaiverCertificate] | None = None,
        apply_resting_haircut: bool = False,
    ) -> list[Breach]:
        """All current breaches, mass-acceptance included.

        ``candidate_positions``: hypothetical fills being contemplated (last
        look passes the accepted side here). ``adding_quote``: pre-quote check
        counts one more open quote.

        ``apply_resting_haircut`` (operator design 2026-07-17; the quote-time
        sites, plus — since the same-day confirm extension — the reservation
        check when ``risk.resting_haircut_at_confirm`` is armed): True ⇒ the
        exposure snapshot weights every RESTING open
        quote's mass-acceptance contribution at ``limits.resting_quote_weight``
        (burst-floored at the full contribution of the
        ``limits.resting_floor_count`` largest; committed positions and
        candidates are never haircut). Passed True by exactly two call sites —
        ``handle_rfq``'s post-pricing check and the F1 pre-pricing gate (which
        must share the semantics for the pre-gate lemma). CONFIRM-TIME callers
        (reservation / last-look / maintenance) leave the default False and are
        thereby PINNED at the 100% fold — they cannot pick the weight up even
        by accident, and a regression test proves their decisions are
        bit-identical with the haircut armed vs not. With the default weight
        of 1 the flag is a no-op (today's behaviour byte-identical).

        ``waived_games`` (CONFIRM-PATH ONLY — the last-look MC waiver): per-game
        state-consistent worst-case certificates. For EXACTLY those games, and
        ONLY when the certificate is certified and its worst case fits the
        game-loss budget (re-validated here), the %-of-GAME loss cap and the
        mutex-directional cap are SKIPPED — every other cap is unchanged.
        QUOTE-TIME callers must pass nothing (the default None is byte-identical
        prior behaviour): the quote-time analytic bounds must stay MONOTONE (E2
        mass-acceptance dominance) and the state-consistent bound is not.

        R2 layer (Phase 2): ``risk_bankroll_cc`` is the live risk-capital
        denominator in cc (BalanceTracker.risk_bankroll_cc), or None when stale
        (caller catches StaleBalanceError). ``start_time_provider`` maps a leg's
        market ticker to its game start for the slate bucket. ``halt_inputs``
        carries the intraday peak/current equity for the give-back halts. All R2
        breaches carry ``shadow=caps_shadow_mode``.

        ``bankroll_source_configured`` distinguishes two None-bankroll cases the
        %-cap denominator cannot tell apart (fail-closed-without-bricking):
          - True (default) + None ⇒ a bankroll SOURCE exists but its reading is
            STALE/absent ⇒ the %-caps FAIL CLOSED (SKIP_BANKROLL_UNAVAILABLE),
            the dark-poll runaway defense (hard rule 6).
          - False + None ⇒ NO bankroll source is wired at all (this deployment
            didn't opt into %-of-bankroll caps) ⇒ the R2 %-cap layer is simply
            INACTIVE (no breach), so a fresh demo/paper start with no balance
            tracker still quotes normally off the enforced hard-dollar caps. This
            is NOT inventing a bankroll — it is not running the layer whose
            denominator is structurally absent.
        A present ``risk_bankroll_cc`` ignores this flag (the caps compute).

        Phase 4: ``book_risk`` is the LATEST full-MC ``BookRiskSnapshot`` (built
        off the hot path); the portfolio-CVaR cap reads its operative ES here
        WITHOUT re-running MC (keeps ``check`` cheap). None ⇒ the CVaR cap is
        simply not evaluated (no snapshot yet); a present-but-unusable snapshot
        fails closed (a breach), matching UNKNOWN-is-never-safe.
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

        # F5 (throughput synthesis 2026-07-16): a DIRECT count. The old
        # ``book.snapshot(marginals, mass_acceptance=False).open_quote_count``
        # built an entire O(positions × legs) exposure decomposition and threw
        # everything away except this len() — one of three full decompositions
        # per admitted RFQ on the single loop thread. ``ExposureSnapshot.
        # open_quote_count`` is ``len(self.open_quotes)`` verbatim (exposure.py
        # snapshot()), so this is value-identical on every book, by construction
        # and by test (test_limits.py::TestOpenQuoteCountDirect).
        open_quotes = len(book.open_quotes)
        if adding_quote and open_quotes + 1 > limits.max_open_quotes:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_MAX_OPEN_QUOTES,
                    f"{open_quotes} open quotes at cap {limits.max_open_quotes}",
                )
            )

        snapshot = book.snapshot(
            marginals,
            mass_acceptance=True,
            extra_positions=candidates,
            # QUOTE-TIME resting haircut: armed sites weight the resting fold;
            # None ⇒ the pre-existing 100% fold, byte-identical (confirm path).
            resting_quote_weight=(
                limits.resting_quote_weight if apply_resting_haircut else None
            ),
            resting_floor_count=limits.resting_floor_count,
        )
        if snapshot.unknown_marginals:
            breaches.append(
                Breach(
                    ReasonCode.SKIP_CLASSIFIER_UNKNOWN,
                    "exposure decomposition has unknown marginals",
                )
            )
        # AUTO-SCALING DELTA CAPS (2026-07-19): each cap's contract threshold
        # is derived PER CHECK from the live bankroll when its frac is armed
        # (frac wins, absolute ignored); frac unset ⇒ the absolute knob,
        # byte-identical detail included. Deltas here are float WHOLE
        # contracts; the derived cap is frac × bankroll-in-dollars at the
        # $1-payout convention (scaled_delta_cap_contracts). Breach shape is
        # unchanged: SKIP_MASS_ACCEPTANCE_BREACH, shadow=False, game=None —
        # the delta family stays non-waivable at the lifecycle's game-key
        # check regardless of which mode derived the threshold.
        market_delta_cap, market_delta_note = scaled_delta_cap_contracts(
            limits.max_market_delta_frac,
            limits.max_market_delta_contracts,
            risk_bankroll_cc,
        )
        event_delta_cap, event_delta_note = scaled_delta_cap_contracts(
            limits.max_event_delta_frac,
            limits.max_event_delta_contracts,
            risk_bankroll_cc,
        )
        for ticker, delta in snapshot.delta_by_market.items():
            if abs(delta) > market_delta_cap:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"market {ticker} delta {delta:.1f} > "
                        f"{market_delta_cap}{market_delta_note}",
                    )
                )
        for game, delta in snapshot.delta_by_game.items():
            if abs(delta) > event_delta_cap:
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"game {game} delta {delta:.1f} > "
                        f"{event_delta_cap}{event_delta_note}",
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
                # WAIVER COVERAGE (2026-07-17): this hard-dollar cap binds on
                # the SAME game-keyed loss aggregate as the %-of-bankroll
                # game-loss cap below — a state-exact certificate within THIS
                # cap's own budget covers it identically (in practice the
                # waiver validates at the STRICTER frac budget too). The
                # breach carries its game key so the waiver can certify it;
                # pre-fix it was emitted game-less under a non-waivable code
                # and disarmed the waiver on every 200-slot confirm.
                hard_cc = int(limits.max_event_worst_case_loss_dollars * 10_000)
                if _waiver_covers(waived_games, game, hard_cc):
                    continue
                breaches.append(
                    Breach(
                        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
                        f"game {game} worst-case loss ${loss_cc / 10_000:.2f} > "
                        f"${limits.max_event_worst_case_loss_dollars}",
                        game=game,
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

        # --- R2 %-of-bankroll cap layer (Phase 2; ENFORCED by default) --------
        breaches.extend(
            self._r2_breaches(
                book,
                snapshot,
                candidates,
                daily_pnl,
                risk_bankroll_cc=risk_bankroll_cc,
                bankroll_source_configured=bankroll_source_configured,
                start_time_provider=start_time_provider,
                halt_inputs=halt_inputs,
                book_risk=book_risk,
                waived_games=waived_games,
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
        bankroll_source_configured: bool = True,
        start_time_provider: StartTimeProvider | None,
        halt_inputs: HaltInputs | None,
        book_risk: PortfolioRisk | None = None,
        waived_games: Mapping[str, WaiverCertificate] | None = None,
    ) -> list[Breach]:
        """The additive %-of-bankroll caps. Every breach carries
        ``shadow=caps_shadow_mode``. Kept in its own method so the enforced-cap
        logic above is untouched and independently testable. ``waived_games`` is
        the confirm-path waiver pass-through (see ``check``); it touches ONLY
        the game-loss and mutex-directional cap sites below."""
        limits = self._limits
        shadow = limits.caps_shadow_mode
        out: list[Breach] = []
        # Narrow the snapshot for the type checker without importing at module
        # scope (avoids a cycle); ExposureSnapshot is the concrete type.
        from combomaker.risk.exposure import ExposureSnapshot

        assert isinstance(snapshot, ExposureSnapshot)

        # NO bankroll source wired at all (bankroll_source_configured False) and no
        # reading ⇒ this deployment did not opt into %-of-bankroll caps, so the
        # whole R2 %-cap + give-back layer is INACTIVE (no breach) — the enforced
        # hard-dollar caps still bind above. This is the do-not-brick path: a fresh
        # demo/paper start with no balance tracker still quotes normally. It is NOT
        # a convenient default (no bankroll is invented); the layer whose
        # denominator is structurally absent simply does not run.
        if risk_bankroll_cc is None and not bankroll_source_configured:
            return out

        # Fail-closed FIRST (hard rule 6): a bankroll SOURCE is configured but its
        # reading is missing (stale ⇒ None) OR non-positive — the risk-capital
        # denominator is UNKNOWN/broken, so we CANNOT compute any %-cap (a zero
        # denominator would collapse every threshold to 0, a wall of spurious
        # breaches). Emit ONE SKIP_BANKROLL_UNAVAILABLE (enforced ⇒ a no-quote, the
        # dark-poll runaway defense) and stop — never invent a bankroll, never a
        # convenient default.
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
        # CONFIRM-PATH waiver: a game whose certificate proves the state-
        # consistent worst case fits THIS SAME budget skips the (deliberately
        # comonotone-overstated) analytic bound — quote-time callers pass no
        # waivers, so their behaviour is byte-identical.
        game_thr = threshold_cc(limits.game_loss_frac, bankroll)
        for game, loss_cc in snapshot.worst_case_loss_by_game_cc.items():
            if loss_cc > game_thr:
                if _waiver_covers(waived_games, game, game_thr):
                    continue
                out.append(
                    Breach(
                        ReasonCode.SKIP_GAME_LOSS_CAP,
                        f"game {game} loss {loss_cc}cc > {limits.game_loss_frac} "
                        f"bankroll = {game_thr}cc",
                        shadow=shadow,
                        game=game,
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

        # (4) One-directional / theme cap (P0-9: mutex-aware hedge semantics).
        # INTERPRETATION: the net directional exposure to a game's single RESULT
        # outcome, aggregated per GAME, in LOSS-equivalent cc. Binds on
        # ``directional_by_game_cc`` — the MUTUAL-EXCLUSION-AWARE directional bound
        # (worst-side under mass acceptance) — NOT the raw ``delta_by_game`` sum of
        # independence proxies. Opposing-advance long-NO positions (short two
        # mutually-exclusive outcomes) NET here, so a genuine same-game HEDGE gets
        # justified credit instead of tripping skip_directional_cap; concentration
        # on ONE outcome still sums and still trips. The bound is a MONOTONIC HARD
        # directional/model-sensitivity backstop (>= the largest single directional
        # entry, <= the summed magnitude; adding a quote never lowers it — so the
        # all-accepted mass snapshot dominates every accepted subset). It is NOT a
        # raised limit: the same directional_frac threshold applies. The loose
        # summed-magnitude ``delta_by_game`` bound stays the HARD backstop the
        # enforced max_event_delta mass-acceptance cap binds on (limits above);
        # richer all-legs hedge credit lives in the candidate-aware MC (P0-1).
        # CONFIRM-PATH waiver: the SAME per-game certificate (state-consistent
        # worst case within the GAME-LOSS budget — never a raised one) also skips
        # this game's directional bound: the certificate's exact enumeration IS
        # the true loss bound the directional proxy overstates. Quote-time
        # callers pass no waivers — behaviour byte-identical.
        directional_thr = threshold_cc(limits.directional_frac, bankroll)
        for game, directional_cc in snapshot.directional_by_game_cc.items():
            if directional_cc > directional_thr:
                if _waiver_covers(waived_games, game, game_thr):
                    continue
                out.append(
                    Breach(
                        ReasonCode.SKIP_DIRECTIONAL_CAP,
                        f"game {game} mutex-aware directional {directional_cc}cc > "
                        f"{limits.directional_frac} bankroll = {directional_thr}cc",
                        shadow=shadow,
                        game=game,
                    )
                )

        # (5) SLATE cap — Σ worst_case_loss_by_game over all games in ONE slate.
        # Slate key = US/Eastern calendar day of the game's earliest known leg
        # start (start_time_provider); UNKNOWN start ⇒ pooled UNKNOWN bucket
        # (fail-closed, itself capped). Roll the game-keyed loss up per slate.
        slate_thr = threshold_cc(limits.slate_loss_frac, bankroll)
        # CERTIFICATE-AWARE SLATE (2026-07-17, the waiver doctrine extended to
        # the SUM): where a game carries a VALID waiver certificate (certified
        # AND within the per-game budget — the same _waiver_covers validation
        # the per-game caps apply), the certificate's state-exact worst case
        # REPLACES that game's comonotone analytic term in the slate roll-up
        # (min() — a certificate can only tighten, never raise). Uncertified
        # games keep the analytic term (fail-closed). Without this, the slate
        # cap re-summed the very overstatement the per-game waiver just
        # disproved and re-blocked every waiver-granted fill on a multi-game
        # slate (slate 0.40 < 2 x game 0.30 is arithmetically unreachable).
        certified_game_loss: dict[str, int] = {}
        if waived_games:
            for g, loss_v in snapshot.worst_case_loss_by_game_cc.items():
                if _waiver_covers(waived_games, g, game_thr):
                    cert = waived_games[g]
                    certified_game_loss[g] = min(int(loss_v), int(cert.worst_case_cc))
        slate_loss = self._slate_rollup(
            book, snapshot, candidates, start_time_provider,
            certified_game_loss=certified_game_loss,
        )
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
            # Settlement-cascade shield: pending receivables (KNOWN-outcome
            # credits the balance poll has not yet observed) reduce the measured
            # give-back, floored at 0 — see HaltInputs. Raw peak/current stay
            # untouched, so the shield can never inflate a peak; losers carry no
            # receivable, so a genuine loss cascade still measures in full.
            raw_give_back_cc = (
                halt_inputs.peak_equity_cc - halt_inputs.current_equity_cc
            )
            pending_cc = halt_inputs.pending_settlement_credit_cc
            give_back_cc = max(0, raw_give_back_cc - pending_cc)
            pending_note = (
                f" (raw {raw_give_back_cc}cc − receivables {pending_cc}cc)"
                if pending_cc > 0
                else ""
            )
            hard_thr = threshold_cc(limits.hard_trip_frac, bankroll)
            draw_thr = threshold_cc(limits.drawdown_frac, bankroll)
            # Hard-trip is the deeper give-back; report it distinctly (KILL, not a
            # soft drawdown). Both can fire — the consumer escalates to the KILL.
            if give_back_cc >= hard_thr:
                out.append(
                    Breach(
                        ReasonCode.HALT_HARD_TRIP,
                        f"give-back {give_back_cc}cc{pending_note} >= "
                        f"{limits.hard_trip_frac} bankroll = {hard_thr}cc "
                        f"(KILL, human-only clear)",
                        shadow=shadow,
                    )
                )
            if give_back_cc >= draw_thr:
                out.append(
                    Breach(
                        ReasonCode.HALT_DRAWDOWN,
                        f"give-back {give_back_cc}cc{pending_note} >= "
                        f"{limits.drawdown_frac} bankroll = {draw_thr}cc",
                        shadow=shadow,
                    )
                )

        # (8) Portfolio joint-tail cap (Phase 4 / M1 §5): the book's GOVERNING
        # MODEL ES_0.99 (max of copula-high ES and challenger ES — the worst
        # SAMPLED CVaR), read off the latest full-MC snapshot, vs a %-of-bankroll
        # ceiling. This is the joint-tail backstop the analytic per-game worst
        # case cannot see (the analytic sums worst cases as if independent; this
        # counts the correlated joint tail — many shared games breaking together).
        # P0-3: the SAMPLED ES and the DETERMINISTIC all-hit maximum are gated as
        # INDEPENDENT axes below — the deterministic maximum no longer dominates
        # (and silences) the sampled ES. A present-but-unusable snapshot (UNKNOWN
        # marginal / empty) fails BOTH closed.
        if book_risk is not None:
            cvar_thr = threshold_cc(limits.portfolio_cvar_frac, bankroll)
            det_max_thr = threshold_cc(limits.portfolio_det_max_frac, bankroll)
            if not book_risk.usable:
                # Fail closed on BOTH tail axes — an unmeasured joint tail AND an
                # unmeasured deterministic maximum are each never safe.
                out.append(
                    Breach(
                        ReasonCode.SKIP_PORTFOLIO_CVAR,
                        "portfolio book-risk snapshot unusable (UNKNOWN marginal / "
                        "empty) — joint-tail cap fails closed",
                        shadow=shadow,
                    )
                )
                out.append(
                    Breach(
                        ReasonCode.SKIP_PORTFOLIO_DET_MAX,
                        "portfolio book-risk snapshot unusable (UNKNOWN marginal / "
                        "empty) — deterministic max-loss cap fails closed",
                        shadow=shadow,
                    )
                )
            else:
                # (8a) SAMPLED model-ES axis — fires on the correlated joint tail.
                if book_risk.governing_model_es_99_cc > cvar_thr:
                    out.append(
                        Breach(
                            ReasonCode.SKIP_PORTFOLIO_CVAR,
                            f"portfolio governing model ES_0.99 "
                            f"{int(book_risk.governing_model_es_99_cc)}cc > "
                            f"{limits.portfolio_cvar_frac} bankroll = {cvar_thr}cc",
                            shadow=shadow,
                        )
                    )
                # (8b) DETERMINISTIC maximum-loss axis — the all-hit
                # premium-at-risk, gated INDEPENDENTLY (P0-3). MUTEX-AWARE
                # (2026-07-18): when armed (the default) the gate reads the
                # snapshot's scenario-aware bound — mutually exclusive parlays
                # are charged max-over-branches within their game, never as if
                # they all hit at once — falling back to the comonotone number
                # when the field is absent/None (fail closed: the LARGER
                # bound). Threshold + reason string unchanged; BOTH bounds are
                # logged in the breach detail for live monitoring comparison.
                det_comono_cc = book_risk.deterministic_max_loss_cc
                det_mutex_cc = getattr(book_risk, "mutex_aware_det_max_cc", None)
                det_gate_cc = det_comono_cc
                if limits.portfolio_det_max_mutex_aware and det_mutex_cc is not None:
                    det_gate_cc = min(det_comono_cc, float(det_mutex_cc))
                if det_gate_cc > det_max_thr:
                    mutex_note = (
                        f"{int(det_mutex_cc)}cc"
                        if det_mutex_cc is not None
                        else "n/a"
                    )
                    out.append(
                        Breach(
                            ReasonCode.SKIP_PORTFOLIO_DET_MAX,
                            f"portfolio deterministic max loss "
                            f"{int(det_gate_cc)}cc (comonotone "
                            f"{int(det_comono_cc)}cc, mutex-aware {mutex_note}, "
                            f"mutex gating "
                            f"{'on' if limits.portfolio_det_max_mutex_aware else 'off'}) > "
                            f"{limits.portfolio_det_max_frac} bankroll = "
                            f"{det_max_thr}cc",
                            shadow=shadow,
                        )
                    )
            # (9) A2 P(RUIN) cap: P(this settlement wave drops equity below the ruin
            # floor, e.g. −30% ⇒ equity < 0.70·bankroll) vs a probability budget.
            # Reads the STRUCTURAL-MC ``p_ruin`` (which reflects same-game hedges,
            # unlike the comonotone deterministic max-loss axis), so
            # a book-balancing fill that LOWERS the joint tail lowers p_ruin and is
            # admitted. Co-equal with the analytic (mutex) + gross backstops — an
            # addition, never a demotion. Fail-closed via the ``usable`` guard above.
            # P1-2: gate the UPPER Wilson confidence bound on p̂ (``p_ruin_upper``),
            # not the point estimate, so an MC estimate that only just clears the
            # budget by sampling luck near it is declined (fail-closed against MC
            # error). ``max`` with ``p_ruin`` keeps the gate never LOOSER than the
            # point estimate even for a snapshot from a code path that left the
            # upper bound at its 0.0 default (z == 0 ⇒ upper bound == p_ruin anyway).
            ruin_budget = float(limits.portfolio_ruin_prob_budget)
            gated_ruin = max(
                book_risk.p_ruin,
                getattr(book_risk, "p_ruin_upper", book_risk.p_ruin),
            )
            if book_risk.usable and gated_ruin > ruin_budget:
                out.append(
                    Breach(
                        ReasonCode.SKIP_PORTFOLIO_RUIN,
                        f"P(ruin) {book_risk.p_ruin:.4f} (upper "
                        f"{gated_ruin:.4f}) > budget {ruin_budget:.4f} "
                        f"(equity below ruin floor this settlement wave)",
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
        certified_game_loss: dict[str, int] | None = None,
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
            # Certificate substitution (2026-07-17): a covered game's term is
            # its state-exact certified worst case (validated by the caller),
            # min'd so a certificate can only ever TIGHTEN the sum.
            if certified_game_loss and game in certified_game_loss:
                loss_cc = min(int(loss_cc), certified_game_loss[game])
            start = earliest_start.get(game)  # None or absent ⇒ UNKNOWN bucket
            slate = slate_key_for_start(start)
            slate_loss[slate] = slate_loss.get(slate, 0) + loss_cc
        return slate_loss
