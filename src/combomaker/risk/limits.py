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

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace
from datetime import datetime
from fractions import Fraction
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from combomaker.core.reasons import ReasonCode
from combomaker.rfq.eviction_value import ES_TAIL_ALPHA, marginal_tail_admit
from combomaker.risk.cap_family import det_max_backstop_frac
from combomaker.risk.entity_admission import (
    EntityLoad,
    certify_entity_admission,
    entity_loads,
)
from combomaker.risk.exposure import (
    ExposureBook,
    LossUnit,
    MarginalProvider,
    OpenPosition,
    leg_entity_key,
    mutex_scenario_bound,
    partitioned_worst_case_cc,
)
from combomaker.risk.exposure import (
    _haircut_compose_cc as _haircut_compose_cc,  # the SAME composition the
)
from combomaker.risk.exposure import (  # per-game axis uses — never a second one
    _topk_sum_int as _topk_sum_int,
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


def _wilson_upper(p_hat: float, n: int, z: float) -> float:
    """One-sided Wilson-score upper bound — KEEP IN SYNC with
    ``sim.book_risk.wilson_upper_bound`` (limits must not import sim.book_risk:
    the documented import cycle behind the PortfolioRisk Protocol). Used by the
    tail-probability form of the portfolio joint-tail cap (2026-07-25)."""
    if z <= 0.0 or n <= 0:
        return p_hat
    p = min(1.0, max(0.0, p_hat))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    halfwidth = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return min(1.0, centre + halfwidth)


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
    # ENTITY BOUND (operator 2026-07-26: "I wouldn't prefer having our book rely
    # on 1 leg like that"). Accumulated premium-at-risk on ONE
    # (family:entity x direction) key — every combo riding "Hunter Greene 4+
    # Ks" — as a %-of-bankroll ceiling. The leg-axis steer PRICES this
    # concentration; this is the hard wall that REFUSES it. None (default) ⇒
    # the axis is not evaluated (byte-identical to before).
    entity_loss_frac: Fraction | None = None
    # TIERED ENTITY LOAD (operator 2026-07-28: "Risk engine = protection not
    # limitation"). The operator's spec, on the entity's load as a percent of
    # bankroll: <1% no action | 1-2% tier 1 | 2-3% tier 2 | >3% DECLINE.
    # False (default) = SHADOW: the entity wall refuses on the WORST single key
    # exactly as today, and the certificate is only ever handed to
    # ``entity_admission_observer`` for the read-out. True = ARMED: a candidate
    # that pushes a key past ``entity_loss_frac`` is admitted when ALL of
    #   TIER   — the key was COOL (< the 1% anchor) BEFORE this candidate, so the
    #            breach is a SIZE event, not the ACCUMULATION ACROSS STRUCTURES
    #            the 3% wall was ratified to refuse (an already-warm key is never
    #            certified — the accumulation wall stays exactly 3%),
    #   NET    — the combo is net DIVERSIFYING in DOLLARS across ALL its legs,
    #            never judged on the worst leg alone, and
    #   SCALE  — the LIVE accumulated exposure on that key still fits the LIVE
    #            per-COMBO wall (``per_combo_loss_frac``) — "the size protection
    #            already exists as per-combo (5%) and skip_size_above_max" —
    #            re-validated at the point of enforcement.
    # The ONLY new numbers in the whole lever are the 1% and 2% tier anchors
    # (NORTH STAR layer 2, operator risk appetite stated once); the decline line
    # IS ``entity_loss_frac`` and the ceiling IS ``per_combo_loss_frac``, both
    # already ratified. Every portfolio SCALE protection (det-max, ruin,
    # CVaR/KILL-tail, slate, per-game, per-combo, the halts) is untouched either
    # way, and the per-combo cap is byte-identical in every flag state.
    entity_admission_armed: bool = False
    # …and its DERIVE-BEFORE-ARM companion, the same ``pbook_enabled`` /
    # ``pbook_armed`` split the tree already uses: True ⇒ the certificate is
    # BUILT and handed to ``entity_admission_observer`` with the decision the
    # ARMED cap WOULD take, while the cap still refuses exactly as today. Off by
    # default so an untouched deployment pays nothing at all.
    entity_admission_enabled: bool = False
    # P1 STAGE-1 STRUCTURE BOUND (operator 2026-08-13: "caps any one structure
    # at the ~1% anchor" — the whale seam, 6 sightings, top-3 tickets = 82% of
    # the 8/13 −$473). Bounds the ACCUMULATED committed+reserved+candidate
    # premium (``ExposureSnapshot.loss_by_combo_cc`` — max_loss_cc, the SAME
    # measure as ``per_combo_loss_frac``; never summed with the directional or
    # notional axes) on ONE combo MARKET (the dossier's "structure").
    # None (default) ⇒ the axis is not evaluated (byte-identical). The arming
    # value is the ratified per-combo ANCHOR (1% — cap_family.PER_COMBO_FRAC),
    # never a new number; derived-caps deployments track the derived anchor.
    structure_loss_frac: Fraction | None = None
    structure_bound_armed: bool = False
    # DERIVE-BEFORE-ARM companion (the entity/slate split): True ⇒ the verdict
    # the ARMED bound would take is handed to ``structure_bound_observer``
    # while enforcement stays exactly today's. Off by default: zero cost.
    structure_bound_enabled: bool = False
    # One-directional / theme: net directional exposure to one leg outcome across
    # games (LOSS-equivalent; see the check site for the interpretation). 10%.
    directional_frac: Fraction = Fraction(10, 100)
    # P1 STAGE-1 GAME-DIRECTION NET BOUND (operator 2026-08-13): the
    # ACCUMULATED one-direction net per game — committed + reserved +
    # candidate, mutex-aware branch-max fold (``directional_net_by_game_cc``,
    # LOSS-EQUIVALENT directional cc, the SAME measure as ``directional_frac``)
    # — judged against the SUNK committed baseline (marginal, never a level
    # gate on the standing book: the 2026-08-01 constitutional). None
    # (default) ⇒ axis off, byte-identical. Waivable (a certified hedge always
    # fills). Arming fraction: derived (see derived_cap_engine join) or a
    # ratified layer-2 anchor — never a hand number.
    game_direction_net_frac: Fraction | None = None
    game_direction_net_armed: bool = False
    game_direction_net_enabled: bool = False
    # SLATE / time-window pre-trade cap: Σ worst_case_loss_by_game over all games
    # in ONE slate (LOSS axis). Start = same as the game cap. 8%.
    slate_loss_frac: Fraction = Fraction(8, 100)
    # FIX 2 — SLATE AGGREGATION BY PARTITION (operator 2026-07-27: "stop summing
    # losses that cannot all occur ... compute the exposure CORRECTLY").
    # False (default) = SHADOW: the slate cap keeps summing
    # ``worst_case_loss_by_game_cc``, which charges a multi-game parlay's FULL
    # max_loss once PER GAME it touches — byte-identical to today, and the
    # corrected number is only ever handed to ``slate_partition_observer``.
    # True = ARMED: the slate's exposure is the ENUMERATED JOINT WORST CASE over
    # loss events, each counted EXACTLY ONCE (risk/exposure.partitioned_worst_
    # case_cc — the same Stage-B mutex fold, the same bucketing rule
    # ``sim.book_risk._mutex_aware_det_fold`` uses, clamped into [largest single
    # loss, once-counted comonotone sum]). THE THRESHOLD DOES NOT MOVE: this is
    # an arithmetic repair of the measure, not a raised cap. Measured live: the
    # roll-up read $2,610.28 against $1,358.71 of real premium (1.92x), and 0 of
    # 25,347 slate refusals could have been a real breach of the ratified
    # fraction on an honest once-counted book.
    slate_partition_armed: bool = False
    # …and its DERIVE-BEFORE-ARM companion. True ⇒ the corrected number is
    # COMPUTED (and handed to ``slate_partition_observer``) on every slate the
    # naive sum would refuse, while the naive sum still enforces. Off by default
    # because it is the ONLY consumer of ``ExposureSnapshot.loss_units``, whose
    # construction is the one allocation this build adds to the hot path — with
    # it off, ``check`` costs what it costs at HEAD (measured, see
    # tools/diagnostics/bench_admission_fixes.py).
    slate_partition_enabled: bool = False
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
    # TAIL-PROBABILITY FORM of the portfolio joint-tail cap (operator anchor
    # ratified 2026-07-25: "more bets = more variance = more money" — risk-on
    # per bet, one-sided concentration capped by the per-game/direction/
    # structure walls, the TOTAL book bound by the PROBABILITY of a
    # KILL-distance night, not the ES99 average, which at small N with
    # ~50%-loss positions barely credits diversification and capped total
    # premium near the KILL distance regardless of variance). When armed, the
    # CVaR-axis check binds `P(book loss >= portfolio_cvar_frac x bankroll)
    # <= portfolio_kill_tail_prob` off the snapshot's loss-quantile envelope
    # (worst credible model, Wilson-upper at portfolio_tail_prob_ci_z);
    # legacy snapshots without the envelope fall back to the ES form (never
    # a free pass). Default OFF = byte-identical ES behavior.
    portfolio_tail_prob_gate: bool = False
    portfolio_kill_tail_prob: float = 0.02
    portfolio_tail_prob_ci_z: float = 0.0
    # ── KILL-ANCHORED BOOK GATE (2026-07-29, demotion RATIFIED 2026-07-31) ──
    # THE DEFECT it repairs. The operator ratified P(KILL-night) <= 2% at the
    # 12%-of-bankroll KILL line, and ``portfolio_tail_prob_gate`` has been ARMED
    # live since 2026-07-25 — but the armed form thresholds the probability on
    # ``portfolio_cvar_frac x bankroll`` (0.35 live), NOT on the KILL line
    # ``hard_trip_frac x bankroll`` (0.12). With ``portfolio_det_max_frac``
    # 0.36 the ratio is 0.35/0.36 = 0.9722, so 2% of scenarios would have to
    # reach 97.22% of the COMONOTONE MAXIMUM loss before the gate could fire.
    # It never has: measured on the 2026-07-28 tape the breach string
    # "P(book loss >= " occurs 0 times in 104,803 ``risk_audit`` rows and
    # "deterministic max loss" 0 times — BOTH portfolio envelope axes refused
    # ZERO fills, and every one of the 1,579 ``skip_portfolio_cvar`` declines
    # was fail-closed STALENESS. The dollar cap did all the governing and the
    # operator's actual risk anchor was decorative.
    #
    # WHY THE DOLLAR CAP CANNOT DO THIS JOB. det-max IS the premium sum
    # (``exposure.max_loss_cc``; live comonotone $408.84 == premium $408.84,
    # ratio 1.000001), so it is BLIND TO SHAPE: five books each holding
    # EXACTLY $693.89 span ES99 $345.96 -> $238.94 and P(KILL-night)
    # 0.00900 -> 0.00100 (9x) with det-max IDENTICAL TO THE CENT. The
    # $1,058.50 wall ADMITS a 2-ticket book at P(KILL-night) 4.9% (2.45x the
    # ratified budget) and REFUSES a 64-ticket book at 0.6% — wrong in both
    # directions.
    #
    # WHAT ARMING DOES — TWO things, both off RATIFIED anchors only:
    #   1. the tail-PROBABILITY form thresholds on the KILL line
    #      ``hard_trip_frac x bankroll`` with budget
    #      ``portfolio_kill_tail_prob`` — the two anchors the operator actually
    #      ratified — instead of ``portfolio_cvar_frac x bankroll``. The KILL
    #      gate becomes the GOVERNING constraint.
    #   2. DET-MAX DEMOTION (operator RATIFICATION 2026-07-31, "ratify and
    #      finish number 2 ... open it up as it was on 7/29 just with more
    #      capacity"): the deterministic max-loss wall moves from the
    #      MC-positioned ``portfolio_det_max_frac`` (0.36 live) to the
    #      MODEL-FREE BACKSTOP ``cap_family.det_max_backstop_frac()``
    #      (= 1 - RUIN_FLOOR_FRAC = 0.70 of bankroll, ~$2,099 at the 7/31
    #      boot equity) — "even if the ENTIRE admitted book loses
    #      simultaneously, equity stays above the 30% [equity] floor". The
    #      demotion applies ONLY while the KILL gate can actually govern
    #      (``portfolio_tail_prob_gate`` armed too): no governor, no demotion.
    #
    # WHAT THE 2026-07-31 MEASUREMENT PUT ON RECORD (it argued AGAINST the
    # demotion; the operator ratified WITH it on the table — the full copula
    # sweep and the ruin-convention collision live in
    # ``cap_family.det_max_backstop_frac`` and are pinned by
    # tests/test_kill_anchored_book_gate.py):
    #  * the ENFORCED p_ruin axis reads ruin as a 30% DRAWDOWN (equity <
    #    0.70 x bankroll), under which the backstop permits a comonotone
    #    collapse of 2.33x the enforced ruin distance;
    #  * the 64-ticket book the demotion admits is a 20.8%-P(KILL) event at
    #    cross-game rho 0.25 and a 29.5%-probability 30%-drawdown at rho 1.0;
    #  * arming therefore TRADES the copula-free wall out to the operator's
    #    stated floor and concentrates day-to-day protection on the
    #    copula-DEPENDENT KILL gate + p_ruin budget. That trade is the
    #    ratification.
    #
    # SMALL-BOOK HONESTY: below ~15 tickets the KILL-line hit count is
    # quantized (at n=8 one ticket flips P(KILL) 1.13% <-> 5.80%, the 2%
    # budget falls INSIDE the gap), so on tiny CONCENTRATED books the armed
    # gate binds TIGHTER than today's dollar wall. That is the gate working —
    # the capacity the ratification buys arrives with DIVERSE books, not
    # concentrated ones.
    #
    # The ES_0.99 fallback form is UNCHANGED and still binds on
    # ``portfolio_cvar_frac`` — an ES is an average-of-the-worst-1% magnitude,
    # not a KILL-distance probability, so re-anchoring IT to 0.12 would brick
    # quoting. Staleness/unusable snapshots fail closed exactly as today.
    # Default False = SHADOW = byte-identical (proven over 20,000 replayed
    # limit cases + 2,000 candidate-gate cases,
    # tools/diagnostics/kill_anchor_shadow_golden.py).
    kill_anchored_book_gate: bool = False
    # ── MARGINAL KILL GATE (2026-08-01, operator ruling — the sunk-book
    # principle) ──────────────────────────────────────────────────────────
    # THE DEFECT the LEVEL form showed live (2026-08-01, boot 14:49Z): armed
    # on an INHERITED 26-position book whose measured P(loss >= the 12% KILL
    # line) read 0.110-0.115 against the 0.02 budget, §(8a) refused EVERY
    # candidate — 140,338 skip_portfolio_cvar rows, quote_sent = 0 — because
    # the level check judges the BOOK, which no candidate can change. The
    # operator ruled (verbatim): "The only reason the book could be -EV is if
    # odds have changed... even if we did have a -EV book we should still
    # quote to increase it; when we fill something we always fill at +EV;
    # what happens after that we can't decide, besides quoting more and
    # filling more." The STANDING book is SUNK (sell-only, no unwind); the
    # ABSOLUTE level constraint belongs to the model-free det-max backstop
    # (0.70B, stays); the KILL gate must judge the MARGINAL candidate.
    #
    # TWO REGIMES when this flag arms ON TOP of the armed
    # ``kill_anchored_book_gate`` + ``portfolio_tail_prob_gate``:
    #   * UNDER budget (book P(KILL) <= ``portfolio_kill_tail_prob``):
    #     behaviour UNCHANGED — §(8a) is silent at quote time and the
    #     candidate-MC gate admits iff POST P(KILL) <= budget, exactly as
    #     armed today.
    #   * OVER budget (the inherited book already past the budget): do NOT
    #     freeze. A candidate is admitted iff its MARGINAL effect is
    #     justified — ``marginal_tail_admit`` (rfq/eviction_value.py):
    #         admit  iff  dES99 x ES_TAIL_ALPHA <= dEV
    #     CORRECTED 2026-08-14 from the original ``dES99 <= dEV x
    #     P(accept, CP-LOWER)`` form after the arming-day incident (1,307
    #     quotes / 4 fills / 6 of 10 accepts reneged; store-measured
    #     refusals 174,000-292,000x over). Two derivation fixes, both on
    #     the function's docstring: P(accept) cancels at a fill-conditional
    #     decision (the eviction metric it reused holds risk
    #     unconditionally — this site does not), and dES99 is
    #     conditional-on-tail so it carries the ES tail mass before it can
    #     be compared to unconditional EV. Certified risk-reducers (the
    #     existing hedge machinery) always admit. The boundary stays
    #     ``<=``: a DIVERSIFYING candidate on a game the tail decomposition
    #     does not touch has dES99 == 0 exactly and admits immediately even
    #     at zero EV, while CONCENTRATING flow (dES99 > 0) must earn its
    #     tail-weighted charge — fail-closed exactly where it must be
    #     (VALIDATE-CAPS-CAN-QUOTE, 2026-07-23 and again 2026-08-14).
    #   The det-max backstop, the staleness fail-closed (an unusable /
    #   unmeasured snapshot refuses BOTH axes before any regime is read),
    #   P(ruin) and every deploy-side wall are UNTOUCHED. A caller that
    #   cannot supply the candidate's marginal facts (``kill_marginal``
    #   None) keeps the LEVEL form — UNKNOWN never admits.
    # Default False = the level form exactly as armed 2026-08-01 morning —
    # byte-identical.
    kill_gate_marginal: bool = False
    # ── MARGINAL RUIN GATE (2026-08-01, the ruin axis of the same sunk-book
    # ruling — constitutional: the standing book is SUNK; only the model-free
    # det-max backstop (0.70B) is a LEVEL gate; every other risk gate judges
    # the MARGINAL candidate). THE FREEZE this repairs (2026-08-01 evening
    # slate, measured): §(9)'s level form read p_ruin 0.2994 == upper vs the
    # 0.05 budget and refused EVERYTHING — skip_portfolio_ruin 1,044/5 min,
    # quote_sent = 0 — while every new fill was PREGAME-ONLY (future games =
    # diversifiers against the in-play book carrying the ruin mass); three
    # in-flight fills that DID land moved the measured p_ruin 0.2994 → 0.1649
    # within 90 s. Armed (with the lifecycle supplying the candidate's
    # marginal facts): an OVER-budget book admits certified risk-reducers and
    # candidates whose allocated dES99 <= dEV × CP-lower P(accept) — the SAME
    # ``KillMarginalCandidate`` object and criterion the marginal KILL gate
    # rides (one machinery, two axes) — and refuses concentrators; the
    # confirm-path mirror in ``sim/book_risk._candidate_gate`` (4) admits
    # only fills that do not RAISE the CRN-measured P(ruin) (certified
    # reducers exempt). UNDER budget: §(9) is silent at quote time exactly as
    # today. Independent of ``kill_anchored_book_gate`` (the ruin axis has no
    # anchor re-read to guard). Default False = byte-identical level form.
    ruin_gate_marginal: bool = False
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
    # FIX 3 HEDGE ACCOUNTING (2026-07-28) — the RISK-LEDGER axis, deliberately
    # distinct from the 2026-07-27 skew work (which changed the PRICE we quote
    # on offsetting flow, not what it CONSUMES). The mutex fold buckets only
    # long-NO units per game, so a COMPLEMENT position — the opposite side of a
    # combo we already hold, which cannot lose when that one does — fell into
    # the comonotone residual and was charged its FULL premium ON TOP of the
    # position it offsets. True: the det-max cap subtracts the snapshot's
    # ``det_max_hedge_credit_cc`` (disjoint certified-exclusive pairs, charged
    # ONCE instead of twice) from the mutex-aware bound. False (default,
    # SHADOW): the credit is measured and logged in the breach detail but never
    # gates — byte-identical. Certification is state enumeration over leg-outcome
    # literals (sim/book_risk._certified_cannot_both_lose), never a leg-sign
    # heuristic; anything ambiguous stays charged in full.
    det_max_hedge_credit: bool = False
    # FIX 2 SETTLED-LEG DET-MAX (2026-07-28). The deterministic max-loss axis is
    # a FORWARD bound, but it was computed unconditionally — a combo whose games
    # are over and whose outcome the exchange has already DETERMINED still
    # carried its full premium (live 2026-07-28: 14 of 77 positions, $80.20,
    # against $3.34–$4.60 of binding headroom). True: a position the exchange's
    # graded legs prove is WON contributes 0 to both det-max axes. False
    # (default, SHADOW): measured into the snapshot's
    # ``det_max_settled_credit_cc`` and logged, never gates — byte-identical.
    # The flag rides here so the quote-time cap, the candidate gate and the
    # book-risk MC all read ONE value and cannot diverge.
    det_max_settlement_aware: bool = False
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


# --- DEPLOYMENT SCALE (see risk/deploy_scale.py for the solver + rationale) ---
# The DEPLOY-SIDE budgets — the shape caps that say how much of the book may ride
# ONE combo / entity / game / slate / direction. These are the ONLY fields a
# solved deployment scale may breathe. Everything not named here (the portfolio
# envelope the scale is SOLVED AGAINST — CVaR / det-max / ruin budget /
# tail-prob anchor — the halts, and every absolute backstop) is invariant under
# ``scale_deploy_budgets`` BY CONSTRUCTION: a field must be listed to move, so
# the scale can never raise its own ceiling. Lives here (not in deploy_scale)
# because it operates on RiskLimits and ``check`` must reach it without the
# import cycle; ``risk.deploy_scale`` re-exports both names.
DEPLOY_BUDGET_FIELDS: tuple[str, ...] = (
    "per_combo_loss_frac",
    "entity_loss_frac",
    "game_loss_frac",
    "slate_loss_frac",
    "directional_frac",
)

_DEPLOY_SCALE_Q = 1_000_000


def as_exact(scale: float) -> Fraction:
    """Quantize a SOLVED float scale to an exact ``Fraction`` (6 dp), truncated
    toward zero so the quantization can only ever make the scale SMALLER — the
    same convention ``cap_family.CapFractions.as_fractions`` uses when a solved
    float becomes a live threshold (floats are never live thresholds)."""
    return Fraction(int(scale * _DEPLOY_SCALE_Q), _DEPLOY_SCALE_Q)


def scale_deploy_budgets(limits: RiskLimits, scale: float) -> RiskLimits:
    """``limits`` with ONLY :data:`DEPLOY_BUDGET_FIELDS` multiplied by ``scale``.

    ``scale <= 1`` returns the SAME OBJECT (byte-identical default — the whole
    feature is inert until a caller passes a solved scale > 1). An unarmed
    (None) axis stays unarmed: a scale never INVENTS a cap that was off. Exact
    ``Fraction`` arithmetic throughout."""
    if scale <= 1.0:
        return limits
    q = as_exact(scale)
    if q <= 1:
        return limits
    updates: dict[str, Any] = {}
    for name in DEPLOY_BUDGET_FIELDS:
        cur = getattr(limits, name, None)
        if cur is None:
            continue
        updates[name] = cur * q
    if not updates:
        return limits
    return dataclasses_replace(limits, **updates)


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


@dataclass(frozen=True, slots=True)
class KillMarginalCandidate:
    """The MARGINAL facts of ONE candidate for the two-regime KILL gate
    (``RiskLimits.kill_gate_marginal`` — the 2026-08-01 sunk-book ruling).

    Built by the LIFECYCLE (the one owner of the acceptance tape, the stored
    quote-time EV and the snapshot's per-game tail decomposition) and passed
    into ``check``; every field reuses machinery that already ships:

      * ``ev_cc`` — the candidate's quote-time EV, the SAME
        ``_quote_candidate_ev_cc`` figure the eviction ranking stores (None =
        UNKNOWN ⇒ the caller must not construct this object; a None input to
        ``check`` keeps the LEVEL form — UNKNOWN never admits).
      * ``p_accept_lower`` — the exact Clopper-Pearson LOWER bound of the
        candidate's premium-size bucket on the in-process acceptance tape at
        the RATIFIED alpha (``portfolio_kill_tail_prob``) —
        ``rfq/eviction_value.py`` verbatim. A thin/empty bucket reads 0.0
        (fail-closed: unmeasured acceptance buys no concentrating capacity).
        Confirm-path callers pass 1.0 — the accept HAPPENED; the probability
        discount is a quote-time realism haircut, not a confirm-time one.
      * ``des99_cc`` — the candidate's allocated marginal tail contribution
        from the book-risk MC's additive per-game CVaR decomposition
        (``allocate_des99_cc``): 0 for a game the book's tail does not touch
        (a diversifier), positive on a tail game (a concentrator), negative
        on a hedge game.
      * ``certified_risk_reducing`` — True ONLY from the existing hedge
        certification machinery (state enumeration / CRN tail comparison),
        never a leg-sign heuristic. Certified reducers always admit
        ("hedges are +EV").
    """

    ev_cc: int
    p_accept_lower: float
    des99_cc: float
    certified_risk_reducing: bool = False


def kill_envelope_tail_upper(
    book_risk: PortfolioRisk | None,
    limits: RiskLimits,
    bankroll_cc: int | None,
) -> float | None:
    """The Wilson-upper P(book loss >= the armed tail threshold) off the
    snapshot's loss-quantile envelope — THE ONE implementation of the number
    §(8a)'s tail-probability form gates on, factored out so the lifecycle's
    regime probe (does the marginal form apply?) and the cap itself can never
    diverge (same function, same inputs ⇒ same regime).

    Returns None when the tail-probability form cannot evaluate on these
    inputs — gate off, no bankroll, unusable snapshot, or no envelope — in
    which case §(8a) takes its ES-fallback / fail-closed paths exactly as
    before and the marginal form NEVER applies (a regime that cannot be
    measured is never "over budget with a marginal bypass"; it is the
    fail-closed level behaviour)."""
    if book_risk is None or not book_risk.usable:
        return None
    if not limits.portfolio_tail_prob_gate:
        return None
    if bankroll_cc is None or bankroll_cc <= 0:
        return None
    quantiles = getattr(book_risk, "loss_quantiles_cc", ()) or ()
    n_mc = int(getattr(book_risk, "n_samples", 0) or 0)
    if not quantiles or n_mc <= 0:
        return None
    tail_thr = threshold_cc(
        limits.hard_trip_frac
        if limits.kill_anchored_book_gate
        else limits.portfolio_cvar_frac,
        bankroll_cc,
    )
    n_grid = len(quantiles)
    k_ge = sum(1 for q in quantiles if q >= tail_thr)
    p_hat = k_ge / max(1, n_grid - 1)
    return _wilson_upper(min(1.0, p_hat), n_mc, limits.portfolio_tail_prob_ci_z)


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


class ConcentrationCertificate(Protocol):
    """TIERED ENTITY-LOAD certificate for ONE (family:entity x direction) key —
    the WaiverCertificate doctrine (certification by STATE ENUMERATION, never a
    leg-sign heuristic) applied to the operator's tiered widening spec.

    Structurally ``risk.entity_admission.EntityAdmissionCertificate``; a Protocol
    for the same reason ``WaiverCertificate`` is one — the check site types the
    contract, not the producer. ``certified`` True means the enumeration of the
    candidate's COMPLETE per-key dollar footprint (every key it touches, with
    that key's own PRIOR dollars and its own ADDED dollars) proved all three of:
    the breaching key was COOL before this candidate (< the 1% tier-1 anchor, so
    the breach is a SIZE event and not the ACCUMULATION the 3% wall was ratified
    to refuse), the combo is NET DIVERSIFYING in dollars across ALL its legs, and
    the key's accumulated load fits the LIVE per-COMBO wall.

    ``widen_weight_pct`` and the tier fields are REPORTING (every decision inside
    the producer is exact integer / Fraction); they are on the Protocol because
    the breach detail and the shadow read-out print them.
    """

    @property
    def key(self) -> str: ...

    @property
    def certified(self) -> bool: ...

    @property
    def verdict(self) -> str: ...

    @property
    def key_loss_cc(self) -> int: ...

    @property
    def prior_cc(self) -> int: ...

    @property
    def add_cc(self) -> int: ...

    @property
    def prior_tier(self) -> int: ...

    @property
    def post_tier(self) -> int: ...

    @property
    def diversifying_cc(self) -> int: ...

    @property
    def concentrating_cc(self) -> int: ...

    @property
    def candidate_total_cc(self) -> int: ...

    @property
    def widen_weight_pct(self) -> float: ...

    @property
    def n_cool_keys(self) -> int: ...

    @property
    def n_loaded_keys(self) -> int: ...


EntityAdmissionObserver = Callable[[ConcentrationCertificate, int, bool], None]
"""SHADOW read-out sink: ``(certificate, ceiling_cc, would_admit)`` for every
entity-axis certificate ``check`` builds — admitted AND refused, armed AND
unarmed. Purely observational: ``check`` ignores whatever it returns and never
branches on it, so a logging callback can never change a risk decision. None
(default) ⇒ never called and never built — zero hot-path cost. It fires only
once a key has ALREADY breached, so it is bounded by the breach rate."""


SlatePartitionObserver = Callable[[str, int, int, int], None]
"""SHADOW read-out sink for FIX 2: ``(slate, naive_cc, partitioned_cc,
threshold_cc)``. Fires ONLY on a slate the NAIVE roll-up would refuse (the case
the operator is deciding about), so the happy path pays nothing. Purely
observational — ``check`` never branches on it."""


StructureBoundObserver = Callable[[str, int, int, bool], None]
"""P1 Stage-1 read-out sink: ``(combo_ticker, accumulated_cc, threshold_cc,
would_refuse)`` for every candidate structure over the anchor. Purely
observational — ``check`` never branches on it; fires only past the threshold,
so it is bounded by the breach rate. None (default) ⇒ zero cost."""


GameDirectionObserver = Callable[[str, int, int, int, bool], None]
"""P1 Stage-1 read-out sink: ``(game, net_with_cc, committed_baseline_cc,
threshold_cc, would_refuse)`` for every candidate game whose accumulated
one-direction net crosses the fraction AND rises above the sunk committed
baseline. Purely observational; bounded by the breach rate."""


def _entity_admits(
    cert: ConcentrationCertificate | None,
    key: str,
    live_key_loss_cc: int,
    ceiling_cc: int,
) -> bool:
    """Whether a tiered entity certificate may waive the entity wall.

    TWO conditions, and only two, both re-validated HERE at the point of
    enforcement (the previous build advertised four and two of them compared a
    value to itself inside one synchronous pure call — a guard that cannot fire
    is not a guard):

    * TIER — the certificate exists, is CERTIFIED (the key was COOL before this
      candidate, the combo is net diversifying in dollars, and the load fits the
      ceiling) and describes THIS key (a certificate for another key never
      travels). A key that was ALREADY loaded is never certified, which is what
      keeps the ratified 3% ACCUMULATION wall at 3%.
    * SCALE — the LIVE accumulated loss on the key still fits ``ceiling_cc``,
      which the caller derives PER CHECK from the LIVE bankroll and the LIVE
      per-COMBO wall. "The size protection already exists as per-combo (5%) and
      skip_size_above_max" — so the admission introduces NO new number and can
      never uncap a key: a certificate built against a larger bankroll can only
      ever be REJECTED by a tighter live wall, never honoured against a looser
      one.
    """
    return (
        cert is not None
        and cert.certified
        and cert.key == key
        and int(live_key_loss_cc) <= int(ceiling_cc)
    )


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

    # FIX 3 (2026-07-28): the offsetting-position det-max credit measured on
    # this snapshot (>= 0; 0.0 when uncomputed). Read via ``getattr`` with a
    # 0.0 fallback in ``check`` so a snapshot/fake predating this field simply
    # takes no credit (fail closed, never looser).
    @property
    def det_max_hedge_credit_cc(self) -> float: ...


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
        deploy_scale: float = 1.0,
        entity_admission_observer: EntityAdmissionObserver | None = None,
        slate_partition_observer: SlatePartitionObserver | None = None,
        structure_bound_observer: StructureBoundObserver | None = None,
        game_direction_observer: GameDirectionObserver | None = None,
        kill_marginal: KillMarginalCandidate | None = None,
    ) -> list[Breach]:
        """All current breaches, mass-acceptance included.

        ``kill_marginal`` (2026-08-01 sunk-book ruling — see
        ``RiskLimits.kill_gate_marginal``): the contemplated candidate's
        marginal facts (quote-time EV, measured CP-lower P(accept),
        allocated dES99, hedge certification). Consulted by EXACTLY ONE
        branch — the §(8a) tail-probability form when the marginal flag is
        armed AND the book's own envelope is already OVER the KILL budget —
        where it replaces the level refusal with the marginal admission
        test. None (the default, and every book-only/maintenance caller)
        keeps the LEVEL form byte-identically: UNKNOWN never admits.

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
        # DEPLOYMENT SCALE (risk/deploy_scale.py). ``deploy_scale`` is SOLVED off
        # the hot path from the live envelope — the largest uniform book scaling
        # that still clears EVERY enforced cap, including the ratified
        # ``portfolio_kill_tail_prob`` anchor. It breathes ONLY the deploy-side
        # budgets (per-combo / entity / game / slate / directional); the
        # ENVELOPE this very number was bounded by (portfolio CVaR, det-max,
        # ruin budget), the HALTS and every absolute backstop are invariant by
        # construction (``DEPLOY_BUDGET_FIELDS`` is the exhaustive list), so the
        # scale can never move its own ceiling. Default 1.0 ⇒ byte-identical
        # (``scale_deploy_budgets`` returns the SAME object) and the whole
        # feature is inert unless the caller passes a solved scale.
        limits = (
            self._limits
            if deploy_scale <= 1.0
            else scale_deploy_budgets(self._limits, deploy_scale)
        )
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
            # FIX 2: the ONLY consumer of the once-counted loss events is the
            # slate partition. Build them only when that lever will read them,
            # so an untouched deployment pays exactly what it paid before.
            want_loss_units=(
                limits.slate_partition_armed or limits.slate_partition_enabled
            ),
            # P1 Stage-1: the accumulated per-game direction fold is built only
            # when its lever will read it (same convention as loss_units).
            want_directional_net=(
                limits.game_direction_net_frac is not None
                and (
                    limits.game_direction_net_armed
                    or limits.game_direction_net_enabled
                )
            ),
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
                entity_admission_observer=entity_admission_observer,
                slate_partition_observer=slate_partition_observer,
                structure_bound_observer=structure_bound_observer,
                game_direction_observer=game_direction_observer,
                kill_marginal=kill_marginal,
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
        entity_admission_observer: EntityAdmissionObserver | None = None,
        slate_partition_observer: SlatePartitionObserver | None = None,
        structure_bound_observer: StructureBoundObserver | None = None,
        game_direction_observer: GameDirectionObserver | None = None,
        kill_marginal: KillMarginalCandidate | None = None,
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
        # (3b) ACCUMULATED per-combo loss (2026-07-25 — the 7/23 re-hit
        # bypass: mass-acceptance re-hits of ONE structure grew a $74 combo
        # to $149.24 past the 5% cap because only the CANDIDATE was ever
        # checked). ``loss_by_combo_cc`` folds committed + reserved positions
        # + this check's candidates/reservations on the same combo MARKET
        # (never resting quotes — the serial reservation chain re-checks at
        # every fill, so the accumulation binds exactly there). SAME anchor,
        # SAME reason code: enforcement repair, not a new number. Emitted
        # only when there IS accumulation beyond the candidate itself (a lone
        # candidate is exactly check (3) above).
        seen_combo: set[str] = set()
        for position in candidates:
            ticker = position.combo_ticker
            if ticker in seen_combo:
                continue
            seen_combo.add(ticker)
            total = snapshot.loss_by_combo_cc.get(ticker, 0)
            if total > combo_thr and total > position.max_loss_cc:
                out.append(
                    Breach(
                        ReasonCode.SKIP_PER_COMBO_LOSS_CAP,
                        f"combo {ticker} ACCUMULATED loss {total}cc "
                        f"(committed+reserved+candidate) > "
                        f"{limits.per_combo_loss_frac} bankroll = {combo_thr}cc",
                        shadow=shadow,
                    )
                )

        # (3c) ENTITY BOUND (operator 2026-07-26: "I wouldn't prefer having our
        # book rely on 1 leg like that"). Same accumulation shape as (3b) but
        # keyed on (family:entity x DIRECTION) instead of the combo market: at
        # most ``entity_loss_frac`` of bankroll may ride ONE player/team in ONE
        # direction across ALL combos and games. The 7/25 book carried ~$420 on
        # four pitchers' arms with nothing to refuse it — the leg-axis steer
        # only PRICED that concentration. ``loss_by_entity_cc`` folds committed
        # + reserved + this check's candidates (never resting quotes — the
        # serial reservation chain re-checks at every fill). Unset (None,
        # default) ⇒ the axis is not evaluated (byte-identical).
        if limits.entity_loss_frac is not None and candidates:
            entity_thr = threshold_cc(limits.entity_loss_frac, bankroll)
            # ONLY the entity keys THIS CANDIDATE touches (2026-07-26 live
            # bricking): the first cut scanned every key in the book, so one
            # already-over arm (Buehler at $134 vs a $74 wall) declined EVERY
            # quote, including combos with no exposure to him — 0 quotes sent.
            # A cap must refuse the flow that WORSENS the breach, never
            # unrelated flow (and per the validate-caps-can-quote rule, it
            # must be proven to still quote before arming).
            candidate_keys: set[str] = set()
            for position in candidates:
                for leg in position.legs:
                    candidate_keys.add(leg_entity_key(leg))
            # TIERED ENTITY LOAD (operator 2026-07-28: "Risk engine =
            # protection not limitation. Those risk caps should be protecting us
            # as intended, right now they're limiting us"). The wall above judges
            # a combo by its WORST key, and the measurement says that costs us
            # twice: on 77.9% of its refusals the candidate's OWN premium alone
            # cleared the wall with ZERO prior dollars on the key (54.8% of
            # breach-key instances sit on keys with ZERO prior dollars) — the
            # entity wall, 3%, sits BELOW the per-COMBO wall, 5%, so it fires as a
            # second, 40%-tighter per-combo cap. Median ticket SENT $9.19 vs
            # REFUSED $127.05; the largest ticket ever sent was $69.27 against a
            # $70.95 wall.
            #
            # The operator's spec, tiered on the entity's load as a percent of
            # bankroll: <1% no action | 1-2% tier 1 | 2-3% tier 2 | >3% DECLINE.
            # When ARMED a breaching key is admitted only when
            # ``risk/entity_admission.py`` certifies ALL THREE:
            #   TIER  — the key was COOL (< the 1% anchor) BEFORE this candidate,
            #           so the breach is a SIZE event, not the ACCUMULATION
            #           ACROSS STRUCTURES the 3% wall was ratified to refuse. A
            #           key that was already warm is NEVER certified — that is
            #           what keeps the per-entity accumulation wall at 3%.
            #   NET   — the combo is net DIVERSIFYING in DOLLARS across ALL its
            #           legs ("it should be judged based on other legs as well,
            #           if they diversify further or concentrate other legs we
            #           have"), never on the worst leg alone.
            #   SCALE — the LIVE accumulated key loss still fits ``combo_thr``,
            #           the LIVE per-COMBO wall. "The size protection already
            #           exists as per-combo (5%) and skip_size_above_max."
            # Only the 1% and 2% tier anchors are new (NORTH STAR layer 2 —
            # operator risk appetite stated once); the decline line IS
            # ``entity_loss_frac`` and the ceiling IS ``per_combo_loss_frac``,
            # both already ratified. Every portfolio SCALE protection (det-max,
            # ruin, CVaR/KILL-tail, slate, per-game, per-combo, the halts) is
            # untouched, and the per-combo cap is byte-identical in every flag
            # state.
            #
            # SHADOW (default): with ``entity_admission_enabled`` the certificate
            # is BUILT and handed to the observer with the would-be decision, and
            # NEVER changes the breach list — the read-out the operator arms
            # from. With both flags off nothing is built at all. Either way the
            # loads are enumerated LAZILY, only once a key has actually breached,
            # so a clean quote pays nothing.
            loads: tuple[EntityLoad, ...] | None = None
            for key in sorted(candidate_keys):
                loss_cc = snapshot.loss_by_entity_cc.get(key, 0)
                if loss_cc <= entity_thr:
                    continue
                cert: ConcentrationCertificate | None = None
                if limits.entity_admission_armed or limits.entity_admission_enabled:
                    if loads is None:
                        loads = entity_loads(
                            candidate_legs_by_position=[
                                (
                                    [leg_entity_key(leg) for leg in p.legs],
                                    int(p.max_loss_cc),
                                )
                                for p in candidates
                            ],
                            post_by_key=snapshot.loss_by_entity_cc,
                            bankroll_cc=bankroll,
                            decline_frac=limits.entity_loss_frac,
                        )
                    cert = certify_entity_admission(
                        key=key,
                        loads=loads,
                        bankroll_cc=bankroll,
                        ceiling_cc=combo_thr,
                    )
                    admits = _entity_admits(cert, key, loss_cc, combo_thr)
                    if entity_admission_observer is not None:
                        try:
                            entity_admission_observer(cert, combo_thr, admits)
                        except Exception:  # pragma: no cover - telemetry only
                            pass
                    if limits.entity_admission_armed and admits:
                        continue
                net_note = (
                    ""
                    if cert is None or not limits.entity_admission_armed
                    else (
                        f"; tier {cert.verdict} "
                        f"(prior {cert.prior_cc}cc tier{cert.prior_tier} "
                        f"+ add {cert.add_cc}cc -> tier{cert.post_tier}, "
                        f"net div ${cert.diversifying_cc / 10_000:.2f} vs conc "
                        f"${cert.concentrating_cc / 10_000:.2f}, "
                        f"widen {cert.widen_weight_pct:.1f}%, "
                        f"ceiling {combo_thr}cc)"
                    )
                )
                out.append(
                    Breach(
                        ReasonCode.SKIP_ENTITY_LOSS_CAP,
                        f"entity {key} ACCUMULATED loss {loss_cc}cc "
                        f"(committed+reserved+candidate) > "
                        f"{limits.entity_loss_frac} bankroll = {entity_thr}cc"
                        f"{net_note}",
                        shadow=shadow,
                    )
                )

        # (3d) P1 STAGE-1 STRUCTURE BOUND (operator 2026-08-13 — the whale
        # seam, 6 sightings: single near-coin tickets at 2.9–3.7% of bankroll
        # carrying pennies of EV; top-3 = 82% of the 8/13 −$473). Same
        # accumulation source as (3b) — ``loss_by_combo_cc``: committed +
        # reserved + this check's candidates on ONE combo MARKET, never
        # resting quotes — but bound at its OWN flag-gated fraction (the
        # ratified per-combo ANCHOR, 1%) instead of the live per-combo wall
        # (5%). Deliberate differences from (3b): NO ``total >
        # position.max_loss_cc`` guard (a lone whale candidate must trip it —
        # the accumulation includes the candidate and flow to an over-anchor
        # structure always worsens it: marginal judgment per the 2026-08-01
        # sunk-book constitutional), candidate-key scope inherited (the
        # 2026-07-26 bricking lesson), and the enabled/armed observer split
        # (the slate-partition pattern). NOT waivable: a same-structure
        # re-hit is never a hedge. Breach carries game=None.
        if (
            limits.structure_loss_frac is not None
            and candidates
            and (limits.structure_bound_armed or limits.structure_bound_enabled)
        ):
            structure_thr = threshold_cc(limits.structure_loss_frac, bankroll)
            seen_structure: set[str] = set()
            for position in candidates:
                ticker = position.combo_ticker
                if ticker in seen_structure:
                    continue
                seen_structure.add(ticker)
                total = snapshot.loss_by_combo_cc.get(ticker, 0)
                if total <= structure_thr:
                    continue
                if structure_bound_observer is not None:
                    try:  # pragma: no cover - telemetry only
                        structure_bound_observer(
                            ticker,
                            total,
                            structure_thr,
                            limits.structure_bound_armed,
                        )
                    except Exception:
                        pass
                if limits.structure_bound_armed:
                    out.append(
                        Breach(
                            ReasonCode.SKIP_STRUCTURE_LOSS_CAP,
                            f"structure {ticker} ACCUMULATED loss {total}cc "
                            f"(committed+reserved+candidate) > "
                            f"{limits.structure_loss_frac} bankroll = "
                            f"{structure_thr}cc",
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
        # CANDIDATE-GAME SCOPING (2026-08-14 defect repair — the 2026-08-01
        # sunk-book constitution: only the det-max backstop is a LEVEL gate;
        # the standing book must never refuse flow it does not touch.
        # Live-proven 10:03 ET 2026-08-14 (store decision 111700015): a PURE
        # 7-leg next-day MLS parlay was refused because an MLB game's
        # standing book direction (26AUG141420STLCHC) sat over this wall —
        # 7,213 soccer + 1,249 esports refusals in one day, the exact "8/1
        # freeze shape" check (4b)'s comment below warns about. With
        # candidates present the wall now judges ONLY the games the
        # candidate touches: for an untouched game ``directional_by_game_cc``
        # is purely the sunk book and refusing the candidate cannot lower
        # it. A candidate leg with an UNKNOWN event ticker disables scoping
        # for that check (fail closed — UNKNOWN never narrows a wall).
        # Book-only callers (no candidates: audits, eviction, monitoring)
        # keep the full-book sweep byte-identically. Touched games keep
        # today's exact behaviour (fail-closed: the with-candidate census vs
        # the wall, hedge waivers unchanged) — the mass-acceptance
        # dominance property is per-game and is untouched by scoping.
        cand_dir_games: set[str] | None = None
        if candidates:
            from combomaker.pricing.grouping import game_key

            cand_dir_games = set()
            for position in candidates:
                for leg in position.legs:
                    if not leg.event_ticker:
                        cand_dir_games = None  # UNKNOWN ⇒ full sweep
                        break
                    cand_dir_games.add(game_key(leg.event_ticker))
                if cand_dir_games is None:
                    break
        for game, directional_cc in snapshot.directional_by_game_cc.items():
            if cand_dir_games is not None and game not in cand_dir_games:
                continue
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

        # (4b) P1 STAGE-1 GAME-DIRECTION ACCUMULATED NET (operator
        # 2026-08-13). Binds ``directional_net_by_game_cc`` — the SAME
        # mutex-aware branch-max fold as (4), but over committed + reserved +
        # candidate ONLY (never resting quotes: the reservation chain
        # re-checks at every fill, the loss_by_combo_cc convention) — and
        # judges the MARGINAL against the SUNK committed baseline (the
        # 2026-08-01 constitutional: a book already over the line never
        # blocks flow that doesn't worsen it; drop the baseline early-out and
        # this becomes the 8/1 freeze shape). Candidate-game-scoped (the
        # 7/26 bricking lesson). WAIVABLE per game: a certified hedge always
        # fills (validated vs game_thr — the same documented-deliberate
        # convention as check (4); dossier doctrine re-confirmation owed).
        if (
            limits.game_direction_net_frac is not None
            and candidates
            and (
                limits.game_direction_net_armed
                or limits.game_direction_net_enabled
            )
            and snapshot.directional_net_built
        ):
            from combomaker.pricing.grouping import game_key

            dir_net_thr = threshold_cc(limits.game_direction_net_frac, bankroll)
            candidate_games: set[str] = set()
            for position in candidates:
                for leg in position.legs:
                    if leg.event_ticker:
                        candidate_games.add(game_key(leg.event_ticker))
            for game in sorted(candidate_games):
                net_with = snapshot.directional_net_by_game_cc.get(game, 0)
                if net_with <= dir_net_thr:
                    continue
                # SUNK-BOOK BASELINE: fold the COMMITTED-only census lazily
                # (bounded by the breach rate — the entity lazy-load
                # precedent) with the same public branch-max fold.
                baseline = int(
                    mutex_scenario_bound(
                        snapshot.committed_dir_entries_by_game.get(game, ()),
                        book.is_me_event,
                    )
                )
                if net_with <= baseline:
                    continue  # zero-contribution / pure-hedge candidate passes
                if _waiver_covers(waived_games, game, game_thr):
                    continue  # certified hedges bypass (invariant 11)
                if game_direction_observer is not None:
                    try:  # pragma: no cover - telemetry only
                        game_direction_observer(
                            game,
                            net_with,
                            baseline,
                            dir_net_thr,
                            limits.game_direction_net_armed,
                        )
                    except Exception:
                        pass
                if limits.game_direction_net_armed:
                    out.append(
                        Breach(
                            ReasonCode.SKIP_GAME_DIRECTION_NET_CAP,
                            f"game {game} ACCUMULATED one-direction net "
                            f"{net_with}cc (committed {baseline}cc + "
                            f"reserved/candidate) > "
                            f"{limits.game_direction_net_frac} bankroll = "
                            f"{dir_net_thr}cc",
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
                    # A DIFFERENT certificate type from the entity-axis one
                    # above; separately named so the two can never be confused
                    # (the previous build reused one ``cert`` binding for both).
                    game_cert = waived_games[g]
                    certified_game_loss[g] = min(
                        int(loss_v), int(game_cert.worst_case_cc)
                    )
        slate_loss = self._slate_rollup(
            book, snapshot, candidates, start_time_provider,
            certified_game_loss=certified_game_loss,
        )
        # FIX 2 — THE PARTITION (operator 2026-07-27). ``_slate_rollup`` sums
        # ``worst_case_loss_by_game_cc``, which charges a multi-game parlay's
        # FULL max_loss once PER GAME it touches; 55% of the live book is
        # multi-game (mean 2.38 games/ticket), so the sum read $2,610.28 against
        # $1,358.71 of real premium. The corrected number counts every loss event
        # EXACTLY ONCE and folds each single-game bucket through the SAME
        # Stage-B mutex bound (``partitioned_worst_case_cc``). Computed ONLY when
        # the lever is ENABLED (read-out) or ARMED, and then ONLY for the slates
        # the naive number would refuse — the happy path pays nothing, and a
        # deployment with both flags off pays nothing at all (the snapshot does
        # not even build the once-counted loss events).
        partitioned: dict[str, int] = {}
        if limits.slate_partition_armed or limits.slate_partition_enabled:
            breaching = {s for s, v in slate_loss.items() if v > slate_thr}
            if breaching:
                partitioned = self._slate_partition(
                    book,
                    snapshot,
                    candidates,
                    start_time_provider,
                    limits=limits,
                    only_slates=breaching,
                    certified_game_loss=certified_game_loss,
                    naive_by_slate=slate_loss,
                )
                if slate_partition_observer is not None:
                    for s in sorted(breaching):
                        try:
                            # FAIL-CLOSED IN THE READ-OUT TOO. A slate absent
                            # from the result is one the partition REFUSED to
                            # correct; reporting 0 for it would print
                            # ``would_admit=True`` on the very line the operator
                            # arms from. Report the number that would actually be
                            # ENFORCED — the naive one.
                            slate_partition_observer(
                                s,
                                slate_loss[s],
                                partitioned.get(s, slate_loss[s]),
                                slate_thr,
                            )
                        except Exception:  # pragma: no cover - telemetry only
                            pass
        for slate, loss_cc in slate_loss.items():
            enforced_cc = loss_cc
            note = ""
            if limits.slate_partition_armed and slate in partitioned:
                enforced_cc = partitioned[slate]
                note = f" (naive sum {loss_cc}cc, once-counted joint worst case)"
            if enforced_cc > slate_thr:
                out.append(
                    Breach(
                        ReasonCode.SKIP_SLATE_CAP,
                        f"slate {slate} loss {enforced_cc}cc > "
                        f"{limits.slate_loss_frac} "
                        f"bankroll = {slate_thr}cc{note}",
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
            # KILL-ANCHORED RE-ANCHOR (2026-07-29, ARMING FLAG — default SHADOW
            # ⇒ every threshold below is exactly the one enforced today). See
            # ``RiskLimits.kill_anchored_book_gate``.
            #
            # ARMED, the GOVERNING CONSTRAINT moves onto the two anchors the
            # operator actually ratified: the KILL line ``hard_trip_frac``
            # (0.12 of bankroll) and the budget ``portfolio_kill_tail_prob``
            # (0.02). Nothing here is derived — both are read straight from
            # config. The tail threshold keeps a separate name from
            # ``cvar_thr`` because the ES_0.99 FALLBACK form below
            # deliberately keeps binding on ``portfolio_cvar_frac``: an ES is
            # an average-of-the-worst-1% MAGNITUDE, not a KILL-distance
            # PROBABILITY, and re-anchoring it to 0.12 would refuse
            # essentially every book (it is the fail-closed path, never a
            # free pass).
            tail_thr = (
                threshold_cc(limits.hard_trip_frac, bankroll)
                if limits.kill_anchored_book_gate
                else cvar_thr
            )
            # DET-MAX DEMOTION (operator RATIFICATION 2026-07-31 — "ratify and
            # finish number 2"): with the KILL gate GOVERNING, det-max is
            # demoted to the model-free backstop derived from the ruin anchor
            # (``cap_family.det_max_backstop_frac()`` = 1 - RUIN_FLOOR_FRAC =
            # 0.70 of bankroll — derivation, ratification quote and the
            # measured caveats live on that function; nothing typed here).
            # GUARDED on the tail-probability form actually being armed: if
            # the governing gate cannot run (``portfolio_tail_prob_gate``
            # off), the wall does NOT move — no governor, no demotion, today's
            # 0.36 wall stands. The unusable-snapshot fail-closed branch below
            # is untouched: an unmeasured book still refuses on BOTH axes
            # before any threshold is consulted.
            if limits.kill_anchored_book_gate and limits.portfolio_tail_prob_gate:
                det_max_thr = threshold_cc(det_max_backstop_frac(), bankroll)
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
                # (8a) SAMPLED joint-tail axis. TWO FORMS (operator anchor
                # ratified 2026-07-25): the tail-PROBABILITY form binds
                # P(book loss ≥ the same cvar threshold) ≤
                # portfolio_kill_tail_prob off the snapshot's loss-quantile
                # envelope (worst credible model per quantile; Wilson-upper
                # against MC sampling error) — diversification directly buys
                # capacity while a one-way book stays hard-blocked. Legacy
                # snapshots without the envelope, or the flag off, bind the
                # governing model ES_0.99 exactly as before (never a free
                # pass).
                # ONE implementation of the envelope number (the helper) —
                # kept value-identical with the pre-2026-08-01 inline
                # computation: each grid point carries 1/(len-1) probability
                # mass, POINTS >= thr round the mass UP (conservative), and
                # the Wilson upper bound guards MC sampling error. The
                # lifecycle probes the SAME function to decide whether to
                # build a marginal-candidate input, so cap and probe can
                # never disagree about the regime.
                p_upper = kill_envelope_tail_upper(book_risk, limits, bankroll)
                if p_upper is not None:
                    if p_upper > limits.portfolio_kill_tail_prob:
                        # ── MARGINAL KILL GATE (2026-08-01 sunk-book ruling;
                        # ``RiskLimits.kill_gate_marginal``). The book is OVER
                        # the ratified budget — the LEVEL is inherited/sunk
                        # and no candidate can lower it by being refused. When
                        # the marginal form is armed AND the caller supplied
                        # the candidate's marginal facts, the refusal becomes
                        # the diversity-key admission test (eviction_value.py
                        # machinery, ratified 2026-07-31): admit iff certified
                        # risk-reducing, or marginal tail dES99 <= the EV the
                        # candidate realistically brings (CP-lower P(accept)).
                        # Un-armed, or with no marginal facts (book-only
                        # callers), the level refusal stands — UNKNOWN never
                        # admits.
                        marginal_armed = (
                            limits.kill_gate_marginal
                            and limits.kill_anchored_book_gate
                        )
                        if marginal_armed and kill_marginal is not None:
                            # CORRECTED 2026-08-14 (arming-day incident:
                            # 1,307 quotes / 4 fills; refusals measured
                            # 174,000-292,000x over): the criterion is the
                            # units-correct ``marginal_tail_admit`` — dES99
                            # weighted by the ES tail mass vs unweighted EV,
                            # P(accept) on NEITHER side (it cancels at a
                            # fill-conditional decision; the derivation and
                            # incident live on the function).
                            km = kill_marginal
                            if not (
                                km.certified_risk_reducing
                                or marginal_tail_admit(km.des99_cc, km.ev_cc)
                            ):
                                out.append(
                                    Breach(
                                        ReasonCode.SKIP_PORTFOLIO_CVAR,
                                        f"book over KILL tail budget "
                                        f"(P(book loss >= {tail_thr}cc) = "
                                        f"{p_upper:.4f} (upper) > "
                                        f"{limits.portfolio_kill_tail_prob:.4f})"
                                        f" and candidate marginal tail "
                                        f"{km.des99_cc:.0f}cc x alpha "
                                        f"{ES_TAIL_ALPHA} > ev {km.ev_cc}cc "
                                        f"(p_accept_lower "
                                        f"{km.p_accept_lower:.4f} informational) "
                                        f"(marginal form)",
                                        shadow=shadow,
                                    )
                                )
                        else:
                            anchor_note = (
                                f" (KILL line {limits.hard_trip_frac} bankroll)"
                                if limits.kill_anchored_book_gate
                                else ""
                            )
                            out.append(
                                Breach(
                                    ReasonCode.SKIP_PORTFOLIO_CVAR,
                                    f"P(book loss >= {tail_thr}cc) = "
                                    f"{p_upper:.4f} (upper) > kill tail budget "
                                    f"{limits.portfolio_kill_tail_prob:.4f} "
                                    f"(tail-probability form)"
                                    f"{anchor_note}",
                                    shadow=shadow,
                                )
                            )
                elif book_risk.governing_model_es_99_cc > cvar_thr:
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
                # FIX 3 (2026-07-28): the offsetting-position credit measured on
                # this snapshot. ALWAYS read for the breach detail (shadow
                # visibility); SUBTRACTED only when armed, and only on top of
                # the mutex-aware bound it was measured against.
                hedge_credit_cc = float(
                    getattr(book_risk, "det_max_hedge_credit_cc", 0.0) or 0.0
                )
                det_gate_cc = det_comono_cc
                applied_credit_cc = 0.0
                if limits.portfolio_det_max_mutex_aware and det_mutex_cc is not None:
                    det_gate_cc = min(det_comono_cc, float(det_mutex_cc))
                    if limits.det_max_hedge_credit:
                        applied_credit_cc = max(0.0, min(hedge_credit_cc, det_gate_cc))
                        det_gate_cc = det_gate_cc - applied_credit_cc
                if det_gate_cc > det_max_thr:
                    mutex_note = (
                        f"{int(det_mutex_cc)}cc"
                        if det_mutex_cc is not None
                        else "n/a"
                    )
                    # Name the wall ACTUALLY enforced: the ruin-anchor
                    # backstop when the demotion is in force, else the
                    # MC-positioned fraction (the breach string is how the
                    # live tape is audited — it must never claim a wall that
                    # was not the one tested).
                    demoted = (
                        limits.kill_anchored_book_gate
                        and limits.portfolio_tail_prob_gate
                    )
                    wall_note = (
                        f"{det_max_backstop_frac()} bankroll "
                        f"(ruin-anchor backstop; KILL gate governs)"
                        if demoted
                        else f"{limits.portfolio_det_max_frac} bankroll"
                    )
                    out.append(
                        Breach(
                            ReasonCode.SKIP_PORTFOLIO_DET_MAX,
                            f"portfolio deterministic max loss "
                            f"{int(det_gate_cc)}cc (comonotone "
                            f"{int(det_comono_cc)}cc, mutex-aware {mutex_note}, "
                            f"mutex gating "
                            f"{'on' if limits.portfolio_det_max_mutex_aware else 'off'}, "
                            f"hedge credit {int(hedge_credit_cc)}cc "
                            f"{'applied' if limits.det_max_hedge_credit else 'shadow'}"
                            f") > "
                            f"{wall_note} = "
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
                # ── MARGINAL RUIN GATE (2026-08-01 sunk-book ruling, the
                # ruin axis — ``RiskLimits.ruin_gate_marginal``). The BOOK is
                # over the ruin budget — a LEVEL no refusal can lower (the
                # standing book is sunk; 2026-08-01 evening this branch froze
                # ALL quoting at p_ruin 0.2994 vs 0.05 — 1,044 refusals/5min,
                # sent = 0 — while the pregame flow it was refusing was the
                # measured CURE: three fills that landed from in-flight
                # reservations moved p_ruin 0.2994 → 0.1649 within 90 s).
                # Armed, with the candidate's marginal facts supplied, the
                # refusal becomes the SAME diversity-key admission test the
                # marginal KILL gate rides (one criterion, one machinery —
                # ``KillMarginalCandidate`` built by the lifecycle): admit
                # iff certified risk-reducing, or allocated marginal tail
                # dES99 <= the EV the candidate realistically brings
                # (CP-lower P(accept) at the ratified alpha). Un-armed, or
                # with no marginal facts (book-only/maintenance callers), the
                # level refusal stands byte-identically — UNKNOWN never
                # admits. The det-max backstop above and the unusable-
                # snapshot fail-closed path are untouched.
                if limits.ruin_gate_marginal and kill_marginal is not None:
                    # CORRECTED 2026-08-14 — same units + conditioning
                    # repair as §(8a); derivation on ``marginal_tail_admit``.
                    km = kill_marginal
                    if not (
                        km.certified_risk_reducing
                        or marginal_tail_admit(km.des99_cc, km.ev_cc)
                    ):
                        out.append(
                            Breach(
                                ReasonCode.SKIP_PORTFOLIO_RUIN,
                                f"book over ruin budget (P(ruin) "
                                f"{book_risk.p_ruin:.4f} (upper "
                                f"{gated_ruin:.4f}) > {ruin_budget:.4f}) "
                                f"and candidate marginal tail "
                                f"{km.des99_cc:.0f}cc x alpha "
                                f"{ES_TAIL_ALPHA} > ev {km.ev_cc}cc "
                                f"(p_accept_lower {km.p_accept_lower:.4f} "
                                f"informational) (marginal form)",
                                shadow=shadow,
                            )
                        )
                else:
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

    def _earliest_start_by_game(
        self,
        book: ExposureBook,
        candidates: list[OpenPosition],
        start_time_provider: StartTimeProvider | None,
    ) -> dict[str, datetime | None]:
        """Earliest known leg start per game — the slate bucket's key input.

        Extracted VERBATIM from ``_slate_rollup`` (2026-07-27) so the corrected
        partition aggregation buckets games through the EXACT same code, never a
        second implementation that could drift. Walks the legs of every book
        position AND every candidate (the hypothetical fills the snapshot already
        folded in under mass acceptance) AND every open quote (folded into the
        loss aggregate too, so a quote-driven game buckets correctly).
        """
        from combomaker.pricing.grouping import game_key

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
        return earliest_start

    def _slate_partition(
        self,
        book: ExposureBook,
        snapshot: object,
        candidates: list[OpenPosition],
        start_time_provider: StartTimeProvider | None,
        *,
        limits: RiskLimits,
        only_slates: set[str],
        certified_game_loss: dict[str, int] | None = None,
        naive_by_slate: Mapping[str, int] | None = None,
    ) -> dict[str, int]:
        """The slate's ENUMERATED JOINT WORST CASE — every loss event counted ONCE.

        FIX 2 (operator 2026-07-27: "stop summing losses that cannot all occur …
        compute the exposure CORRECTLY"). ``_slate_rollup`` sums
        ``worst_case_loss_by_game_cc``; a parlay spanning G games contributes its
        FULL max_loss G times. Measured on the live book: $2,610.28 against
        $1,358.71 of real premium (1.92x), and **99.5% of that overstatement is
        the multi-game double count, not missing mutex credit** (the within-game
        mutex fold recovers $5.87 of $1,251.58). So the repair is the PARTITION,
        using the machinery that already exists — no second concept, no
        exemption, and the ratified ``slate_loss_frac`` is not touched.

        THE AGGREGATION, per slate S:
          * a loss event is assigned to S iff it touches ANY game in S (a ticket
            spanning two slates is charged IN FULL to BOTH — each slate cap is a
            separate constraint and the ticket can lose within either window —
            but exactly ONCE WITHIN each; only the within-slate duplication was
            the bug);
          * within S it lands in EXACTLY ONE bucket: its single game when every
            leg lives in one game, else the comonotone residual;
          * each game bucket folds through the existing Stage-B
            ``_mutex_game_worst_cc`` (single explicit-ME-event max-over-branches,
            fail-closed to comonotone on 0 or >=2 ME events);
          * the total is CLAMPED into [largest single loss, once-counted
            comonotone sum] — the sum counted once IS the all-lose bound, so
            nothing above it is a worst case.

        RESTING QUOTES keep the operator's approved 40% burst-floored haircut:
        the base fold (committed + candidates) and the full fold (base + resting)
        are composed by the SAME ``_haircut_compose_cc`` the per-game axis uses,
        with the CONSERVATIVE comonotone base as the floor term. Unarmed haircut
        (weight 1) ⇒ the full fold, exactly as the per-game axis behaves.

        FAIL-CLOSED. An UNGAMED loss event (no identifiable game on any leg) is
        pooled into the conservative ``UNKNOWN_SLATE_KEY`` residual rather than
        dropped — today's roll-up cannot see it at all. A slate whose corrected
        number cannot be built is simply absent from the result, and the caller
        then enforces the naive number (the larger one).

        THE THREE FAIL-OPENS THIS FUNCTION MUST NOT HAVE, and how each is shut.
        Every one of them is the same species: the corrected number is built from
        a CENSUS (``snapshot.loss_units``), an empty census folds to ZERO, and
        zero is the PERMISSIVE answer on a cap — so an absent census would make
        every slate breach silently disappear (quiet-failure defense #2, and hard
        rule 6: missing data ⇒ no-quote, never a convenient default).

          1. **CENSUS NEVER TAKEN.** ``snapshot()`` only builds ``loss_units``
             when asked (``want_loss_units``), and today the ONE caller derives
             that flag from the SAME two booleans that reach here — with no
             assertion, and the permissive answer as the default if they ever
             diverge. ``loss_units_built`` makes the state EXPLICIT rather than
             inferred from emptiness: not built ⇒ return ``{}`` ⇒ the naive
             number enforces. A future caller (a test, a replay harness, a
             second call site) can no longer open the cap by forgetting a kwarg.
          2. **CENSUS TAKEN BUT EMPTY FOR A BREACHING SLATE.** The naive
             roll-up says this slate carries loss; a once-counted census that
             sees NOTHING there contradicts it, which means the two views
             disagree about the book. ``naive_by_slate`` lets us detect exactly
             that and omit the slate ⇒ the naive number enforces.
          3. **CORRECTED NUMBER ABOVE THE NAIVE ONE.** Impossible by
             construction (the partition can only remove duplicate copies of one
             event), so if it happens the census is not the book the roll-up
             measured. ``min`` with the naive term keeps the enforced number the
             LARGER-of-the-two-that-can-be-trusted, never a raised cap.
        """
        from combomaker.risk.exposure import ExposureSnapshot

        assert isinstance(snapshot, ExposureSnapshot)

        # FAIL-OPEN #1 — the census was never taken. An empty ``loss_units`` is
        # indistinguishable from "the book is empty" without this flag, and the
        # empty reading is the one that ADMITS everything.
        if not snapshot.loss_units_built:
            return {}

        earliest_start = self._earliest_start_by_game(
            book, candidates, start_time_provider
        )

        # Slate key per game, resolved ONCE (``slate_key_for_start`` does a
        # timezone conversion; the folds below would otherwise re-do it per unit
        # per game per slate).
        slate_by_game = {
            g: slate_key_for_start(s) for g, s in earliest_start.items()
        }

        def slate_of(game: str) -> str:
            key = slate_by_game.get(game)
            if key is None:
                key = slate_key_for_start(earliest_start.get(game))
                slate_by_game[game] = key
            return key

        # Certified games tighten a bucket exactly as they tighten the naive
        # term: min() only, so a certificate can never RAISE the number.
        certified = certified_game_loss or {}

        # Each unit paired with its slate SET, resolved ONCE for every fold.
        # Ungamed ⇒ only the conservative UNKNOWN pool sees it (today's roll-up
        # cannot see it at all).
        tagged: list[tuple[LossUnit, frozenset[str]]] = [
            (
                u,
                frozenset(slate_of(g) for g, _ in u.legs_by_game)
                if u.legs_by_game
                else frozenset((UNKNOWN_SLATE_KEY,)),
            )
            for u in snapshot.loss_units
        ]
        base_tagged = [(u, s) for u, s in tagged if not u.resting]

        def fold(us: list[tuple[LossUnit, frozenset[str]]], slate: str) -> int:
            return partitioned_worst_case_cc(
                [u for u, slates in us if slate in slates],
                book.is_me_event,
                certified,
            )

        haircut = limits.resting_quote_weight < 1
        out: dict[str, int] = {}
        for slate in only_slates:
            # FAIL-OPEN #2 — the census sees NOTHING in a slate the naive
            # roll-up says is over the wall. The two views disagree about the
            # book; omit the slate so the naive number enforces.
            if not any(slate in slates for _u, slates in tagged):
                continue
            full = fold(tagged, slate)
            if not haircut:
                out[slate] = full
                continue
            base = fold(base_tagged, slate)
            resting_losses = [
                u.loss_cc
                for u, slates in tagged
                if u.resting and slate in slates
            ]
            topk = _topk_sum_int(resting_losses, max(0, limits.resting_floor_count))
            # Floor base term: the COMONOTONE base (each event once). Always >=
            # the netted base fold, so the burst floor can only be raised by it —
            # the conservative choice, and it never depends on which netting
            # regime the combined census happens to land in.
            floor_base = sum(
                u.loss_cc for u, slates in base_tagged if slate in slates
            )
            out[slate] = min(
                full,
                _haircut_compose_cc(
                    base,
                    full,
                    topk,
                    limits.resting_quote_weight.numerator,
                    limits.resting_quote_weight.denominator,
                    floor_base=floor_base,
                ),
            )
        # FAIL-OPEN #3 — a corrected number ABOVE the naive one is impossible
        # (the partition only ever removes duplicate copies of one loss event),
        # so it means the census is not the book the roll-up measured. Clamp to
        # the naive term: the enforced number can never be raised by this
        # function, and it can never be lowered by a census we cannot trust.
        if naive_by_slate is not None:
            for slate, value in list(out.items()):
                naive = naive_by_slate.get(slate)
                if naive is not None and value > int(naive):
                    out[slate] = int(naive)
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
        from combomaker.risk.exposure import ExposureSnapshot

        assert isinstance(snapshot, ExposureSnapshot)

        earliest_start = self._earliest_start_by_game(
            book, candidates, start_time_provider
        )

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
