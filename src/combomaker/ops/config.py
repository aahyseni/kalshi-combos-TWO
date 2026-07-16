"""Configuration: pydantic-validated YAML per environment; secrets via env only.

The production guard is hardcoded here, not in YAML: quoting on prod requires
BOTH the explicit ``--confirm-live`` CLI flag AND ``prod_limits_configured:
true`` in the prod config file. Demo is the default environment everywhere.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from combomaker.risk.limits import RiskLimits

if TYPE_CHECKING:
    from combomaker.risk.breakers import BreakerThresholds


class Env(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class Mode(StrEnum):
    OBSERVE = "observe"  # log RFQ flow + would-quotes; sends nothing
    PAPER = "paper"      # full pipeline incl. risk, records hypothetical quotes; sends nothing
    QUOTE = "quote"      # sends real quotes


# Doc-verified base URLs (docs/api-notes/auth-env.md). Demo is .co, prod .com.
_ENDPOINTS: dict[Env, tuple[str, str]] = {
    Env.DEMO: (
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
    Env.PROD: (
        "https://external-api.kalshi.com/trade-api/v2",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    ),
}


class StrictModel(BaseModel):
    """Reject unknown keys: a typo in a limit name must fail loudly, not silently."""

    model_config = ConfigDict(extra="forbid")


class EndpointsConfig(StrictModel):
    rest_base_url: str
    ws_url: str

    @classmethod
    def for_env(cls, env: Env) -> Self:
        rest, ws = _ENDPOINTS[env]
        return cls(rest_base_url=rest, ws_url=ws)


class LoggingConfig(StrictModel):
    json_output: bool = True
    level: str = "INFO"


class SafetyConfig(StrictModel):
    # Set true in prod.yaml only after limits have been reviewed by the human.
    prod_limits_configured: bool = False
    # Phase 6 go-live gate: prod requires a NON-EMPTY leg-series allowlist
    # (``filters.allowed_leg_series_prefixes``) so only whitelisted series quote
    # on real money. Defaults ON — an operator must deliberately set it false to
    # weaken it, and even then the empty-collection-whitelist gate still binds.
    # This is a distinct gate from ``prod_limits_configured``: limits bound how
    # MUCH we risk; the whitelist bounds WHAT we quote (no crypto/esports/
    # unmodeled legs on prod, per judge finding F1).
    prod_require_series_whitelist: bool = True
    # Phase 6: prod requires an external-supervisor heartbeat to be established
    # AND the external kill path reachable before the first quote (the preflight
    # checks both). Defaults ON. An operator can only disable it deliberately;
    # everything ships NOT-LIVE, so prod stays off regardless.
    prod_require_supervisor: bool = True
    # P0-5 (exact exchange-quantity reconciliation): the ONE subaccount all
    # account endpoints (positions, and by extension order placement) are pinned
    # to. Kalshi's GET /portfolio/positions takes a ``subaccount`` query param
    # that DEFAULTS to 0 (=primary); 1–63 are numbered subaccounts
    # (docs/api-notes/index-scan.md §portfolio). We pin every positions read to
    # this value so the exposure book reconciles to EXACTLY the exchange's
    # quantity/side FOR THE ACCOUNT WE TRADE ON — a position held under a
    # different subaccount must never leak into (nor be missing from) this book.
    # Default 0 matches the exchange default and the single-account posture; an
    # operator only sets this if the bot trades under a numbered subaccount.
    subaccount: int = 0

    @field_validator("subaccount")
    @classmethod
    def _valid_subaccount(cls, v: int) -> int:
        # 0 = primary; 1–63 = numbered subaccounts (Kalshi caps at 64 total).
        if not (0 <= v <= 63):
            raise ValueError(f"subaccount must be in 0..63 (0=primary), got {v}")
        return v


class SupervisorConfig(StrictModel):
    """External safety supervisor knobs (Phase 6; ops/supervisor.py). The
    supervisor is a SEPARATE process — these values are read both by the bot (to
    size its heartbeat cadence) and by the standalone ``python -m
    combomaker.ops.supervisor`` entry point. Defaults are the conservative
    first-live posture; the report tables them."""

    # The bot must beat within this window or the supervisor presumes it wedged.
    heartbeat_timeout_s: float = 15.0
    # How often the supervisor polls the heartbeat file.
    poll_interval_s: float = 1.0
    # Reserved API write budget: tokens the supervisor may spend per window on
    # its OWN cancels, so it can always act under a 429 storm. Sized well above a
    # realistic resting-quote count (max_open_quotes default 20).
    write_budget_capacity: int = 200
    write_budget_refill_s: float = 10.0

    @field_validator("heartbeat_timeout_s", "poll_interval_s", "write_budget_refill_s")
    @classmethod
    def _positive_seconds(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"must be > 0, got {v}")
        return v

    @field_validator("write_budget_capacity")
    @classmethod
    def _positive_capacity(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"write_budget_capacity must be >= 1, got {v}")
        return v


class BreakerConfig(StrictModel):
    """Circuit-breaker thresholds (Phase 6; risk/breakers.py). Every breaker is
    fail-closed; these are the trip levels. Conservative for the shadow/first-live
    posture — the report tables them with reason codes."""

    max_rx_age_s: float = 5.0             # feed staleness (HALT_DATA_STALE)
    max_latency_ms: float = 2_000.0       # confirm/round-trip (HALT_LATENCY_SPIKE)
    # The latency-spike breaker judges the worst round-trip in THIS trailing
    # window, not the all-time max — one historical slow confirm must not latch
    # the human-only kill switch forever. A spike self-clears once no slow
    # sample has landed within the window.
    latency_spike_window_s: float = 60.0  # recent-window for HALT_LATENCY_SPIKE
    rate_limit_window_s: float = 10.0     # 429-burst window (HALT_RATE_LIMIT_BURST)
    max_rate_limit_in_window: int = 10    # 429s at/over ⇒ burst
    max_marginal_jump: float = 0.25       # prob jump between ticks (HALT_MARGINAL_JUMP)

    @field_validator(
        "max_rx_age_s",
        "max_latency_ms",
        "latency_spike_window_s",
        "rate_limit_window_s",
        "max_marginal_jump",
    )
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"must be > 0, got {v}")
        return v

    @field_validator("max_rate_limit_in_window")
    @classmethod
    def _positive_count(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_rate_limit_in_window must be >= 1, got {v}")
        return v

    def to_thresholds(self) -> BreakerThresholds:
        from combomaker.risk.breakers import BreakerThresholds

        return BreakerThresholds(
            max_rx_age_s=self.max_rx_age_s,
            max_latency_ms=self.max_latency_ms,
            latency_spike_window_s=self.latency_spike_window_s,
            rate_limit_window_s=self.rate_limit_window_s,
            max_rate_limit_in_window=self.max_rate_limit_in_window,
            max_marginal_jump=self.max_marginal_jump,
        )


class FiltersConfig(StrictModel):
    """Quote/no-quote gates. Every rejection carries a ReasonCode and is logged."""

    # Whitelist of mve_collection_ticker prefixes. Empty = observe everything
    # (observe/paper only — quote mode refuses to run with an empty whitelist).
    collection_whitelist: list[str] = []
    # LEG-SERIES allowlist (operator directive 2026-07-11, judge finding F1:
    # collections mix sports, so the collection whitelist alone admits
    # crypto/esports/unmodeled-league legs that classify UNKNOWN and price at
    # flat priors instead of declining). Every leg's market ticker must start
    # with one of these prefixes or the RFQ declines SKIP_SERIES_NOT_ALLOWED.
    # UNBLOCK a new sport/competition by adding its series prefix (per-env
    # YAML) once its classification + priors exist; null disables the gate;
    # empty list blocks ALL combos (fail-closed).
    allowed_leg_series_prefixes: list[str] | None = ["KXWC", "KXMLB"]
    combos_only: bool = True          # skip single-market RFQs
    # Quote-time feed-freshness gate: refuse to POST a quote when the WS feed's
    # rx-age exceeds this. Keep it >= breakers.max_rx_age_s (5s) so that a feed
    # stale enough for the HALT_DATA_STALE breaker to be (transiently) HOLDING can
    # never produce a live quote on stale prices — closes the gap between "feed
    # connected (feed_healthy, <=30s)" and "fresh enough to price" (review finding
    # 2026-07-13). rx-age None (disconnected) is already caught by feed_healthy.
    max_feed_age_s: float = 5.0
    min_legs: int = 2
    max_legs: int = 6
    min_contracts: float = 1.0        # whole contracts
    max_contracts: float = 10_000.0
    min_target_cost_dollars: float = 1.0
    max_target_cost_dollars: float = 50_000.0
    max_leg_spread_cc: int = 800      # widest acceptable leg spread ($0.08)
    min_leg_depth_contracts: float = 1.0   # min size behind BOTH best bids
    min_time_to_close_s: float = 3600.0    # pregame-only default (1h before close)
    # Decline combos that carry a two-legged-tie knockout leg (UCL/UEL/UECL):
    # "advance" there is decided over two legs, so a single-match win does NOT
    # imply advancing and the single-match soccer priors mis-apply. Gated off
    # until its own regime is built. Flip to false once that regime exists.
    decline_two_legged_tie: bool = True

    # --- Pregame-only quote gate (Phase 3, operator directive 2026-07-10) ---
    # "Never quote a combo where a leg is currently in play — ALL sports."
    # Schedule-based: each leg's game START time comes from a fail-closed
    # chain — (a) verified ticker-embedded start (KXMLB*, US/Eastern HHMM
    # token, API-verified 2026-07-10), (b) earliest(close_time,
    # expected_expiration_time) minus the offset below, (c) UNKNOWN ⇒ decline
    # (skip_start_time_unknown). Any leg with now >= start declines the RFQ
    # (skip_inplay_leg) and is re-checked at last look (decline_inplay_leg).
    # Flip allow_inplay_legs to true to re-enable in-play quoting later
    # WITHOUT code changes; the market-motion detector (risk/inplay.py) and
    # min_time_to_close_s stay active regardless. See rfq/pregame.py.
    allow_inplay_legs: bool = False
    # Estimate offset (hours) for chain (b). The LIVE-GATE default is 4.5h —
    # deliberately larger than the backtest harnesses' soccer 2.5h: measured
    # 2026-07-10 on real WC markets, expected_expiration lands 2.95-3.95h
    # after kickoff depending on series, so 2.5h would admit ~1.5h of
    # in-play. Conservative side: too-large only costs late-pregame quoting,
    # too-small quotes in-play.
    pregame_start_offset_hours: float = 4.5
    # Per-series overrides (ticker prefix -> hours). MLB 4.0 is the
    # mlb_backtest-validated estimate (fallback only — the embedded ET start
    # normally wins for KXMLB*; API-measured expected_expiration = start+3h,
    # so 4.0 lands 1h before first pitch).
    pregame_start_offset_hours_by_prefix: dict[str, float] = {"KXMLB": 4.0}
    # MAX pregame horizon per prefix (hours): decline a combo whose leg game is
    # more than this far out. Far-out Kalshi leg books are UNINFORMED and diverge
    # from the sharp consensus, so pricing off them invites adverse selection
    # (2026-07-14: an MLB leg priced 41% vs 58% true, ~3 days out, got picked off).
    # Empty default = no horizon limit; the armed config sets KXMLB: 24.0.
    max_pregame_hours_by_prefix: dict[str, float] = {}

    # --- Precision ladder margins (Phase 5, R3 Part B) -----------------------
    # Split the single start buffer into TWO margins applied to a PRECISE start
    # (from the embedded-ET path or the explicit schedule feed — NOT the blunt
    # expiry-minus-offset estimate, which already bakes in its own conservative
    # padding):
    #   - quote-cutoff margin M_q — stop QUOTING M_q seconds before start. The
    #     flow knob: with a verified precise start it can be small (recover the
    #     last ~1.5h the 4.5h estimate forgoes). CONSERVATIVE DEFAULT this phase.
    #   - confirm-cutoff margin M_c (M_c >= M_q) — the SAFETY knob, applied at
    #     LAST LOOK (decline if now >= start − M_c). Keeps the confirm side
    #     strict even when the quote side is tightened for flow.
    # SEAM + CONSERVATIVE DEFAULTS only: no live tightening without a hard-rule-5
    # verified feed (deferred). The defaults keep today's behaviour — the
    # embedded-ET path currently pairs with a 0s M_q/M_c (the estimate's padding
    # is the buffer), so quoting/confirm cutoffs are unchanged until an operator
    # sets per-prefix margins backed by a measured pooled markout study.
    pregame_quote_margin_s: float = 0.0
    pregame_confirm_margin_s: float = 0.0
    pregame_quote_margin_s_by_prefix: dict[str, float] = {}
    pregame_confirm_margin_s_by_prefix: dict[str, float] = {}

    @field_validator("pregame_confirm_margin_s")
    @classmethod
    def _confirm_ge_quote(cls, v: float, info: ValidationInfo) -> float:
        # M_c >= M_q (the confirm side is never looser than the quote side).
        q = info.data.get("pregame_quote_margin_s")
        if q is not None and v < q:
            raise ValueError(
                f"pregame_confirm_margin_s ({v}) must be >= "
                f"pregame_quote_margin_s ({q}) — confirm never looser than quote"
            )
        return v

    @model_validator(mode="after")
    def _confirm_ge_quote_by_prefix(self) -> Self:
        # The scalar validator above only guards the DEFAULT margins. The
        # per-prefix tables must obey the SAME fail-closed invariant, or an
        # operator could configure a prefix whose confirm cutoff (the last-look
        # SAFETY gate) is LOOSER than its quote cutoff — the exact inversion the
        # invariant exists to prevent. This includes the mixed case where only
        # ONE table names a prefix (the other side falls back to its scalar
        # default). For EVERY prefix appearing in EITHER table, resolve the
        # effective quote margin (prefix override, else scalar) and confirm
        # margin (likewise) and reject if the effective confirm is looser.
        # Resolve effective margins the SAME WAY the live gate does — via
        # rfq.pregame._prefix_lookup (startswith, first-insertion-order match) —
        # NOT by exact dict key. Exact-key resolution let an operator pass
        # validation with OVERLAPPING prefixes (e.g. {"KXMLB", "KXMLBGAME"} in
        # differing insertion order) that invert M_c >= M_q at RUNTIME. Lazy
        # import breaks the config <-> pregame cycle; reusing the real resolver
        # means the validator can never drift from the gate it guards.
        from combomaker.rfq.pregame import _prefix_lookup

        prefixes = (
            self.pregame_quote_margin_s_by_prefix.keys()
            | self.pregame_confirm_margin_s_by_prefix.keys()
        )
        for prefix in sorted(prefixes):
            # Feed the prefix itself as a representative ticker: a ticker that IS
            # (or starts with) this prefix resolves to the first table entry it
            # startswith — exactly what the live gate computes for such a ticker.
            q_eff = _prefix_lookup(
                prefix, self.pregame_quote_margin_s_by_prefix, self.pregame_quote_margin_s
            )
            c_eff = _prefix_lookup(
                prefix, self.pregame_confirm_margin_s_by_prefix, self.pregame_confirm_margin_s
            )
            if c_eff < q_eff:
                raise ValueError(
                    f"pregame_confirm_margin_s for prefix {prefix!r} ({c_eff}) "
                    f"must be >= the effective quote margin ({q_eff}) — confirm "
                    "never looser than quote (per-prefix)"
                )
        return self


class FeeConfig(StrictModel):
    """Coefficients as decimal strings (exact Fractions downstream). VERIFIED
    against the official Kalshi fee-schedule PDF (effective 2026-06-29): taker
    0.07, maker 0.0175, quadratic. Maker fee applies only on markets in Kalshi's
    maker-fee list (quadratic combo series charge $0 maker); monitor changes via
    GET /series/fee_changes, and still reconcile vs real fills (defense #3)."""

    taker_coef: str = "0.07"
    maker_coef: str = "0.0175"
    # fee_type per combo series until fetched from GET /series: conservative
    # default; overrides keyed by series/collection ticker prefix.
    default_fee_type: str = "quadratic"
    default_multiplier: str = "1.0"


class CorrelationConfig(StrictModel):
    """Conservative priors; empirical calibration (with sample-size gates)
    updates these, never code."""

    same_event_rho: float = 0.6
    cross_event_rho: float = 0.0
    rho_uncertainty: float = 0.25
    # SGP structure: signed YES-YES priors per typed pair (legtypes.pair_key).
    # CALIBRATED 2026-07-06 from 8,982 matches (top-5 EU leagues 20/21-24/25,
    # tools/calibrate_pairs_from_history.py — implied rho solved through OUR
    # copula). Notable corrections vs the original hand priors: btts|moneyline
    # flipped SIGN (+0.05 → −0.17: decisive wins are clean-sheet-ish); both
    # corners pairs measured ≈ 0 (folk wisdom busted). player_goal and extras
    # pairs remain hand priors (no match-level data) with the wide band.
    pair_rho: dict[str, float] = {
        "moneyline|moneyline": -0.95,      # measured −0.99: P(both win) = 0 exactly
        "btts|total": 0.75,                # measured +0.746, n=8982
        "player_goal|total": 0.40,         # hand prior (uncalibrated)
        "btts|player_goal": 0.35,          # hand prior (uncalibrated)
        # Structurally implied ~+0.51 in BOTH scoreline-inversion worked
        # examples (fav ENG/Kane and dog POR/Ronaldo, 2026-07-06) — a star
        # scorer's goals ARE his team's goals. Global kept below the soccer
        # value (other sports unmeasured), wide band.
        "moneyline|player_goal": 0.40,
        "btts|moneyline": -0.17,           # measured −0.20/−0.14 (home/away pooled)
        "moneyline|total": 0.23,           # measured +0.28/+0.18 (side asymmetry)
        "total|total": 0.95,               # nested thresholds: measured at the cap
        "corners|total": 0.00,             # measured −0.04 ≈ 0
        "btts|corners": 0.00,              # measured +0.01 ≈ 0
        "extras|total": 0.50,              # hand prior (MLB, uncalibrated)
    }
    typed_rho_uncertainty: float = 0.15
    untyped_rho_uncertainty: float = 0.30
    # Sport-specific tables (calibrated 2026-07-06; the same pair correlates
    # DIFFERENTLY per sport: winner×over is +0.23 in soccer, 0.00 in NFL/NBA).
    # Sports absent here fall back to the global table above with wider bands.
    # WNBA inherits NBA's numbers (transfer assumption, wider band).
    pair_rho_by_sport: dict[str, dict[str, float]] = {
        # Soccer: moneyline|total is CONDITIONAL-MLE on per-game closing-line
        # marginals (directive-compliant; club n=7,228 train, rho +0.30 SE
        # 0.019, BEATS independence out-of-sample on held-out 23/24+24/25) —
        # band widened for home/away asymmetry + internationals. btts pairs
        # remain POOLED-method (no closing BTTS odds in the dataset) with
        # widened bands until a conditional/structural refit; btts|ml at least
        # measured identically in club and international data (−0.197 both).
        # btts|moneyline is ORIENTATION-CONDITIONAL (":fav"/":dog" resolved by
        # the ML leg's YES-side marginal, blended across 45–55c): the −0.19
        # calibration pooled favorites and dogs, but "winners keep clean
        # sheets" is a favorites-only effect — a dog can only win by scoring,
        # so dog-win×btts is ~0 (scoreline-model implied +0.04 on the live
        # SPA/POR example that the market priced at exactly our structural
        # fair). Plain entry retained for marginal-less callers.
        # moneyline|player_goal: structurally implied +0.51/+0.52 on both
        # worked examples (fav and dog) — orientation-insensitive, so a single
        # entry; replaces the 0.25 hand prior that made us auto-lose striker
        # SGP auctions.
        "soccer": {
            "moneyline|total": 0.28,
            "btts|total": 0.70,
            # btts|moneyline is now a WIN-PROB CURVE (oriented_curve below), not
            # two flat fav/dog plateaus: the residual btts<->win rho is a monotone
            # function of the ML leg's win-prob (RE-MEASURED 2026-07-07, 8,982
            # matches — heavy longshot ~0, deepening to ~-0.36 for heavy favorites).
            # The curve WINS whenever leg marginals are available; these plain /
            # :fav / :dog entries are the marginal-less fallback (and the -0.19
            # pooled value the curve reduces to at a mid-strength dog).
            "btts|moneyline": -0.19,
            "btts|moneyline:fav": -0.19,
            "btts|moneyline:dog": 0.00,
            "moneyline|player_goal": 0.50,
            # RE-MEASURED 2026-07-07 (Understat 3,652 matches, orientation-balanced
            # top-xG scorer per team): implied rho +0.549, so 0.45->0.55. Must
            # EXCEED player_goal|total (0.46) and it does. Strongly orientation-
            # dependent (dog-scorer +0.81, fav-scorer +0.31) but there is no ML leg
            # to orient on, so a single 0.55 ships with a wide band (0.30) spanning
            # the fav/dog range.
            "btts|player_goal": 0.55,
            "player_goal|total": 0.46,       # measured +0.46 (Understat 3,652); was global 0.40
            "player_goal|player_goal": 0.03,  # teammates ~0 (Poisson-split, exact) / opp +0.05
            "total|total": 0.95,
            # TOTAL corners (KXWCCORNERS) — measured ⊥ goals AND ⊥ result.
            "corners|total": 0.00,
            "corners|moneyline": 0.00,
            "corners|spread": 0.00,       # TOTAL corners ⊥ margin (measured +0.02)
            "btts|corners": 0.00,
            # CORNERS × FIRST-HALF markets — MEASURED (M1 2026-07-12, job
            # 24844262 tmp/zerogaps/soccer_wire_list.txt; football-data top-5
            # EU 20/21-24/25 n=8,981, implied-rho thru the shipped copula;
            # match-cluster bootstrap CI95 hw <= 0.08). TOTAL corners stay ~0
            # against every 1H family (the 2026-07-08 near-zero priors now
            # measured): 1H-winner ~0 (:team +0.013 / :tie -0.023), 1H-total
            # -0.02 (centers over0.5 +0.010 / over1.5 -0.046), 1H-btts -0.01,
            # 1H-spread -0.05 (a 1H margin>=2 kills the late corners chase).
            "corners|first_half_moneyline": 0.00,
            "corners|first_half_total": -0.02,
            "corners|first_half_btts": -0.01,
            "corners|first_half_spread": -0.05,
            # TEAM corners × 1H-winner/1H-spread are ORIENTED (:same/:opp/
            # :tie), MEASURED strength-controlled (raw pooled is the Simpson
            # trap, same as corners_team|moneyline): the chasing team earns
            # corners, 1H stronger than FT (:same -0.204 vs FT -0.15). Routed
            # by the corners_team|moneyline / |spread resolvers (sgp.py — the
            # 1H suffixes are the same TEAM-code / TEAM+digits shapes); the
            # plain entries are the unparseable-orientation fallback, bands
            # RAISED to span the measured oriented extremes.
            "corners_team|first_half_moneyline": 0.00,
            "corners_team|first_half_moneyline:same": -0.20,
            "corners_team|first_half_moneyline:opp": 0.23,
            "corners_team|first_half_moneyline:tie": 0.00,
            "corners_team|first_half_total": 0.00,   # M1: o0.5 +0.010 / o1.5 -0.021
            "corners_team|first_half_btts": 0.00,    # M1: -0.002 ~0
            "corners_team|first_half_spread": 0.00,
            "corners_team|first_half_spread:same": -0.18,
            "corners_team|first_half_spread:opp": 0.15,
            # advance|corners (M1 2026-07-12): DERIVED+MEASURED ET channel —
            # pooled 0.00 by identity, but a STRENGTH CURVE dog +0.23 <-> fav
            # -0.23 (a drawn-90 forces ET; corners settle INCLUDING ET, so a
            # dog's advance co-occurs with the extra corners window). The
            # curve ships in oriented_curve["soccer:advance|corners"] keyed on
            # the ADVANCE leg's marginal (btts|moneyline machinery); this
            # plain scalar is the marginal-less fallback and must band-span
            # the curve (band 0.25; q-sweep +-0.04, ET-pmf unc +-0.01).
            "advance|corners": 0.00,
            # corners|player_goal (M1 2026-07-12): MEASURED on the Understat x
            # football-data join (n=3,614 matches / 7,228 star rows, 99.0%
            # join) — club strength-controlled -0.054, raw -0.042; KO-ET
            # adjusted -0.010 (goal+corners both settle incl ET; kick
            # +0.02..0.03; 87.4% of tape flow is knockout) -> -0.03 centers
            # the regimes. Replaces the +0.05 labeled prior.
            "corners|player_goal": -0.03,
            # TOTAL corners × a TEAM's corners (same game). Unlike the other
            # corners pairs (measured ~0), the total CONTAINS the team's corners
            # as a large component, so they are strongly comonotone — the one
            # pair where the old +0.6 fallback POINT was accidentally close, but
            # its fail-safe band spanned zero (treating a structurally-certain-
            # positive pair as maybe-negative → over-wide quotes on corners-heavy
            # combos; RFQ test C24/C25/C26). MEASURED 2026-07-08 on 8,981 matches
            # by two independent passes (football-data HC/AC; total=HC+AC): copula
            # rho +0.61-0.66 at real tape lines (KXWCCORNERS 7-10 × KXWCTCORNERS
            # 4-6), home +0.65 / away +0.57 (home corners a bigger share of the
            # total). No :same/:opp — corners_team carries no home/away orient, so
            # a single home/away blend ships with a wide band spanning the split.
            "corners|corners_team": 0.62,
            # ADVANCE (knockout progress) × full-time markets. ADVANCE was
            # UNLISTED against total/btts/scorer/spread → fell to the flat +0.6
            # fallback (6× too high on totals, WRONG SIGN on btts/opp-scorer),
            # and advance is the SINGLE most common soccer leg (1.02M tape legs).
            # DERIVED 2026-07-07 from our own Dixon-Coles model (advance = win90 +
            # ET + 0.5 shootout) and CROSS-CHECKED against 4 historical-knockout
            # studies (WC/Euros n=247, Copa/AFCON/Asian/Gold n=185, UCL n=310,
            # UEL/UECL n=78): the DC's implied k=P(advance|reg-draw) 0.50(even)→
            # 0.64(fav) MATCHES the measured k, and the attenuation matches too.
            # advance is a moneyline attenuated by the ~35% of ties that go to a
            # scoreline-decoupled shootout: SYMMETRIC goal markets (total, btts)
            # retain ~½ (btts stays NEGATIVE like btts|moneyline); DIRECTIONAL
            # markets (scorer, spread) retain ~0.8. LINE-STABLE across total lines
            # incl. over-0.5 (advance, unlike a win, does NOT imply a goal — a 0-0
            # shootout advances). player_goal flips sign on team orientation
            # (scorer on the advancing team vs the opponent) — resolved in sgp.py
            # to :same/:opp. spread≥2 ⟹ win ⟹ advance is a near-containment
            # (Kalshi blocks the conflict anyway). NOTE: this is the SINGLE-MATCH
            # regime (KXWC*); two-legged UCL/UEL/UECL is a DIFFERENT regime
            # (symmetric→0) — see memory, wire by ticker series when it lists.
            "advance|total": 0.12,
            "advance|btts": -0.07,
            "advance|player_goal:same": 0.45,
            "advance|player_goal:opp": -0.45,
            "advance|spread": 0.95,
            # SPREAD (win-by-margin) × full-time markets — also UNLISTED → +0.6
            # fallback. DERIVED 2026-07-07 from the DC model (GoalSpread min_margin=2
            # = "wins by >=2"): spread is a STRONGER directional signal than a bare
            # win. spread|total is LINE-DEPENDENT — a 2+ margin FORCES >=2 goals so
            # spread⊂over-1.5 is a near-containment (rho~+0.999) BUT Kalshi blocks
            # spread×total conflicts, so only the reachable over-2.5+ value ships:
            # +0.31 (spread≥2 vs >=3 goals, range +0.22 even→+0.46 heavy-fav, wide
            # band). spread|btts NEGATIVE (a clean 2-0 dominant win → not-btts, like
            # a moneyline but stronger). spread|player_goal flips on team orientation
            # (scorer on the spread-winning team vs opponent) — resolved in sgp.py
            # to :same/:opp, same as advance|player_goal.
            # (keys are pair_key-sorted alphabetically: btts<player_goal<spread<total)
            "spread|total": 0.31,
            "btts|spread": -0.30,
            "player_goal|spread:same": 0.46,
            "player_goal|spread:opp": -0.42,
            # TEAM corners (KXWCTCORNERS), MEASURED conditional on 8,981 matches
            # (team-strength + venue controlled, so the residual the copula needs
            # after live marginals). Unlike TOTAL corners, a TEAM's corners are
            # NEGATIVELY tied to that team winning / covering — the chasing team
            # piles on corners (pooled sign is a Simpson's-paradox artifact).
            # ⊥ goals (total/btts ≈ 0). player_goal / advance are LABELED PRIORS
            # (no football-data coverage), wide-banded, not measured.
            # team corners × match winner — ORIENTED (:same/:opp/:tie). A team's
            # corners are anti-correlated with THAT team winning (chasing team
            # earns corners) and positively with the OPPONENT winning; ~0 with a
            # draw. STRENGTH-CONTROLLED (2026-07-08, 8,980 matches, binned by
            # devigged win prob): raw pooled corr is a Simpson trap (+0.05, wrong
            # sign); conditional :same −0.154 / :opp +0.152 / :tie +0.014. The
            # plain entry is the marginal-less / unparseable-orientation fallback.
            "corners_team|moneyline": -0.15,
            "corners_team|moneyline:same": -0.15,
            "corners_team|moneyline:opp": 0.15,
            "corners_team|moneyline:tie": 0.00,
            # team corners × spread — ORIENTED (:same/:opp), the sibling of
            # corners_team|moneyline. −ρ if the corners team is the one covering
            # the margin (chasing/pressing team earns corners), +ρ if the
            # OPPONENT covers. STRENGTH-CONTROLLED (2026-07-08, 8,980 matches):
            # raw pooled +0.07 (Simpson trap, wrong sign); conditional
            # :same −0.114 / :opp +0.109. Plain entry = unparseable-orient fallback.
            "corners_team|spread": -0.13,
            "corners_team|spread:same": -0.11,
            "corners_team|spread:opp": 0.11,
            "corners_team|total": 0.00,
            "btts|corners_team": 0.00,
            # Team corners nest ONLY for the SAME team (POR4 & POR8); game totals
            # do NOT nest (0 in tape). Resolved in sgp.py to ":same"/":opp" by
            # stripping the trailing line digits off the team suffix. OPPOSITE
            # teams: territory is roughly zero-sum, RE-MEASURED 2026-07-07 on
            # 8,981 matches (HC x AC over lines 4-7): implied rho -0.287 (grid) /
            # -0.283 (matched lines), so -0.21 -> -0.28 (the shipped -0.21 was the
            # opposite-team value, ~0.07 too shallow). SAME team, nested lines are
            # EXACT CONTAINMENT (over-M subset of over-N, 0 violations) — when the
            # pair is buried in a larger combo (not the bare-pair IMPOSSIBLE/OK case
            # relationships.py resolves) the copula approximates it with a strong
            # comonotone positive. Plain entry is the opposite-team value, used when
            # the team suffix cannot be parsed.
            "corners_team|corners_team": -0.28,
            "corners_team|corners_team:opp": -0.28,
            "corners_team|corners_team:same": 0.90,
            # corners_team × scorer (M1 2026-07-12): ORIENTED, MEASURED
            # strength-controlled on the Understat x football-data join
            # (n=3,614 / 7,228 star rows). SIGN FLIP vs the old +0.05
            # same-team-attack folk prior: the star scores -> his team is
            # ahead -> its corners are SUPPRESSED (:same -0.163; raw -0.025
            # is the Simpson trap, same as shipped corners_team|moneyline);
            # the OPPONENT's corners rise (:opp +0.113). Routed by the
            # spread×scorer resolver (sgp.py — corners_team's TEAM+digits
            # suffix parses identically); plain = unparseable fallback, band
            # spans the measured -0.14..+0.11.
            "corners_team|player_goal": 0.00,
            "corners_team|player_goal:same": -0.14,
            "corners_team|player_goal:opp": 0.11,
            # advance × team corners (M1 2026-07-12): ORIENTED, DERIVED
            # (bridge advance = win90 + q*draw90, q=0.5+-0.14 sweep) + ET
            # boost, strength-controlled -0.132/+0.132, VALIDATED on real
            # knockouts (KO direct :same -0.08..-0.14, CI contains derived).
            # Routed by the winner-vs-team resolver (advance suffix IS the
            # team code); plain -0.05 kept (flow skews same-team backing),
            # band raised to span +-0.13.
            "advance|corners_team": -0.05,
            "advance|corners_team:same": -0.13,
            "advance|corners_team:opp": 0.13,
            "moneyline|moneyline": -0.95,
            # Period (1st-half) × full-time, CALIBRATED 2026-07-07 on 8,981 club
            # matches (football-data.co.uk HT/FT, era-stable across a 2023
            # split; docs/calibration/results_soccer.md §1). The 1H-winner ×
            # FT-winner sign FLIPS with team orientation — resolved by sgp.py to
            # ":same" (both legs name one team) vs ":opp" (different teams).
            # DRAW-involving winner pairs (M1 2026-07-12): MEASURED — the flat
            # +0.6/band-0.90 fallback they used to hit had the WRONG SIGN for
            # tiexwin/teamxtie. Suffix order = pair_key leg order (1H leg
            # first): ":tiexwin" = 1H draw × FT team win, ":teamxtie" = 1H
            # team lead × FT draw, ":tiextie" = draw × draw (each pooled over
            # both teams where a team is named).
            "first_half_moneyline|moneyline:same": 0.71,
            "first_half_moneyline|moneyline:opp": -0.67,
            "first_half_moneyline|moneyline:tiexwin": -0.15,
            "first_half_moneyline|moneyline:teamxtie": -0.21,
            "first_half_moneyline|moneyline:tiextie": 0.35,
            "first_half_total|total": 0.73,
            # FT-BTTS x 1H-TOTAL-over-N. UNLISTED -> the pair fell to the flat
            # +0.6 same_event fallback whenever the DC pricer declined (live-RFQ
            # combos C22/C27/C28 mispriced vs maker clearing). DERIVED 2026-07-08
            # TWO independent ways on 8,981 club matches (football-data HT/FT,
            # tools/calibrate_soccer_btts_1h_total.py):
            #   STRUCTURAL (shipped half-time DC, inverted per game from the FT
            #     1X2 + O/U-2.5 lines at h=0.45 / dc_rho=-0.05): pooled implied
            #     rho +0.533 (N=1, over-0.5) / +0.544 (N=2, over-1.5).
            #   EMPIRICAL (count P(FT-BTTS & 1H>=N), invert the shipped copula):
            #     +0.570 [+0.482,+0.653] (N=1) / +0.552 [+0.477,+0.623] (N=2).
            # Methods AGREE (|diff| <= 0.037) and the pair is LINE-STABLE (N=1 vs
            # N=2 within 0.02 in BOTH methods) -> one entry, no line-specific key.
            # +0.55 sits at the center of both methods and both lines. COHERENT
            # with the anchors: BELOW first_half_total|total (+0.73 — 1H-total is
            # a component of FT-total) and btts|total (+0.70), because FT-BTTS
            # needs BOTH teams to score across the FULL 90' while a 1H goal is
            # either team inside 45' — a looser, once-removed link (~0.70*0.73
            # latent attenuation lands near +0.51, measured a touch higher for the
            # direct 1H-goal path). Settlement: BTTS = regulation 90', 1H = 45'.
            # Era-stability / structural proxy (no live 1H book to conditional-MLE
            # gate yet), so the 1H-family band until a live 1H book tightens it.
            "btts|first_half_total": 0.55,
            # 1H-winner x 1H-total, WITHIN the first half. The sign FLIPS HARD on
            # whether the 1H-moneyline leg names a TEAM or the TIE — resolved in
            # sgp.py to ":team" / ":tie" (same hard sign-flip as the 1H x FT
            # ":same"/":opp" winner pair). Both are near-DETERMINISTIC structural
            # containments MEASURED on 8,981 club matches (football-data HT
            # scores, tools/calibrate_soccer_1h_winner_total.py):
            #   :team  a 1H lead REQUIRES a goal => 1H-lead subset of 1H-over0.5,
            #          all 5,401 lead matches over -> implied rho +0.99 (clamp +0.95).
            #   :tie   1H under0.5 (0-0) is a subset of 1H-TIE, all 2,518 under
            #          matches are ties -> tie x over implied rho -0.99 (clamp -0.95).
            # Without these the pair fell to the flat same_event_rho +0.6, which
            # is the WRONG SIGN for a TIE leg (it priced tie x under as strongly
            # mutually exclusive when 0-0 IS a tie) — the SUICOL pick-off.
            "first_half_moneyline|first_half_total:team": 0.95,
            "first_half_moneyline|first_half_total:tie": -0.95,
            # First-half SPREAD (1H goal margin, series KXWC1HSPREAD; the only
            # traded line is 2 = "leads at half by over 1.5", i.e. 1H margin>=2)
            # x FULL-TIME legs, CALIBRATED 2026-07-07 on 8,981 club matches
            # (football-data HT/FT; tools/calibrate_soccer_1h_spread.py;
            # results_soccer.md §2). NOTE: the 1H-spread x 1H-total / 1H-btts /
            # 1H-winner pairs ARE reachable (the prod tape has real same-game
            # combos — an earlier "Kalshi blocks 1H×1H" assumption was wrong);
            # they are DEFERRED (uncalibrated → +0.6 fallback), not blocked. Only
            # 1H-spread x FULL-TIME is calibrated here. A 1H spread
            # NAMES a team, so spread|spread and spread|moneyline flip sign HARD
            # on team orientation — resolved in sgp.py to ":same" (both legs
            # name one team) / ":opp" (different teams) by stripping the trailing
            # line digits off the TEAM+digits suffix, the same/opposite analogue
            # of the winner ":same"/":opp" prior. Measured pooled over both
            # naming orientations:
            #   spread|spread :same  1H margin>=2 x FT margin>=2, +0.777
            #     [+0.726,+0.826] (home +0.773 / away +0.776; not deterministic
            #     — a 2-0 half can end level).
            #   spread|spread :opp   1H team-A>=2 x FT team-B>=2, measured -0.646
            #     [-0.802,-0.591]; a >=4-goal swing, P(A|B)=0.002 (near-exclusion,
            #     point estimate driven by 1-2 freak comebacks) — shipped as the
            #     copula-fit -0.65 (reproduces the observed ~0.2% frequency), NOT
            #     clamped to -0.95 (that sits outside the measured CI and would
            #     over-state), with a wide band spanning the small-sample range.
            #   spread|moneyline :same 1H margin>=2 x FT win, +0.739 [+0.652,+0.854].
            #   spread|moneyline :opp  1H team-A>=2 x FT team-B win, -0.662
            #     [-0.709,-0.624]; near-exclusion, shipped copula-fit -0.66.
            # spread|total is orientation-FREE (total names no team): a 1H lead
            # requires 1H goals => positive with FT total. Anchored at the modal
            # FT line over 2.5 (>=3): +0.518 [+0.418,+0.635] (over3.5 +0.44;
            # over1.5 is a structural containment +0.99, out of band — a rare
            # pairing, noted). Wide band absorbs the FT-line dependence.
            "first_half_spread|spread:same": 0.78,
            "first_half_spread|spread:opp": -0.65,
            "first_half_spread|moneyline:same": 0.74,
            "first_half_spread|moneyline:opp": -0.66,
            # 1H-spread × FT-DRAW (M1 2026-07-12): MEASURED -0.444 — the flat
            # +0.6 fallback this case used to hit was the WRONG SIGN (a 2-goal
            # 1H lead makes a FT draw unlikely; a 1.04 rho swing). Pooled over
            # both teams; resolved by the 1H-spread × winner resolver's :tie
            # branch (sgp.py).
            "first_half_spread|moneyline:tie": -0.44,
            "first_half_spread|total": 0.52,
            # === 1H CROSS-TYPE CLUSTER (calibrated 2026-07-08, 3-agent batch:
            # half-time Dixon-Coles + football-data HT/FT + Understat; every pair
            # cross-validated structural vs empirical <=0.04, strength-controlled
            # where a result leg confounds). Retires the +0.6 fallback on the
            # ~700k same-game combos the audit flagged. Bands are the 1H-family
            # era/structural proxy (no live 1H book to conditional-MLE gate yet).
            # -- 1H-winner (first_half_moneyline) x FT, oriented :same/:opp/:tie --
            "advance|first_half_moneyline:same": 0.64,
            "advance|first_half_moneyline:opp": -0.64,
            "advance|first_half_moneyline:tie": 0.00,
            "first_half_moneyline|total:team": 0.24,
            "first_half_moneyline|total:tie": -0.42,
            "first_half_moneyline|player_goal:same": 0.45,
            "first_half_moneyline|player_goal:opp": -0.20,
            "first_half_moneyline|player_goal:tie": -0.22,
            # btts x 1H-lead is POSITIVE (a 1H lead = a goal already happened +
            # open game), UNLIKE FT btts|moneyline (negative = clean sheet).
            "btts|first_half_moneyline:team": 0.10,
            "btts|first_half_moneyline:tie": -0.17,
            "first_half_moneyline|spread:same": 0.70,
            "first_half_moneyline|spread:opp": -0.63,
            "first_half_moneyline|spread:tie": -0.32,
            # -- 1H-total / 1H-btts x FT, plain scalars (no team named) --
            "advance|first_half_total": 0.09,
            "first_half_total|moneyline": 0.14,
            "first_half_total|spread": 0.27,
            "first_half_total|player_goal": 0.33,
            "advance|first_half_btts": -0.03,
            "first_half_btts|total": 0.65,       # o2.5 anchor; o1.5 = exact containment
            "first_half_btts|moneyline": -0.03,
            "first_half_btts|spread": -0.08,
            "first_half_btts|player_goal": 0.33,
            # -- 1H x 1H (within-half; logical containments hit +-0.95) + 1H-spread x FT --
            "first_half_spread|first_half_total": 0.95,   # 1H margin>=2 => 1H over1.5
            "first_half_btts|first_half_moneyline:team": -0.18,
            "first_half_btts|first_half_moneyline:tie": 0.30,
            "first_half_moneyline|first_half_spread:same": 0.95,
            "first_half_moneyline|first_half_spread:opp": -0.95,
            "first_half_moneyline|first_half_spread:tie": -0.95,
            "first_half_btts|first_half_spread": -0.22,
            "first_half_btts|first_half_total": 0.95,      # 1H-btts => 1H over1.5
            # FT-btts × 1H-btts (M1 2026-07-12): EXACT CONTAINMENT — 1H-btts
            # => FT-btts, 0/8,981 violations, implied rho at the +0.99 cap ->
            # the containment clamp 0.95 (convention of
            # first_half_btts|first_half_total above). The BARE pair is
            # intercepted by relationships.py containment; this entry prices
            # the pair when BURIED in a larger combo (corners_team|
            # corners_team:same precedent).
            "btts|first_half_btts": 0.95,
            "advance|first_half_spread:same": 0.72,
            "advance|first_half_spread:opp": -0.72,
            "btts|first_half_spread": 0.00,      # VERIFIED ~0 (2H recovery cancels it)
            "first_half_spread|player_goal:same": 0.45,
            "first_half_spread|player_goal:opp": -0.22,
            # FT advance x regulation-DRAW (the OBSERVED advance|moneyline flow; a
            # draw is symmetric re: which team advances -> ~0. Team cases are
            # logical containment/impossible, handled in relationships.py).
            "advance|moneyline:tie": 0.00,
        },
        # NFL moneyline|total = 0.00 DOUBLY confirmed: pooled-vs-Vegas-lines
        # AND conditional-MLE (+0.02, SE 0.023) whose fit does NOT beat
        # independence out of sample — per the directive, a dependence that
        # loses to independence OOS is noise and must not ship.
        "nfl": {
            "moneyline|total": 0.00,
            "spread|total": 0.03,
            "moneyline|spread": 0.88,
            "extras|total": 0.20,
            "moneyline|moneyline": -0.95,
        },
        # NBA verified on MODERN data (hoopR/ESPN 2016-2025, n=12,567):
        # ml|total +0.008, era-split 2016-20 vs 21-25 drift +0.008 — the zero
        # correlation survived the 3PT revolution.
        "nba": {
            "moneyline|total": 0.01,
            "moneyline|moneyline": -0.95,
        },
        "wnba": {
            "moneyline|total": 0.01,
            "moneyline|moneyline": -0.95,
        },
        # MLB (Retrosheet 2015-2024, n=20,642): home winner is slightly
        # ANTI-correlated with over (−0.056: home wins often skip the bottom
        # 9th ⇒ fewer runs). extras|total uses the POST-2020 value (+0.10):
        # the ghost-runner rule structurally changed extras scoring
        # (pre-2020 −0.04 → post +0.10) — a measured rule-change break.
        "mlb": {
            "moneyline|total": -0.05,
            "extras|total": 0.10,
            "extras|moneyline": -0.04,
            "moneyline|moneyline": -0.95,
            # ---- MLB props tranche (Retrosheet 2005-25, 49,486 games; 8-agent
            # measurement pass + xhigh adversarial judge, 2026-07-09; source of
            # truth docs/calibration/staged_mlb_props.md FINAL RECOMMENDED
            # TABLE). All keys legtypes.pair_key-sorted, verified by running the
            # helper. Retires the sign-wrong flat +0.60/0.90 fallback these
            # same-game pairs hit while KXMLBKS/HIT/HR/HRR/TB/RFI typed UNKNOWN.
            # -- [A] orientation-free MEASURED --
            # Starter K over × GAME total over: cluster-boot 99% [-0.271,-0.230];
            # ladder-FLAT across all posted K lines 3.5-8.5, so ONE entry serves
            # every KS rung (self-median line convention judge-validated).
            "player_ks|total": -0.25,
            # MEASURED +0.233 in the GAME-total frame. NOT the team-frame +0.367:
            # that is HR × OWN-TEAM total (KXMLBTEAMTOTAL), which is NOT
            # combo-eligible — loading it here would mis-sign/mis-scale
            # (staged_mlb_props.md:166-169). Dilution below team-frame is required.
            "player_hr|total": 0.24,
            "player_hit|total": 0.25,      # rung-monotone 0.19(1+)/0.25(2+)/0.30(3+)
            "player_tb|total": 0.27,       # rungs: 2+ 0.26 / 4+ 0.28
            "player_hrr|total": 0.40,      # starters-only pop; line-monotone .33/.40/.44
            "rfi|total": 0.37,             # strongest + most era-stable MLB pair measured
            "moneyline|rfi": 0.00,         # either-team RFI is team-symmetric ⇒ ⊥ winner
            "player_ks|rfi": -0.10,        # orientation-free; ships without the resolver
            "player_ks|player_ks": 0.04,   # opposing starters; sign confirmed small-pos
            # Copula-FALLBACK only: the structural margin/total grid supersedes
            # this whenever it prices; it fires on structural declines. Parity
            # coupling explains the fixed-line oscillation seen in calibration.
            "spread|total": 0.13,
            # -- [B] same-family batter pairs, UNROUTED -- teammate/opponent
            # ticker-prefix routing is the next phase; these are the measured
            # sign-spanning UNROUTED blends (e.g. hrr: teammate +0.17 / opponent
            # 0.00). Bands span both routed values.
            "player_hr|player_hr": 0.03,
            "player_hit|player_hit": 0.00,
            "player_tb|player_tb": 0.00,
            "player_hrr|player_hrr": 0.08,
            # -- [C] ML-orientation-resolver-gated, NEUTRALIZED -- the measured
            # values are team-oriented (+/-0.24 ml|ks, +/-0.23 ml|hr, +/-0.23
            # ml|hit, +/-0.37 ml|hrr; facing-pitcher hit|ks -0.13, hr|ks -0.075,
            # hrr|ks -0.18) and sign-FLIP when the ML leg is the opponent.
            # sgp.py has no MLB team-orientation resolver yet (the fav/dog
            # marginal axis is the WRONG axis) — until a resolver compares the
            # prop ticker's team prefix to the ML suffix, ship 0.00 with a
            # sign-spanning band: point error <=0.37 vs up to 0.84 at +0.60.
            # The signed values above unlock with the resolver.
            "moneyline|player_ks": 0.00,
            "moneyline|player_hr": 0.00,
            "moneyline|player_hit": 0.00,
            "moneyline|player_hrr": 0.00,
            # ml|tb was MISSING from the tranche enumeration entirely (caught
            # by the 2026-07-10 routing design pass — 1,267 sg pairs/10h at
            # flat +0.6; docs/reports/2026-07-10-bands-routing-sweep-designs.md
            # §2). Orientation-dependent like its ml|prop siblings above, so
            # NEUTRALIZED the same way — but unlike them its ml-oriented
            # values are NOT yet measured (MEASURE — DO-8), so the band is the
            # widest sibling span (0.30, ~ml|ks) rather than a measured ±.
            "moneyline|player_tb": 0.00,
            "player_hit|player_ks": 0.00,
            "player_hr|player_ks": 0.00,
            # Judge-CONFIRMED neutralized: binding constraint is facing/starters
            # -0.190 (CI lo -0.196), not the now-measured ~0 teammate.
            "player_hrr|player_ks": 0.00,
            # -- [D] cross-family batter-batter, distinct players (judge-approved
            # 2026-07-09; bounded by the measured same-family values; hr|hrr
            # measured teammate +0.05 / opponent 0.00) --
            "player_hit|player_hr": 0.01,
            "player_hit|player_hrr": 0.04,
            "player_hit|player_tb": 0.02,
            # hr|hrr: measured in the 2026-07-09 tranche (teammate +0.053 /
            # opponent -0.003, cross-player only — same-player HR=>HRR>=3 is
            # EXACT containment, never a copula rho); judge unrouted +0.03.
            "player_hr|player_hrr": 0.03,
            "player_hr|player_tb": 0.02,
            "player_hrr|player_tb": 0.04,
            # ks|tb: judge RE-CENTERED from the resolver-gated 0.00/0.15
            # placeholder — orientation-free like player_ks|rfi.
            "player_ks|player_tb": -0.06,
            # -- ROUTED oriented entries (sgp.py MLB team-routing resolvers,
            # 2026-07-10). The plain 0.00 / unrouted entries ABOVE STAY as the
            # fail-closed fallback for any parse doubt (soccer precedent:
            # plain + oriented coexist). ML×prop ':same' = the prop player's
            # team IS the ML YES team. For batter-stat × player_ks, ':opp' IS
            # the facing case (batter bats against the opposing starter) and
            # carries the measured negative. hr|ks / hrr|ks ':same' (teammate)
            # were deliberately ABSENT here while unmeasured; B4 measured them
            # (B4 2026-07-10) — wired in the B4 addendum block at the end of
            # this table. Values:
            # staged_mlb_props.md FINAL RECOMMENDED TABLE [B]/[C]/[D] +
            # 2026-07-10 judge amendments.
            "moneyline|player_ks:same": 0.24,   # pitcher's team wins × his Ks over
            "moneyline|player_ks:opp": -0.24,   # exact sign flip (2-way complement)
            "moneyline|player_hr:same": 0.23,   # (2+ rung +0.27: per-rung later)
            "moneyline|player_hr:opp": -0.23,
            "moneyline|player_hit:same": 0.23,
            "moneyline|player_hit:opp": -0.23,
            "moneyline|player_hrr:same": 0.37,  # strongest oriented pair measured
            "moneyline|player_hrr:opp": -0.37,
            "player_hit|player_ks:opp": -0.13,  # FACING (measured -0.126)
            # (B4 2026-07-10) teammate un-runged aggregate, MEASURED (spans
            # r1-r3 -0.0023..+0.0134) — replaces the 0.00 placeholder.
            "player_hit|player_ks:same": 0.010,
            "player_hr|player_ks:opp": -0.075,  # FACING (measured -0.076)
            "player_hrr|player_ks:opp": -0.18,  # FACING (measured -0.190, judge band)
            # [B] same-family batter stacks, teammate/opponent split (measured):
            "player_hr|player_hr:same": 0.04,
            "player_hr|player_hr:opp": 0.02,
            "player_hit|player_hit:same": 0.07,
            "player_hit|player_hit:opp": 0.00,
            "player_tb|player_tb:same": 0.06,
            "player_tb|player_tb:opp": 0.00,
            "player_hrr|player_hrr:same": 0.17,
            "player_hrr|player_hrr:opp": 0.00,
            # [D] cross-family: ONLY hr|hrr split is measured (teammate +0.053
            # / opp -0.003); hit|hr, hit|hrr, hit|tb, hr|tb, hrr|tb stay
            # unrouted-plain until the split measurement runs (existing npz).
            "player_hr|player_hrr:same": 0.05,
            "player_hr|player_hrr:opp": 0.00,
            # ---- DO-1 untabled-cell quick-fix (2026-07-10; source
            # docs/reports/2026-07-10-bands-routing-sweep-designs.md §3). The
            # sweep's full 9x9 matrix audit marked 12 cells NO prior tranche
            # had listed (~13,257 sg pairs/10h still at the flat +0.60/0.90
            # fallback — spread×props alone is 9,641/10h, 11.5x the ml|spread
            # flow). With these tabled (and moneyline|spread since DO-3,
            # 2026-07-10), NO same-game MLB pair
            # ever hits the flat default. All keys generated by EXECUTING
            # legtypes.pair_key (the rfi|player_ks sort trap: 'player_*' sorts
            # before 'rfi' and 'spread'). ----
            # -- [C-sibling] spread × props, NEUTRALIZED plain fallbacks --
            # spread is team-SIGNED like the moneyline ("wins by more than
            # N-0.5"), so every spread×prop correlation sign-FLIPS with the
            # prop player's team vs the spread team: no unoriented scalar is
            # right. The oriented/runged values are now MEASURED and wired in
            # the Phase 2 block below (A1/A2 + judge fallbacks, 2026-07-10);
            # these plain 0.00 entries STAY as the fail-closed parse fallback
            # (KEEP directive, phase2_wire_list.txt lines 78-82) with bands
            # RAISED to span the measured oriented extremes.
            "player_ks|spread": 0.00,
            "player_hit|spread": 0.00,
            "player_hr|spread": 0.00,
            "player_hrr|spread": 0.00,
            "player_tb|spread": 0.00,
            # -- rfi × spread / batter props, labeled priors
            # (MEASURE-BEFORE-TIGHTEN — DO-8) -- rfi ("a run in the FIRST
            # INNING by EITHER team") is team-symmetric / orientation-FREE,
            # so these ship as plain scalars with no resolver dependency.
            # rfi|spread: MEASURED per-rung (M2 2026-07-12, zero-gaps wire —
            # DO-8 MEASURE-BEFORE-TIGHTEN executed; corpus parsed2.npz PBP
            # inning-1 runs, linescore-validated, x final margins, 49,486
            # games 2005-25, ties excluded). rfi is team-symmetric but the
            # spread frame is NOT (leading home team skips bottom-9), so each
            # rung value is the HOME/AWAY MIDPOINT; the band covers the frame
            # half-span (<= 0.0344), CI hw (<= 0.0245) and era shift
            # (<= 0.0295). The plain key is the unparsed-rung fallback —
            # REPLACES the hand-prior 0.00/0.15: spans the r1-r5 midpoints
            # 0.000..+0.107 and both frames -0.026..+0.131 (the old 0.00 was
            # right ONLY at r1, 0.13 shallow at deep rungs — inside the old
            # band, so no misprice occurred, but now MEASURED). Rung ladder
            # in the M2 zero-gaps block at the end of this table.
            "rfi|spread": 0.05,
            # rfi × batter props: MEASURED plain orientation-free values (A3,
            # Phase 2 wire 2026-07-10, docs/calibration/phase2_wire_list.txt
            # lines 51-54) — REPLACES the one-factor labeled priors
            # (0.09/0.09/0.10/0.10, bands 0.15-0.20). The old +0.10 sketch cap
            # had CLIPPED hrr (measured +0.122). Bands cover the rung spreads
            # and pooled-window values quoted in the wire list.
            "player_hit|rfi": 0.065,    # (A3) band covers 2+ rung +0.085 / pooled +0.058
            "player_hr|rfi": 0.091,     # (A3) pooled +0.083
            "player_tb|rfi": 0.085,     # (A3) covers 4+ rung +0.094 / pooled +0.079
            "player_hrr|rfi": 0.122,    # (A3) band spans rungs 2+ +0.102 .. 5+ +0.140
            # ---- moneyline|spread (DO-3, wired 2026-07-10). Truth is
            # containment-shaped by side (:same +0.95 / :opp -0.95, exact:
            # 0/98,980 violations — no unoriented scalar is right).
            # relationships.py routes resolvable same-game shapes to
            # CONTAINMENT / IMPOSSIBLE before any copula; the copula-reachable
            # side-cases (e.g. win-yes × cover-no) route to the oriented keys
            # via sgp._mlb_winner_spread_prior. The PLAIN entry only catches
            # PARSE-FAILURES (unresolvable team suffix / mismatched game
            # segments): 0.00 with a sign-spanning band — replaces the
            # documented-badly-wrong flat +0.6 default those cases used to
            # hit. ----
            "moneyline|spread": 0.00,
            "moneyline|spread:same": 0.95,
            "moneyline|spread:opp": -0.95,
            # ---- Phase 2 wire (2026-07-10): measured oriented/runged prop ×
            # spread/total/ML entries + judge fallbacks, wired VERBATIM from
            # docs/calibration/phase2_wire_list.txt (A1-A4 measurement agents
            # + final-pairs judge; values keyed to the 2015-2025 window; all
            # base keys verified by EXECUTING legtypes.pair_key). Key grammar
            # per the wire-list CONVENTION header: ':rN' = Kalshi ticker line
            # integer of a rung-keyed leg (YES iff stat/margin > N-0.5; props
            # N+; spread wins by N+ — e.g. spread r1.5 = ticker 2 = :r2).
            # Rung-keyed families: player_hit/hr/tb/hrr + spread; when BOTH
            # legs are rung-keyed the suffixes CHAIN in pair_key leg order
            # ('player_hit|spread:same:r1:r2' = hit 1+ × own team wins by 2+).
            # player_ks/total/moneyline/rfi legs NEVER carry rungs. ':same' =
            # prop player's team IS the ML/spread YES team; for batter-stat ×
            # player_ks, ':opp' IS the facing case. Batter-prop cells are the
            # all-PA batter-game frame EXCEPT player_hrr|total:rN = STARTERS
            # frame (consistent with shipped plain player_hrr|total 0.40).
            # Lookup fallback: exact rung key → un-runged oriented key →
            # plain (fail-closed); NO rung interpolation/extrapolation, EVER.
            # The three wire-list NOT-WIRED holes (lines 89-91:
            # player_hr|total:r3, tb×ks extra rungs, teammate ':same' for
            # hit/hr/hrr×ks + ks|tb) were CLOSED BY MEASUREMENT in the B4
            # addendum block at the end of this table (B4 2026-07-10;
            # docs/calibration/phase2_wire_list_addendum.txt). Still standing
            # from line 90: tb×ks rung slope interpolation/extrapolation is
            # REFUTED (U-shaped ladder) — per-rung keyed ONLY, forever. ----
            # -- (A1) player_hit × spread, oriented + chained rungs --
            "player_hit|spread:same:r1:r2": 0.239,   # (A1)
            "player_hit|spread:same:r1:r3": 0.249,   # (A1)
            "player_hit|spread:same:r1:r4": 0.255,   # (A1)
            "player_hit|spread:same:r1:r5": 0.256,   # (A1)
            "player_hit|spread:same:r2:r2": 0.268,   # (A1)
            "player_hit|spread:same:r2:r3": 0.285,   # (A1)
            "player_hit|spread:same:r2:r4": 0.297,   # (A1)
            "player_hit|spread:same:r2:r5": 0.304,   # (A1)
            "player_hit|spread:opp:r1:r2": -0.194,   # (A1)
            "player_hit|spread:opp:r1:r3": -0.172,   # (A1)
            "player_hit|spread:opp:r1:r4": -0.160,   # (A1)
            "player_hit|spread:opp:r1:r5": -0.150,   # (A1)
            "player_hit|spread:opp:r2:r2": -0.223,   # (A1)
            "player_hit|spread:opp:r2:r3": -0.203,   # (A1)
            "player_hit|spread:opp:r2:r4": -0.189,   # (A1)
            "player_hit|spread:opp:r2:r5": -0.178,   # (A1)
            # (A1) hr|spread:same is FLAT across spread rungs (range 0.0026)
            # — a single un-runged oriented entry; its band also covers the
            # unmeasured hr 2+ rung ~+0.27.
            "player_hr|spread:same": 0.241,          # (A1)
            "player_hr|spread:opp:r1:r2": -0.210,    # (A1)
            "player_hr|spread:opp:r1:r3": -0.197,    # (A1)
            "player_hr|spread:opp:r1:r4": -0.185,    # (A1)
            "player_hr|spread:opp:r1:r5": -0.178,    # (A1)
            "player_tb|spread:same:r2:r2": 0.265,    # (A1)
            "player_tb|spread:same:r2:r3": 0.277,    # (A1)
            "player_tb|spread:same:r2:r4": 0.283,    # (A1)
            "player_tb|spread:same:r2:r5": 0.287,    # (A1)
            "player_tb|spread:opp:r2:r2": -0.221,    # (A1)
            "player_tb|spread:opp:r2:r3": -0.201,    # (A1)
            "player_tb|spread:opp:r2:r4": -0.186,    # (A1)
            "player_tb|spread:opp:r2:r5": -0.176,    # (A1)
            "player_hrr|spread:same:r3:r2": 0.389,   # (A1)
            "player_hrr|spread:same:r3:r3": 0.404,   # (A1)
            "player_hrr|spread:same:r3:r4": 0.410,   # (A1)
            "player_hrr|spread:same:r3:r5": 0.413,   # (A1)
            "player_hrr|spread:opp:r3:r2": -0.334,   # (A1)
            "player_hrr|spread:opp:r3:r3": -0.309,   # (A1)
            "player_hrr|spread:opp:r3:r4": -0.289,   # (A1)
            "player_hrr|spread:opp:r3:r5": -0.274,   # (A1)
            # -- (A2) moneyline × player_tb, oriented (rung-flat single
            # entries: tb2 +0.2494 / tb4 +0.2581, delta 0.011; ':opp' is the
            # exact 2-way complement after tie exclusion AND was ALSO
            # verified by direct measurement — not a hand negation) --
            "moneyline|player_tb:same": 0.25,        # (A2)
            "moneyline|player_tb:opp": -0.25,        # (A2)
            # -- (A2) player_ks × spread, oriented + runged (rung = the
            # spread leg's; ks legs never carry rungs) --
            "player_ks|spread:same:r2": 0.207,       # (A2)
            "player_ks|spread:same:r3": 0.200,       # (A2)
            "player_ks|spread:same:r4": 0.188,       # (A2)
            "player_ks|spread:same:r5": 0.170,       # (A2)
            "player_ks|spread:opp:r2": -0.260,       # (A2)
            "player_ks|spread:opp:r3": -0.281,       # (A2)
            "player_ks|spread:opp:r4": -0.297,       # (A2; band widened for era drift)
            "player_ks|spread:opp:r5": -0.310,       # (A2; band widened for era drift)
            # -- (A4) player_hr × total rungs (all-PA frame, consistent with
            # the shipped plain 0.24; rung drift r1→r2 +0.071 [0.057,0.085]) --
            "player_hr|total:r1": 0.238,             # (A4)
            "player_hr|total:r2": 0.306,             # (A4)
            # -- (A4) player_hit × player_ks FACING rungs (':opp' per shipped
            # convention; rung = the hit leg's) --
            "player_hit|player_ks:opp:r1": -0.126,   # (A4; FACING)
            "player_hit|player_ks:opp:r2": -0.149,   # (A4; FACING)
            "player_hit|player_ks:opp:r3": -0.160,   # (A4; FACING)
            # -- (A4) player_ks × player_tb FACING rungs (pair_key order puts
            # ks first — the rung is the TB leg's). NON-MONOTONE ladder (r4
            # dips: HR⇒TB>=4 containment dilutes the 3.5-line rung; the
            # multi-hit channel returns at r5) — per-rung keyed ONLY, never
            # interpolate/extrapolate TB rungs. --
            "player_ks|player_tb:opp:r2": -0.125,    # (A4; FACING)
            "player_ks|player_tb:opp:r3": -0.122,    # (A4; FACING; statistically = r2)
            "player_ks|player_tb:opp:r4": -0.103,    # (A4; FACING; NON-MONOTONE rung)
            "player_ks|player_tb:opp:r5": -0.127,    # (A4; FACING)
            # -- (A4) player_hrr × total rungs (STARTERS frame, consistent
            # with the shipped plain 0.40; r4 pooled 2005-25 — the one cell
            # without a 15-25 run, adjacent-rung 15-25 shifts <+0.004) --
            "player_hrr|total:r2": 0.379,            # (A4)
            "player_hrr|total:r3": 0.407,            # (A4; = shipped plain 0.40)
            "player_hrr|total:r4": 0.437,            # (A4)
            "player_hrr|total:r5": 0.468,            # (A4)
            # -- judge fallbacks: un-runged oriented entries for unparsed
            # rung lines (lookup tier 2: exact rung key → THESE → plain);
            # each spans its measured rung grid --
            "player_hit|spread:same": 0.27,    # judge fallback; spans +0.239..+0.304
            "player_hit|spread:opp": -0.19,    # judge fallback; spans -0.150..-0.223
            "player_hr|spread:opp": -0.19,     # judge fallback; spans -0.178..-0.210
            "player_tb|spread:same": 0.28,     # judge fallback; spans +0.265..+0.287
            "player_tb|spread:opp": -0.20,     # judge fallback; spans -0.176..-0.221
            "player_hrr|spread:same": 0.40,    # judge fallback; spans +0.389..+0.413
            "player_hrr|spread:opp": -0.30,    # judge fallback; spans -0.274..-0.334
            "player_ks|spread:same": 0.19,     # judge fallback; spans +0.170..+0.207
            "player_ks|spread:opp": -0.29,     # judge fallback; spans -0.260..-0.310
            "player_ks|player_tb:opp": -0.12,  # judge fallback; spans -0.103..-0.127
            # -- routed :same/:opp splits for the cross-family batter-batter
            # [D] pairs (final-pairs judge 2026-07-10, cross-checked corpora;
            # wire-list lines 95-104). Completes the [D] routing — the plain
            # entries above STAY as the fail-closed parse fallback. --
            "player_hit|player_hr:same": 0.03,     # judge: teammate +0.0304
            "player_hit|player_hr:opp": 0.00,      # judge: opponent -0.0043
            "player_hit|player_hrr:same": 0.09,    # judge: teammate +0.0877
            "player_hit|player_hrr:opp": 0.00,     # judge: opponent -0.0087
            "player_hit|player_tb:same": 0.05,     # judge: teammate +0.0478
            "player_hit|player_tb:opp": 0.00,      # judge: opponent -0.0034
            "player_hr|player_tb:same": 0.04,      # judge: teammate +0.0356
            "player_hr|player_tb:opp": 0.00,       # judge: opponent ~0.00
            "player_hrr|player_tb:same": 0.10,     # judge: teammate +0.0950
            "player_hrr|player_tb:opp": 0.00,      # judge: opponent -0.0066
            # ---- B4 measurement addendum (B4 2026-07-10): closes the three
            # Phase-2 NOT-WIRED holes (wire list lines 89-91) by DIRECT
            # measurement against the unrelaxed Phase-1 judge standard (25/25
            # judge windows PASS; 7/7 regression anchors reproduced to
            # ±0.0005 first). Wired VERBATIM from
            # docs/calibration/phase2_wire_list_addendum.txt (full-precision
            # numbers in the B4 measurements JSON); all base keys verified by
            # EXECUTING legtypes.pair_key. Grammar/frames per the Phase 2
            # CONVENTION header above. NO interpolation anywhere: TB rungs
            # r6/r7 are Kalshi-real (tape universe {2..7}, 650/161 leg
            # occurrences) and DIRECTLY measured; TB rungs >7 are unseen on
            # tape and stay fail-closed on the un-runged :opp fallback.
            # Teammate rungs NOT wired (hit r4, hrr r1 — Kalshi-real but
            # outside B4's scope) fall back to the un-runged :same aggregates
            # below, NEVER interpolated. The plain ks|tb blend (-0.06 band
            # 0.10) is KEPT — B4 confirmed teammate ≈ +0.006..+0.010, so the
            # 'unmeasured teammate ~0' blend assumption now stands measured. ----
            # -- line-89 hole: hr 3+ × game total (all-PA frame, consistent
            # with wired :r1/:r2; full-precision remeasure of the A4
            # UNCERTAIN cell — 154 positives 15-25, CI95 hw 0.0690; ladder
            # stays monotone r1 +0.238 → r2 +0.306 → r3 +0.357) --
            "player_hr|total:r3": 0.357,             # (B4 2026-07-10)
            # -- line-90 hole: tb×ks Kalshi-real extra rungs (FACING; direct
            # per-rung measurement, 12,661/6,045 positives; multi-hit channel
            # keeps the post-U-turn r5 depth -0.127; the un-runged :opp
            # fallback -0.12 band 0.05 still covers both — unchanged) --
            "player_ks|player_tb:opp:r6": -0.127,    # (B4 2026-07-10; FACING)
            "player_ks|player_tb:opp:r7": -0.128,    # (B4 2026-07-10; FACING)
            # -- line-91 hole: teammate ':same' orientations (own-team
            # starter K frame, batter==own-starter excluded; teammate/env
            # channel ≈ 0 — every hw95 < 0.01, every era shift < 0.01). The
            # hit|ks:same un-runged aggregate is wired IN PLACE above
            # (replaces its 0.00 placeholder). --
            "player_hit|player_ks:same:r1": 0.013,   # (B4 2026-07-10)
            "player_hit|player_ks:same:r2": 0.007,   # (B4 2026-07-10)
            "player_hit|player_ks:same:r3": -0.002,  # (B4 2026-07-10)
            # hr|ks:same: single un-runged oriented entry at the hr 1+ anchor
            # (precedent: hr|spread:same; r2 sensitivity -0.0005 inside band)
            "player_hr|player_ks:same": 0.010,       # (B4 2026-07-10)
            # hrr|ks:same rungs (all-PA frame — the frame of the Phase-1 hrr
            # facing runs); hrr r1 unmeasured → falls to the aggregate:
            "player_hrr|player_ks:same:r2": 0.017,   # (B4 2026-07-10)
            "player_hrr|player_ks:same:r3": 0.014,   # (B4 2026-07-10)
            "player_hrr|player_ks:same:r4": 0.010,   # (B4 2026-07-10)
            "player_hrr|player_ks:same:r5": 0.005,   # (B4 2026-07-10)
            "player_hrr|player_ks:same": 0.010,      # (B4 2026-07-10; aggregate r2-r5)
            # ks|tb:same rungs (rung is the TB leg's — pair_key puts ks
            # first); hit r4-style unmeasured TB teammate rungs fall to the
            # aggregate:
            "player_ks|player_tb:same:r2": 0.010,    # (B4 2026-07-10)
            "player_ks|player_tb:same:r3": 0.007,    # (B4 2026-07-10)
            "player_ks|player_tb:same:r4": 0.009,    # (B4 2026-07-10)
            "player_ks|player_tb:same:r5": 0.006,    # (B4 2026-07-10)
            "player_ks|player_tb:same": 0.010,       # (B4 2026-07-10; aggregate r2-r5)
            # ---- M2 ZERO-GAPS wire (M2 2026-07-12): pair-table rungs wired
            # VERBATIM from job 24844262 tmp/zerogaps/mlb_wire_list.txt
            # sections 3-7 (full precision mlb_measurements.json). Estimator =
            # tetrachoric (bvnu vs scipy 1.1e-16), game-cluster bootstrap,
            # 2015-25 window; judge standard UNRELAXED (CI95 hw <= 0.08, era
            # shift inside band, band = max(0.04, CI95 hw, |era shift|)).
            # Grammar/frames per the Phase 2 CONVENTION header above; rung
            # universe = TAPE EVIDENCE (852,940 MLB-strict RFQ'd combos) — no
            # rung invented, nothing interpolated (TB U-ladder refutation
            # stands). ----
            # -- (S3) teammate rungs formerly on the un-runged :same
            # aggregates (which STAY as the unparsed-rung fallback) --
            "player_hit|player_ks:same:r4": -0.009,  # (M2; teammate ~0 at r4)
            "player_hrr|player_ks:same:r1": 0.015,   # (M2; completes r1-r5)
            # -- (S4) FACING (:opp) sides of the same Kalshi-real rungs
            # (monotone ladder continuation, per-rung DIRECT) --
            "player_hit|player_ks:opp:r4": -0.187,   # (M2; extends r1-r3 ladder)
            "player_hrr|player_ks:opp:r1": -0.153,   # (M2; r1 SHALLOWER than -0.18)
            "player_hrr|player_ks:opp:r2": -0.176,   # (M2)
            "player_hrr|player_ks:opp:r3": -0.178,   # (M2; = Phase-1 anchor -0.1776)
            "player_hrr|player_ks:opp:r4": -0.184,   # (M2)
            "player_hrr|player_ks:opp:r5": -0.189,   # (M2; r1->r5 monotone)
            # -- (S5) TB r8 (now Kalshi-real: 98 legs this window) + ks|tb
            # :same deep rungs; post-U-turn depth holds, NO interpolation --
            "player_ks|player_tb:opp:r8": -0.119,    # (M2; FACING, direct)
            "player_ks|player_tb:same:r6": -0.002,   # (M2; teammate ~0)
            "player_ks|player_tb:same:r7": -0.004,   # (M2; teammate ~0)
            "player_ks|player_tb:same:r8": -0.007,   # (M2; teammate ~0)
            # -- (S6) moneyline × player_hr 2+/3+ rungs (detector est ~+0.27
            # CONFIRMED; :opp = exact tetrachoric complement, ties excluded,
            # same convention as the shipped +/-0.23 pair) --
            "moneyline|player_hr:same:r2": 0.270,    # (M2)
            "moneyline|player_hr:opp:r2": -0.270,    # (M2; mirror)
            "moneyline|player_hr:same:r3": 0.338,    # (M2; 163 positives 15-25)
            "moneyline|player_hr:opp:r3": -0.338,    # (M2; mirror)
            # -- (S7) rfi|spread per-rung ladder (HOME/AWAY midpoints; see the
            # plain-entry comment above) --
            "rfi|spread:r1": 0.000,                  # (M2; home +0.026/away -0.026)
            "rfi|spread:r2": 0.050,                  # (M2)
            "rfi|spread:r3": 0.079,                  # (M2)
            "rfi|spread:r4": 0.097,                  # (M2)
            "rfi|spread:r5": 0.107,                  # (M2)
        },
    }
    # Band overrides: sport-prefixed keys ("nfl:moneyline|total") for
    # calibrated sport entries; unprefixed keys for the global table.
    pair_rho_uncertainty: dict[str, float] = {
        "moneyline|moneyline": 0.04,
        "soccer:moneyline|total": 0.10,     # conditional-MLE SE .019; band covers asymmetry
        "soccer:btts|total": 0.12,          # pooled-method: widened pending conditional refit
        "soccer:btts|moneyline": 0.10,      # pooled-method (but club==intl)
        "soccer:btts|moneyline:fav": 0.10,
        "soccer:btts|moneyline:dog": 0.10,  # structural implication, 1 live validation
        "soccer:moneyline|player_goal": 0.12,   # structural implication ×2 examples
        "soccer:btts|player_goal": 0.30,    # 0.55 mid; band spans fav +0.31 ↔ dog +0.81
        "moneyline|player_goal": 0.20,      # global fallback: non-soccer scorers unmeasured
        "soccer:total|total": 0.04,
        "soccer:corners|total": 0.08,
        "soccer:btts|corners": 0.08,
        "soccer:corners|moneyline": 0.08,        # total corners, measured ~0 (team is separate now)
        "soccer:corners|spread": 0.08,           # total corners ⊥ margin (measured +0.02)
        # corners × 1H: MEASURED (M1 2026-07-12); band = max(0.04, CI95 hw,
        # |season shift|, line-halfspread) per the judge standard. Plain
        # corners_team entries are the unparseable-orientation fallbacks and
        # SPAN the measured oriented extremes (-0.20..+0.23 / -0.18..+0.15).
        "soccer:corners|first_half_moneyline": 0.06,
        "soccer:corners|first_half_total": 0.05,
        "soccer:corners|first_half_btts": 0.04,
        "soccer:corners|first_half_spread": 0.05,
        "soccer:corners_team|first_half_moneyline": 0.25,
        "soccer:corners_team|first_half_moneyline:same": 0.05,
        "soccer:corners_team|first_half_moneyline:opp": 0.04,
        "soccer:corners_team|first_half_moneyline:tie": 0.04,
        "soccer:corners_team|first_half_total": 0.07,
        "soccer:corners_team|first_half_btts": 0.05,
        "soccer:corners_team|first_half_spread": 0.22,
        "soccer:corners_team|first_half_spread:same": 0.07,
        # :opp small cell: season shift +0.156 is the band driver (M1).
        "soccer:corners_team|first_half_spread:opp": 0.16,
        # advance|corners plain: band must SPAN the strength curve (+-0.23)
        # for marginal-less callers (M1 2026-07-12; was the 0.15 labeled prior).
        "soccer:advance|corners": 0.25,
        # advance × full-time bands (DC-derived + 4-study cross-check; the
        # correlation swings 0→~0.22 with favorite strength, so a wide band):
        "soccer:advance|total": 0.15,
        "soccer:advance|btts": 0.13,
        "soccer:advance|player_goal:same": 0.15,
        "soccer:advance|player_goal:opp": 0.15,
        "soccer:advance|spread": 0.10,          # near-containment, tight
        # spread × full-time bands (DC-derived; spread|total wide for the line
        # dependence, the rest span the matchup range):
        "soccer:spread|total": 0.20,
        "soccer:btts|spread": 0.13,
        "soccer:player_goal|spread:same": 0.15,
        "soccer:player_goal|spread:opp": 0.15,
        # MEASURED (M1 2026-07-12): band 0.10 spans the club-controlled /
        # KO-ET-adjusted regime spread around -0.03 (was the 0.20 labeled prior).
        "soccer:corners|player_goal": 0.10,
        "soccer:corners_team|moneyline": 0.10,
        "soccer:corners_team|moneyline:same": 0.10,
        "soccer:corners_team|moneyline:opp": 0.10,
        "soccer:corners_team|moneyline:tie": 0.08,
        "soccer:corners_team|spread": 0.10,
        "soccer:corners_team|spread:same": 0.10,
        "soccer:corners_team|spread:opp": 0.10,
        "soccer:corners_team|total": 0.08,
        "soccer:btts|corners_team": 0.08,
        "soccer:corners|corners_team": 0.15,  # home/away split (0.57-0.65) + line drift
        "soccer:corners_team|corners_team": 0.10,
        "soccer:corners_team|corners_team:opp": 0.10,   # re-measured, tight
        "soccer:corners_team|corners_team:same": 0.10,  # comonotone containment approx
        # corners_team × scorer / advance × corners_team (M1 2026-07-12):
        # oriented cells measured/derived tight; plain fallbacks span their
        # oriented extremes (-0.14..+0.11 / +-0.13).
        "soccer:corners_team|player_goal": 0.20,
        "soccer:corners_team|player_goal:same": 0.05,
        "soccer:corners_team|player_goal:opp": 0.04,
        "soccer:advance|corners_team": 0.20,
        "soccer:advance|corners_team:same": 0.05,
        "soccer:advance|corners_team:opp": 0.05,
        "soccer:player_goal|total": 0.15,        # hand-prior width around measured +0.46
        "soccer:player_goal|player_goal": 0.10,  # teammate(0)/opponent(+0.05) blend band
        "soccer:moneyline|moneyline": 0.04,
        # Period × full-time bands (results_soccer.md §1: era-stability proxy,
        # not the conditional-MLE gate — no live 1H book yet — so kept modest).
        "soccer:first_half_moneyline|moneyline:same": 0.08,
        "soccer:first_half_moneyline|moneyline:opp": 0.08,
        # Draw-orientation cells (M1 2026-07-12): all on the 0.04 judge floor
        # (n=8,981, CI95 hw and season shift inside).
        "soccer:first_half_moneyline|moneyline:tiexwin": 0.04,
        "soccer:first_half_moneyline|moneyline:teamxtie": 0.04,
        "soccer:first_half_moneyline|moneyline:tiextie": 0.04,
        "soccer:first_half_spread|moneyline:tie": 0.04,
        # Exact-containment cap value (M1 2026-07-12): tight band, the
        # containment-clamp convention.
        "soccer:btts|first_half_btts": 0.04,
        "soccer:first_half_total|total": 0.12,
        # FT-BTTS × 1H-total: two-method agreement (structural +0.53/+0.54,
        # empirical +0.57/+0.55) + line-stability; 1H-family proxy band (no live
        # 1H book to conditional-MLE gate). Spans both methods and the empirical
        # 99% CI up to +0.65.
        "soccer:btts|first_half_total": 0.13,
        # Near-deterministic containment (tight 99% CI, both hitting the clamp);
        # modest band consistent with the 1H family (no live 1H book to gate on).
        "soccer:first_half_moneyline|first_half_total:team": 0.10,
        "soccer:first_half_moneyline|first_half_total:tie": 0.10,
        # 1H-spread × full-time bands (results_soccer.md §2; era-stability proxy,
        # no live 1H book to conditional-MLE gate — kept in the 1H family's
        # 0.10-0.15 range). :opp / total wider for small-sample / FT-line spread.
        "soccer:first_half_spread|spread:same": 0.12,
        "soccer:first_half_spread|spread:opp": 0.15,
        "soccer:first_half_spread|moneyline:same": 0.12,
        "soccer:first_half_spread|moneyline:opp": 0.15,
        "soccer:first_half_spread|total": 0.15,
        # 1H cross-type cluster bands (2026-07-08; 1H-family era/structural proxy)
        "soccer:advance|first_half_moneyline:same": 0.12,
        "soccer:advance|first_half_moneyline:opp": 0.12,
        "soccer:advance|first_half_moneyline:tie": 0.10,
        "soccer:first_half_moneyline|total:team": 0.08,
        "soccer:first_half_moneyline|total:tie": 0.10,
        "soccer:first_half_moneyline|player_goal:same": 0.15,
        "soccer:first_half_moneyline|player_goal:opp": 0.18,
        "soccer:first_half_moneyline|player_goal:tie": 0.12,
        "soccer:btts|first_half_moneyline:team": 0.10,
        "soccer:btts|first_half_moneyline:tie": 0.10,
        "soccer:first_half_moneyline|spread:same": 0.10,
        "soccer:first_half_moneyline|spread:opp": 0.12,
        "soccer:first_half_moneyline|spread:tie": 0.12,
        "soccer:advance|first_half_total": 0.16,
        "soccer:first_half_total|moneyline": 0.13,
        "soccer:first_half_total|spread": 0.14,
        "soccer:first_half_total|player_goal": 0.17,
        "soccer:advance|first_half_btts": 0.12,
        "soccer:first_half_btts|total": 0.13,
        "soccer:first_half_btts|moneyline": 0.10,
        "soccer:first_half_btts|spread": 0.11,
        "soccer:first_half_btts|player_goal": 0.18,
        "soccer:first_half_spread|first_half_total": 0.10,
        "soccer:first_half_btts|first_half_moneyline:team": 0.10,
        "soccer:first_half_btts|first_half_moneyline:tie": 0.10,
        "soccer:first_half_moneyline|first_half_spread:same": 0.10,
        "soccer:first_half_moneyline|first_half_spread:opp": 0.10,
        "soccer:first_half_moneyline|first_half_spread:tie": 0.10,
        "soccer:first_half_btts|first_half_spread": 0.10,
        "soccer:first_half_btts|first_half_total": 0.10,
        "soccer:advance|first_half_spread:same": 0.13,
        "soccer:advance|first_half_spread:opp": 0.15,
        "soccer:btts|first_half_spread": 0.10,
        "soccer:first_half_spread|player_goal:same": 0.15,
        "soccer:first_half_spread|player_goal:opp": 0.15,
        "soccer:advance|moneyline:tie": 0.10,
        "nfl:moneyline|total": 0.05,
        "nfl:spread|total": 0.05,
        "nfl:moneyline|spread": 0.05,
        "nfl:extras|total": 0.10,
        "nfl:moneyline|moneyline": 0.04,
        "nba:moneyline|total": 0.05,
        "nba:moneyline|moneyline": 0.04,
        "wnba:moneyline|total": 0.12,       # NBA transfer: wider until measured
        "wnba:moneyline|moneyline": 0.04,
        "mlb:moneyline|total": 0.06,
        "mlb:extras|total": 0.10,           # post-rule-change sample is smaller
        "mlb:extras|moneyline": 0.08,
        "mlb:moneyline|moneyline": 0.04,
        # MLB props tranche bands (2026-07-09 measurement pass + judge; see the
        # pair_rho_by_sport["mlb"] block for provenance).
        # [A] measured, orientation-free:
        "mlb:player_ks|total": 0.12,        # spans era drift -0.28..-0.22
        "mlb:player_hr|total": 0.10,
        "mlb:player_hit|total": 0.12,       # spans the 1+/2+/3+ rung ladder
        "mlb:player_tb|total": 0.10,
        "mlb:player_hrr|total": 0.08,
        "mlb:rfi|total": 0.10,
        "mlb:moneyline|rfi": 0.05,
        "mlb:player_ks|rfi": 0.08,
        "mlb:player_ks|player_ks": 0.08,
        "mlb:spread|total": 0.10,
        # [B] same-family unrouted (span teammate/opponent):
        "mlb:player_hr|player_hr": 0.06,
        "mlb:player_hit|player_hit": 0.08,
        "mlb:player_tb|player_tb": 0.08,
        "mlb:player_hrr|player_hrr": 0.12,
        # [C] neutralized (sign-spanning until the ML orientation resolver):
        "mlb:moneyline|player_ks": 0.30,    # spans +/-0.24 orientation
        "mlb:moneyline|player_hr": 0.28,    # spans +/-0.23
        "mlb:moneyline|player_hit": 0.26,   # spans +/-0.23
        "mlb:moneyline|player_hrr": 0.40,   # spans +/-0.37
        # RAISED 0.15→0.17 (Phase 2 wire: deepest measured facing rung -0.160)
        "mlb:player_hit|player_ks": 0.17,
        "mlb:player_hr|player_ks": 0.12,    # facing -0.075
        "mlb:player_hrr|player_ks": 0.20,   # facing -0.18 (judge-confirmed band)
        # [D] cross-family batter-batter (judge-approved):
        "mlb:player_hit|player_hr": 0.06,
        "mlb:player_hit|player_hrr": 0.10,
        "mlb:player_hit|player_tb": 0.08,
        "mlb:player_hr|player_hrr": 0.08,   # covers teammate +0.053 / opp ~0
        "mlb:player_hr|player_tb": 0.06,
        "mlb:player_hrr|player_tb": 0.10,
        "mlb:player_ks|player_tb": 0.10,    # judge re-centered -0.06/0.10
        # ROUTED oriented bands (1:1 with the pair_rho_by_sport["mlb"] oriented
        # entries; measured-value bands from staged_mlb_props.md [B]/[C]/[D]):
        "mlb:moneyline|player_ks:same": 0.06,   # ladder-flat across K rungs
        "mlb:moneyline|player_ks:opp": 0.06,
        "mlb:moneyline|player_hr:same": 0.08,
        "mlb:moneyline|player_hr:opp": 0.08,
        "mlb:moneyline|player_hit:same": 0.08,
        "mlb:moneyline|player_hit:opp": 0.08,
        "mlb:moneyline|player_hrr:same": 0.08,
        "mlb:moneyline|player_hrr:opp": 0.08,
        "mlb:player_hit|player_ks:opp": 0.10,
        # (B4 2026-07-10) tightened 0.05 → 0.04 with the measured aggregate
        # (+0.010; all rung CI95s inside the band):
        "mlb:player_hit|player_ks:same": 0.04,
        "mlb:player_hr|player_ks:opp": 0.05,
        "mlb:player_hrr|player_ks:opp": 0.06,
        "mlb:player_hr|player_hr:same": 0.06,
        "mlb:player_hr|player_hr:opp": 0.05,
        "mlb:player_hit|player_hit:same": 0.06,
        "mlb:player_hit|player_hit:opp": 0.06,
        "mlb:player_tb|player_tb:same": 0.05,
        "mlb:player_tb|player_tb:opp": 0.06,
        "mlb:player_hrr|player_hrr:same": 0.06,
        "mlb:player_hrr|player_hrr:opp": 0.05,
        "mlb:player_hr|player_hrr:same": 0.06,
        "mlb:player_hr|player_hrr:opp": 0.05,
        # DO-1 untabled-cell bands (2026-07-10 sweep §3; every new entry gets
        # a band — zero orphans, count-verified):
        "mlb:moneyline|player_tb": 0.30,    # unmeasured; widest ml|prop sibling span
        # spread × props plain fallbacks: RAISED 0.20→spans (Phase 2 wire
        # KEEP/RAISE, phase2_wire_list.txt lines 78-82) — the plain
        # fail-closed 0.00 entries must span the measured oriented extremes.
        "mlb:player_ks|spread": 0.32,       # spans -0.310..+0.207
        "mlb:player_hit|spread": 0.31,      # spans ±0.304
        "mlb:player_hr|spread": 0.28,       # spans -0.210..+0.241 + unmeasured hr2+ ~+0.27
        "mlb:player_hrr|spread": 0.42,      # spans -0.334..+0.413
        "mlb:player_tb|spread": 0.30,       # spans ±0.287
        # (M2 2026-07-12) plain rfi|spread is now the MEASURED unparsed-rung
        # fallback +0.05: band 0.08 spans the r1-r5 midpoints AND both
        # home/away frames -0.026..+0.131 (replaces the 0.00/0.15 hand prior).
        "mlb:rfi|spread": 0.08,
        # rfi × props: MEASURED bands (A3, Phase 2 wire — replaces the
        # labeled-prior 0.15/0.20 widths; cover rung + pooled spreads)
        "mlb:player_hit|rfi": 0.05,
        "mlb:player_hr|rfi": 0.04,
        "mlb:player_tb|rfi": 0.04,
        "mlb:player_hrr|rfi": 0.06,
        # moneyline|spread (DO-3, 2026-07-10): the plain entry is the
        # parse-failure fallback and must SPAN THE SIGN (either team could be
        # the covering one); the oriented ±0.95 entries are measured-exact
        # containment shapes (0/98,980 violations) with a tight band (the
        # +side clamp at 0.95 makes it effectively one-sided).
        "mlb:moneyline|spread": 0.95,
        "mlb:moneyline|spread:same": 0.04,
        "mlb:moneyline|spread:opp": 0.04,
        # ---- Phase 2 wire bands (2026-07-10; 1:1 with the Phase 2 rho block
        # in pair_rho_by_sport["mlb"] — zero orphans, count-verified). Bands
        # are half-widths from docs/calibration/phase2_wire_list.txt;
        # PA-frame/era systematics are priced into the 0.04-0.08 floors. ----
        # (A1) player_hit × spread chained rungs:
        "mlb:player_hit|spread:same:r1:r2": 0.05,
        "mlb:player_hit|spread:same:r1:r3": 0.05,
        "mlb:player_hit|spread:same:r1:r4": 0.05,
        "mlb:player_hit|spread:same:r1:r5": 0.05,
        "mlb:player_hit|spread:same:r2:r2": 0.05,
        "mlb:player_hit|spread:same:r2:r3": 0.05,
        "mlb:player_hit|spread:same:r2:r4": 0.05,
        "mlb:player_hit|spread:same:r2:r5": 0.05,
        "mlb:player_hit|spread:opp:r1:r2": 0.05,
        "mlb:player_hit|spread:opp:r1:r3": 0.05,
        "mlb:player_hit|spread:opp:r1:r4": 0.05,
        "mlb:player_hit|spread:opp:r1:r5": 0.05,
        "mlb:player_hit|spread:opp:r2:r2": 0.05,
        "mlb:player_hit|spread:opp:r2:r3": 0.05,
        "mlb:player_hit|spread:opp:r2:r4": 0.05,
        "mlb:player_hit|spread:opp:r2:r5": 0.05,
        # (A1) player_hr × spread (':same' band also covers hr2+ ~+0.27):
        "mlb:player_hr|spread:same": 0.05,
        "mlb:player_hr|spread:opp:r1:r2": 0.05,
        "mlb:player_hr|spread:opp:r1:r3": 0.05,
        "mlb:player_hr|spread:opp:r1:r4": 0.05,
        "mlb:player_hr|spread:opp:r1:r5": 0.05,
        # (A1) player_tb × spread chained rungs:
        "mlb:player_tb|spread:same:r2:r2": 0.05,
        "mlb:player_tb|spread:same:r2:r3": 0.05,
        "mlb:player_tb|spread:same:r2:r4": 0.05,
        "mlb:player_tb|spread:same:r2:r5": 0.05,
        "mlb:player_tb|spread:opp:r2:r2": 0.05,
        "mlb:player_tb|spread:opp:r2:r3": 0.05,
        "mlb:player_tb|spread:opp:r2:r4": 0.05,
        "mlb:player_tb|spread:opp:r2:r5": 0.05,
        # (A1) player_hrr × spread chained rungs:
        "mlb:player_hrr|spread:same:r3:r2": 0.05,
        "mlb:player_hrr|spread:same:r3:r3": 0.05,
        "mlb:player_hrr|spread:same:r3:r4": 0.05,
        "mlb:player_hrr|spread:same:r3:r5": 0.05,
        "mlb:player_hrr|spread:opp:r3:r2": 0.05,
        "mlb:player_hrr|spread:opp:r3:r3": 0.05,
        "mlb:player_hrr|spread:opp:r3:r4": 0.05,
        "mlb:player_hrr|spread:opp:r3:r5": 0.05,
        # (A2) moneyline × player_tb oriented:
        "mlb:moneyline|player_tb:same": 0.06,
        "mlb:moneyline|player_tb:opp": 0.06,
        # (A2) player_ks × spread (opp r4/r5 widened for era drift):
        "mlb:player_ks|spread:same:r2": 0.05,
        "mlb:player_ks|spread:same:r3": 0.05,
        "mlb:player_ks|spread:same:r4": 0.05,
        "mlb:player_ks|spread:same:r5": 0.05,
        "mlb:player_ks|spread:opp:r2": 0.06,
        "mlb:player_ks|spread:opp:r3": 0.06,
        "mlb:player_ks|spread:opp:r4": 0.07,
        "mlb:player_ks|spread:opp:r5": 0.08,
        # (A4) rung ladders:
        "mlb:player_hr|total:r1": 0.04,
        "mlb:player_hr|total:r2": 0.04,
        "mlb:player_hit|player_ks:opp:r1": 0.04,
        "mlb:player_hit|player_ks:opp:r2": 0.04,
        "mlb:player_hit|player_ks:opp:r3": 0.04,
        "mlb:player_ks|player_tb:opp:r2": 0.04,
        "mlb:player_ks|player_tb:opp:r3": 0.04,
        "mlb:player_ks|player_tb:opp:r4": 0.04,
        "mlb:player_ks|player_tb:opp:r5": 0.04,
        "mlb:player_hrr|total:r2": 0.05,
        "mlb:player_hrr|total:r3": 0.05,
        "mlb:player_hrr|total:r4": 0.05,
        "mlb:player_hrr|total:r5": 0.05,
        # judge fallbacks (un-runged oriented; each spans its rung grid):
        "mlb:player_hit|spread:same": 0.08,
        "mlb:player_hit|spread:opp": 0.08,
        "mlb:player_hr|spread:opp": 0.07,
        "mlb:player_tb|spread:same": 0.06,
        "mlb:player_tb|spread:opp": 0.07,
        "mlb:player_hrr|spread:same": 0.07,
        "mlb:player_hrr|spread:opp": 0.09,
        "mlb:player_ks|spread:same": 0.07,
        "mlb:player_ks|spread:opp": 0.09,
        "mlb:player_ks|player_tb:opp": 0.05,
        # routed [D] cross-family batter-batter splits (final-pairs judge):
        "mlb:player_hit|player_hr:same": 0.05,
        "mlb:player_hit|player_hr:opp": 0.04,
        "mlb:player_hit|player_hrr:same": 0.05,
        "mlb:player_hit|player_hrr:opp": 0.04,
        "mlb:player_hit|player_tb:same": 0.05,
        "mlb:player_hit|player_tb:opp": 0.04,
        "mlb:player_hr|player_tb:same": 0.05,
        "mlb:player_hr|player_tb:opp": 0.04,
        "mlb:player_hrr|player_tb:same": 0.05,
        "mlb:player_hrr|player_tb:opp": 0.04,
        # B4 measurement addendum bands (B4 2026-07-10; 1:1 with the B4 rho
        # block in pair_rho_by_sport["mlb"] — zero orphans, count-verified).
        # Half-widths from docs/calibration/phase2_wire_list_addendum.txt:
        # band = max(0.04, CI95 hw, |era shift|); hr|total:r3 = CI95 hw
        # 0.0690 rounded up; all teammate/tb-rung cells sit on the 0.04 floor.
        "mlb:player_hr|total:r3": 0.07,
        "mlb:player_ks|player_tb:opp:r6": 0.04,
        "mlb:player_ks|player_tb:opp:r7": 0.04,
        "mlb:player_hit|player_ks:same:r1": 0.04,
        "mlb:player_hit|player_ks:same:r2": 0.04,
        "mlb:player_hit|player_ks:same:r3": 0.04,
        "mlb:player_hr|player_ks:same": 0.04,
        "mlb:player_hrr|player_ks:same:r2": 0.04,
        "mlb:player_hrr|player_ks:same:r3": 0.04,
        "mlb:player_hrr|player_ks:same:r4": 0.04,
        "mlb:player_hrr|player_ks:same:r5": 0.04,
        "mlb:player_hrr|player_ks:same": 0.04,
        "mlb:player_ks|player_tb:same:r2": 0.04,
        "mlb:player_ks|player_tb:same:r3": 0.04,
        "mlb:player_ks|player_tb:same:r4": 0.04,
        "mlb:player_ks|player_tb:same:r5": 0.04,
        "mlb:player_ks|player_tb:same": 0.04,
        # M2 ZERO-GAPS wire bands (M2 2026-07-12; 1:1 with the M2 rho block in
        # pair_rho_by_sport["mlb"] — zero orphans, count-verified). Band =
        # max(0.04, CI95 hw, |era shift|) per the unrelaxed judge standard;
        # ml|hr r3 = 0.07 (CI95 hw 0.0627 rounded up, 163 positives 15-25 —
        # precedent player_hr|total:r3); everything else sits on the 0.04
        # floor. rfi|spread rung bands additionally cover the home/away frame
        # half-span (<= 0.0344).
        "mlb:player_hit|player_ks:same:r4": 0.04,
        "mlb:player_hrr|player_ks:same:r1": 0.04,
        "mlb:player_hit|player_ks:opp:r4": 0.04,
        "mlb:player_hrr|player_ks:opp:r1": 0.04,
        "mlb:player_hrr|player_ks:opp:r2": 0.04,
        "mlb:player_hrr|player_ks:opp:r3": 0.04,
        "mlb:player_hrr|player_ks:opp:r4": 0.04,
        "mlb:player_hrr|player_ks:opp:r5": 0.04,
        "mlb:player_ks|player_tb:opp:r8": 0.04,
        "mlb:player_ks|player_tb:same:r6": 0.04,
        "mlb:player_ks|player_tb:same:r7": 0.04,
        "mlb:player_ks|player_tb:same:r8": 0.04,
        "mlb:moneyline|player_hr:same:r2": 0.04,
        "mlb:moneyline|player_hr:opp:r2": 0.04,
        "mlb:moneyline|player_hr:same:r3": 0.07,
        "mlb:moneyline|player_hr:opp:r3": 0.07,
        "mlb:rfi|spread:r1": 0.04,
        "mlb:rfi|spread:r2": 0.04,
        "mlb:rfi|spread:r3": 0.04,
        "mlb:rfi|spread:r4": 0.04,
        "mlb:rfi|spread:r5": 0.04,
    }
    # Orientation CURVES: a pair whose YES-YES rho is a monotone function of one
    # leg's marginal (not a single scalar or a fav/dog step). Keyed
    # "<sport>:<pair_key>" -> sorted (marginal, rho) knots, piecewise-linear
    # interpolated in sgp.py with FLAT clamp outside the knot range; the curve
    # WINS over the scalar / fav-dog entry whenever leg marginals are available.
    # soccer:btts|moneyline RE-MEASURED 2026-07-07 (8,982 top-5-EU matches,
    # btts x team-win implied rho binned across devigged closing win-prob): heavy
    # longshot (~0.20) ~ 0 (NOT the -0.19 the old 2-anchor blend over-negated),
    # deepening monotonically to ~-0.36 for heavy favorites ("winners keep clean
    # sheets" is a favorites effect; a dog can only win by scoring).
    oriented_curve: dict[str, list[tuple[float, float]]] = {
        "soccer:btts|moneyline": [
            (0.20, -0.05),
            (0.35, -0.18),
            (0.50, -0.28),
            (0.65, -0.34),
            (0.85, -0.36),
        ],
        # advance × TOTAL corners STRENGTH CURVE (M1 2026-07-12, zero-gaps
        # wire; job 24844262 tmp/zerogaps/soccer_measurements.json 'curves',
        # q=0.5, corners line >= 9; knots verbatim). Keyed on the ADVANCE
        # leg's YES-side marginal — NOT a moneyline (sgp.py routes the
        # {advance, corners} pair here explicitly). Mechanism: a drawn-90
        # forces ET and corners settle INCLUDING ET, so a longshot's advance
        # co-occurs with the extra corners window (dog +0.23) while a heavy
        # favorite's advance skips it (fav -0.23); pooled 0.00 by identity —
        # the plain scalar entry (0.00 band 0.25) is the marginal-less
        # fallback spanning this curve. Antisymmetric by construction
        # (rho(p) = -rho(1-p)); knot band 0.10 (q attenuation at the ends;
        # q-sweep +-0.04, ET-pmf unc +-0.01).
        "soccer:advance|corners": [
            (0.1684, 0.2274),
            (0.2823, 0.2336),
            (0.3491, 0.1549),
            (0.4195, 0.0732),
            (0.4607, 0.0204),
            (0.5393, -0.0205),
            (0.5805, -0.0732),
            (0.6509, -0.1549),
            (0.7177, -0.2336),
            (0.8316, -0.2274),
        ],
    }
    oriented_curve_uncertainty: dict[str, float] = {
        "soccer:btts|moneyline": 0.13,
        "soccer:advance|corners": 0.10,
    }


class QuoteConfig(StrictModel):
    base_width_cc: int = 200
    per_leg_width_cc: int = 100
    # legs component = per_leg × n^convexity — model error compounds
    # multiplicatively with leg count, so width should too (1.0 = linear).
    leg_count_convexity: float = 1.0
    uncertainty_width_scale: float = 1.0
    size_width_cc_per_100: int = 50
    time_wide_threshold_s: float = 21_600.0
    time_width_cc: int = 200
    in_play_extra_cc: int = 800
    # DO-6 basket width adder (2026-07-10): measured +25-35c/$1 overbid on
    # 8-16-leg all-NO single-prop-family baskets (docs/reports/ 2026-07-10 MLB
    # reports). Extra quote WIDTH (int centi-cents) added when a combo has
    # >= 8 legs AND every leg is NO-side AND all legs are one single MLB prop
    # family (player_hr/player_hit/player_tb/player_hrr/player_ks). Applied
    # AFTER all normal width components, BEFORE the maker-favorable snap; it
    # can only WIDEN, never tighten. 0 disables.
    basket_width_extra_cc: int = 250
    min_capture_cc: int = 100
    free_money_margin_cc: int = 100
    # Fade defense (2026-07-08): quote combos ONE-SIDED as a pure parlay SELLER —
    # force yes_bid=0 so we can only ever end up LONG NO (sell the parlay), never
    # LONG YES (buy it). On Kalshi a combo is a two-sided binary; accepting our
    # yes_bid makes US long YES = the adversely-selected fade flow (settlement
    # backtest −14¢/ct on that side); accepting our no_bid makes us long NO = the
    # +EV seller side. Enabled in prod.yaml/demo.yaml; pydantic default stays
    # False so the pricing primitive + its tests remain two-sided. See
    # docs/reports/2026-07-08-combo-yes-no-side-mechanics.md.
    sell_parlays_only: bool = False
    # Longshots: absolute uncertainty shrinks with tiny fairs (gradient ∝ P),
    # which is anti-conservative in RELATIVE terms for whoever shorts the
    # longshot — floor it as a fraction of fair below the threshold.
    longshot_fair_threshold: float = 0.15
    longshot_min_rel_uncertainty: float = 0.25
    # Favorites stacks: well-estimated products, price-insensitive flow —
    # tighten to win it (1.0 = off; validate via markouts before lowering).
    favorite_leg_threshold: float = 0.65
    favorite_width_multiplier: float = 1.0
    # --- Impossible-combo farming (see pricing/quote.construct_farm_quote) ---
    # A logically-impossible combo (relationship.farmable) can only settle NO,
    # so the maker who shorts YES / is long the certain-NO side collects the
    # premium risk-free. We quote such combos instead of declining them.
    farm_impossible_combos: bool = True
    # Multiplier on the naive-independence YES value (the operator's chosen
    # anchor). 1.0 = quote the impossible YES at exactly its independence price.
    farm_markup: float = 1.0
    # Conservative per-combo size cap (whole contracts). Farming a TRUE
    # impossibility is riskless, but the only loss path is a misclassification,
    # so we cap size well below max_contracts_per_quote (100) as defense in
    # depth until live settlements confirm the classifier on farmed combos.
    farm_max_contracts: int = 50


class SkewConfig(StrictModel):
    """Inventory-aware quote skew (Phase 5, R3 Part A; risk/skew.py).

    DARK SHIP by default (``enabled=False``): the skew is COMPUTED + LOGGED on
    every quote but passed as 0 into the pricer — a zero-P&L shadow classifier.
    Only a shadow-markout validation (does it reduce portfolio CVaR without an
    adverse markout?) may flip ``enabled`` true; the weights are STRUCTURAL,
    tuned on exposure-vs-markout, NEVER on a P&L window (feedback_no_refit_on_pnl).

    Sign (load-bearing, R3 §A0): the whole lever operates on ``no_bid``. The
    CLASSIFIER ``skew_cc`` reads intuitively — a CONCENTRATING candidate is
    ``>= 0`` and an OFFSETTING one is ``<= 0`` — but the PRICER seam
    (``no_raw = ($1 − fair) − half − fee_no + inventory_skew_cc``) runs opposite,
    so ``InventorySkew.applied_cc`` NEGATES it into the pricer: a CONCENTRATING
    candidate WIDENS (lower ``no_bid`` ⇒ dearer combo ⇒ sell LESS), an OFFSETTING
    one REBATES (higher ``no_bid`` ⇒ cheaper combo ⇒ win MORE of the flattening
    flow). After the negation the offsetting rebate is the ``no_bid``-raising,
    free-money-dangerous direction — bounded by ``skew_max_tighten_cc`` and
    doubly contained by the free-money clamp in construct_quote.
    """

    enabled: bool = False
    w_conc: float = 1.0                # concentration (widen) weight
    w_off: float = 1.0                 # offset (rebate) weight
    gamma: float = 2.0                 # convex headroom ramp f(u)=u**gamma
    skew_max_widen_cc: int = 600       # cap on the positive (concentrating) side
    skew_max_tighten_cc: int = 150     # cap on the negative (offsetting) rebate

    @field_validator("w_conc", "w_off")
    @classmethod
    def _nonneg_weight(cls, v: float) -> float:
        if v < 0.0:
            raise ValueError(f"skew weight must be >= 0, got {v}")
        return v

    @field_validator("gamma")
    @classmethod
    def _positive_gamma(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"skew gamma must be > 0, got {v}")
        return v

    @field_validator("skew_max_widen_cc", "skew_max_tighten_cc")
    @classmethod
    def _nonneg_cap(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"skew cap must be >= 0, got {v}")
        return v


class WidenConfig(StrictModel):
    """Widen-vs-DECLINE policy (Phase 5, R3 Part R2; risk/skew.py).

    SHADOW by default (``enabled=False``): the would-be decision is LOGGED every
    quote with zero live impact. When enabled, a candidate that is CONCENTRATING
    AND near a per-game cap (``util >= util_threshold``) DECLINES
    (SKIP_WIDEN_AVOIDED) rather than posting a wide quote — widening a thin book
    near a limit only attracts hitters (our own P&L-sweep finding). An OFFSETTING
    candidate near a cap is never declined (it balances the book). The
    widen-attracts-toxic-flow decision is graded on markouts, never a P&L window.
    """

    enabled: bool = False
    util_threshold: float = 0.75

    @field_validator("util_threshold")
    @classmethod
    def _valid_threshold(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(f"widen util_threshold must be in (0, 1], got {v}")
        return v


class ExternalOddsConfig(StrictModel):
    """SportsGameOdds adapter (docs/api-notes/sportsgameodds.md). OFF by
    default; free tier is 2,500 objects/month so the poller is budget-gated."""

    enabled: bool = False
    leagues: list[str] = []               # e.g. ["NBA", "NFL"]
    weight: float = 0.3                   # blend weight vs Kalshi book's 1.0
    poll_interval_s: float = 3_600.0
    max_events_per_league: int = 10
    max_age_s: float = 900.0              # cached marginal expiry
    devig_method: str = "power"
    base_uncertainty: float = 0.01
    # Explicit Kalshi ticker → "eventID|oddID" table; unmapped = Kalshi-only.
    mapping: dict[str, str] = {}


class StructuralConfig(StrictModel):
    """Dixon-Coles scoreline pricer for soccer SGPs (pricing/dixon_coles.py).

    ``enabled`` may be flipped ON only by an out-of-sample gate result
    (tools/validate_structural_oos.py): a structural fit that does not beat
    the v1 copula on held-out seasons is noise and must not ship.

    GATE PASSED 2026-07-06 (8,980 club games, train <2024 / test 23/24+24/25):
    structural beats the SHIPPED v1 copula on all three OOS joint-log-loss
    metrics — hw×over 1.24657 vs 1.24734, hw×btts 1.26330 vs 1.26724, and the
    3-leg triple 1.70607 vs 1.74775 (independence 1.94197). The margin GROWS
    with combo complexity: coherent scorelines beat pairwise rho stitching
    most where the maker quotes most."""

    enabled: bool = True
    max_goals: int = 12
    # DC low-score adjustment: FITTED on train-season scorelines through the
    # production inversion (grid MLE, tools/validate_structural_oos.py).
    dc_rho: float = -0.05
    dc_rho_band: float = 0.08
    # ET intensity as a fraction of regulation scoring rate (30min pro-rata =
    # 1/3); band edges re-price the joint for the model-form width.
    et_factor: float = 0.3333
    et_factor_low: float = 0.25
    et_factor_high: float = 0.40
    # P(named team wins a shootout | level after ET) for Advance legs.
    # Kalshi's knockout game market includes pens (rules text 2026-07-06);
    # 0.5 prior with a band covering first-kicker/keeper effects.
    pens_win_prob: float = 0.5
    pens_band: float = 0.10
    # First-half goal share h: goals_1H ~ Poisson(lam*h) per team, so a 1H leg
    # prices on the DC half-split scoreline and its correlations with FT (and
    # other 1H) legs are DERIVED coherently instead of hitting the flat +0.6
    # copula default. MEASURED 0.4507 on 8,981 football-data.co.uk HT/FT club
    # matches (top-5 EU 20/21-24/25; per-league 0.440 Serie A .. 0.461 Bundes),
    # matching the design's 0.45. A banded CONSTANT, never inverted from a single
    # 1H leg (design_halftime_dc.md §6); the ±band re-prices the joint so the
    # width absorbs the share uncertainty + the omitted inter-half serial corr.
    # Only exercised on combos that actually carry a 1H leg.
    half_share: float = 0.45
    half_share_band: float = 0.03  # covers the 0.44-0.46 league spread (§7)
    misfit_uncertainty_scale: float = 1.0
    # Series prefixes whose matches are knockout format (ET+pens possible).
    # Settlement windows per family are RULE-BOOK (operator-provided Kalshi
    # rules text 2026-07-06): knockout game market = advance incl pens;
    # BTTS/totals = regulation; player props = full game excl pens.
    # Remaining assumption: this maps SERIES, not match phase — a group-stage
    # KXWC match would be misclassified (fine for the current knockout
    # rounds; revisit for the next tournament's group stage).
    knockout_series: list[str] = ["KXWC"]
    # P0-7 PREFERRED — CONSERVATIVE shared-factor loading for the risk-MC
    # conditioning of a KNOCKOUT total-corners fallback leg on its game's sampled
    # scoreline intensity (corners settle including ET, so a level-after-90 scoreline
    # opens an extra corners window — config ``pair_rho`` ``advance|corners`` measured
    # a dog +0.23 ↔ fav −0.23 ET strength curve, pooled ~0). Small + width-bearing:
    # NOT a fabricated strong correlation. 0.0 ⇒ conditioning off (production sample
    # = the independent structural split). Applied ONLY to knockout total corners;
    # group-format corners are measured ⊥ goals (``corners|total`` = 0.00) and cards
    # have no defensible link ⇒ 0 (independence + the worse-tail challenger backstop).
    corners_et_loading: float = 0.10


class MarginTotalConfig(StrictModel):
    """Bivariate-normal (margin, total) structural pricer for NFL/NBA/WNBA
    (pricing/margin_total.py). Per-game means invert from live prices; the
    sport shapes below are calibrated from RECENT seasons
    (tools/calibrate_margin_total.py, 2026-07-06 — NFL 2020-2025 closing-line
    residuals; NBA 2022-2026 & WNBA 2021-2026 team-fixed-effects residuals,
    the FE method validated against NFL's line residuals). A sport enters
    ``enabled_sports`` ONLY via an OOS gate (directive point 4).

    NFL GATE PASSED 2026-07-06 (tools/validate_margin_total_oos.py; train
    2015-2023, test 2024-2025 n=562): structural beats the shipped v1 copula
    on all three OOS metrics — hw×over 1.29275 vs 1.29293, hw×cover 0.96260
    vs 0.99940 (exact comonotone geometry vs the 0.88 approximation), triple
    1.65544 vs 1.69217.

    WNBA enabled 2026-07-06 by OPERATOR REQUEST (season live now): the
    geometry is the NFL-gated one, the WNBA shape is calibrated on 1,338
    recent games through 2026-07-05, and its rho≈0 means ml×total prices
    within noise of the incumbent v1 — the upgrade is coherent spread/
    team-total joints. Confirmation gate from prod-shadow settlements as
    data accrues. NBA stays disabled until its season approaches (odds
    source or shadow gate)."""

    enabled_sports: list[str] = ["nfl", "wnba"]
    params: dict[str, dict[str, float]] = {
        "nfl": {"sigma_margin": 12.66, "sigma_total": 13.06, "rho": 0.026},
        "nba": {"sigma_margin": 13.71, "sigma_total": 18.42, "rho": 0.000},
        "wnba": {"sigma_margin": 12.04, "sigma_total": 16.55, "rho": -0.019},
    }
    sigma_band_frac: float = 0.05   # FE-vs-line method gap on NFL was ~3%
    rho_band: float = 0.05
    # Normal approximation of discrete scores (NFL key numbers worst):
    # flat probability adder when any margin-flavored leg is present.
    discreteness_unc: dict[str, float] = {"nfl": 0.010, "nba": 0.004, "wnba": 0.005}
    misfit_uncertainty_scale: float = 1.0


class MlbRunsConfig(StrictModel):
    """NegBin runs model for MLB SGPs (pricing/mlb_runs.py). Dispersion k
    calibrated from Retrosheet final scores (tools/calibrate_mlb_runs.py,
    2026-07-06): k=3.62 on 2021-2024, k=3.63 on 2015-2019 — era-stable; the
    band covers the unmodeled home/away asymmetry (k 3.37 away / 3.91 home —
    tickers don't reveal the home side).

    GATE PASSED 2026-07-06 (tools/validate_mlb_runs_oos.py; SBR closing-odds
    archive, k from the 2015-2019 train era, test = 2021 season n=2,351):
    structural beats the shipped v1 copula on all three OOS joint-log-loss
    metrics — hw×over 1.36134 vs 1.36300, hw×runline-cover 1.00824 vs
    1.12151 (v1 has NO calibrated ml|spread for MLB and its flat 0.6 prior
    is badly wrong), triple 1.71126 vs 1.88090. Also measured: v1's pooled
    ml|total −0.05 LOSES to independence OOS (1.36300 vs 1.36209) — the
    runs grid supersedes it for same-game combos. Confirmation re-gate on
    prod-shadow settlements as they accrue."""

    enabled: bool = True
    # 2021-2025 window (Retrosheet gl2025 fetched 2026-07-06); was 3.62 on
    # 2021-2024, 3.63 on 2015-2019 — drift well inside the band.
    dispersion_k: float = 3.54
    k_band: float = 0.30
    misfit_uncertainty_scale: float = 1.0


class SportMarkupConfig(StrictModel):
    """Per-sport maker markup. A FLAT (uniform) markup over fair. Because a taker
    only fills us when the combo clears at >= our ask (= fair + markup), the
    markup itself SELF-SELECTS the FAT tier (room >= markup) and auto-declines
    competitive/NORMAL flow — no room classifier needed for v1. The explicit
    FAT/NORMAL room-predictor tiering slots in later behind the same MarkupPolicy.
    """

    enabled: bool = False
    markup_cc: int = 0  # centi-cents over fair (400 = 4¢); self-selects FAT


class MarkupConfig(StrictModel):
    """Maker profit markup, applied in construct_quote as margin=max(width, markup).
    DARK by default (enabled=False, every sport off) so an un-set markup is
    BIT-IDENTICAL to the pre-markup pricer. Numbers come from POOLED settlement
    evidence (game-clustered lower-CI bound), NEVER a single-window P&L refit
    (feedback_no_refit_on_pnl). WC-FAT is the first validated tier (reality test
    2026-07-13: WC longshot parlays settle 13.8% vs priced 19.6%)."""

    enabled: bool = False  # master switch
    soccer: SportMarkupConfig = Field(default_factory=SportMarkupConfig)
    mlb: SportMarkupConfig = Field(default_factory=SportMarkupConfig)
    # Per-leg-series defensive markup ADDERS (series ticker prefix -> extra cc on
    # top of the sport markup). First use: the #37 corners edge-floor — corners
    # combos measured 3-5c RICH vs our (correct) fair (2026-07-15 rho measurement:
    # corners⊥goals confirmed at every traded line, so the gap is market richness,
    # NOT a correlation error) ⇒ quote them fair + markup + 3c instead of leaking
    # the richness to adverse selection. Applied ONCE per combo (max matching
    # adder, never summed) and ONLY when the combo's sport markup is enabled —
    # a dark sport stays bit-identical dark.
    series_adders_cc: dict[str, int] = Field(default_factory=dict)

    @field_validator("series_adders_cc")
    @classmethod
    def _adders_sane(cls, v: dict[str, int]) -> dict[str, int]:
        for key, cc in v.items():
            if not key:
                raise ValueError("series_adders_cc key must be a non-empty prefix")
            if cc < 0:
                raise ValueError(f"series_adders_cc[{key}] must be >= 0, got {cc}")
        return v


class PricingConfig(StrictModel):
    fee: FeeConfig = Field(default_factory=FeeConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    quote: QuoteConfig = Field(default_factory=QuoteConfig)
    markup: MarkupConfig = Field(default_factory=MarkupConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
    margin_total: MarginTotalConfig = Field(default_factory=MarginTotalConfig)
    mlb_runs: MlbRunsConfig = Field(default_factory=MlbRunsConfig)
    external_odds: ExternalOddsConfig = Field(default_factory=ExternalOddsConfig)
    # Inventory-aware quote skew (Phase 5, R3 Part A). DARK by default: computed
    # + logged every quote but passed as 0 into the pricer (a zero-P&L shadow).
    skew: SkewConfig = Field(default_factory=SkewConfig)
    # Widen-vs-DECLINE policy (Phase 5, R3 Part R2). SHADOW by default: the
    # would-be decision is logged, the quote still goes out.
    widen: WidenConfig = Field(default_factory=WidenConfig)
    max_source_disagreement: float = 0.08


class RiskConfig(StrictModel):
    """Limits + last-look + in-play policies. Enforced pre-quote AND pre-confirm."""

    max_contracts_per_quote: float = 100.0
    max_notional_per_quote_dollars: float = 500.0
    max_market_delta_contracts: float = 300.0
    max_event_delta_contracts: float = 500.0
    max_gross_notional_dollars: float = 5_000.0
    max_open_quotes: int = 20
    max_daily_loss_dollars: float = 500.0
    max_event_worst_case_loss_dollars: float = 1_000.0
    # last look
    leg_move_tolerance_cc: int = 150
    joint_move_tolerance_cc: int = 200
    max_leg_age_s: float = 2.0
    # in-play detection
    velocity_window_s: float = 5.0
    velocity_threshold_cc: int = 300
    update_count_threshold: int = 25
    in_play_cooldown_s: float = 30.0

    # --- R2 %-of-bankroll cap layer (Phase 2). ENFORCED by DEFAULT (wire-live
    # 2026-07-13): the new caps run in parallel with the enforced caps above and
    # now actually block a quote/confirm and arm the give-back KILL. Set
    # caps_shadow_mode: true in YAML to re-SHADOW them (LOG-ONLY, zero quote
    # impact) when comparing a new cap against the tape before enforcing it. The
    # percentages are decimal STRINGS parsed to exact Fractions (the established
    # FeeConfig pattern — YAML can't hold a Fraction and floats are banned for
    # thresholds); thresholds are computed at check time from the live bankroll.
    # Defaults are the researched $2,000 START values
    # (docs/research/CAP_recommendation_2000.md). A fresh demo start with no
    # balance/positions still quotes normally: the %-caps fail closed to a
    # no-quote on a stale bankroll (never a permanent halt) and the give-back
    # halts skip when peak/current equity is unavailable (no invented peak). ---
    caps_shadow_mode: bool = False
    # P0-1 candidate-aware portfolio-risk gate at CONFIRM. ENFORCED by default: a
    # confirm the existing analytic/gross/burst gates already admit runs an
    # ADDITIONAL candidate-aware ~20k-sample portfolio MC (off the loop) and confirms
    # ONLY when the candidate's marginal EV is positive AND the POST-book joint-tail
    # / ruin / deterministic / gross budgets pass. STRICTLY ADDITIVE (only ever
    # DECLINES a fill the other gates admit). Set candidate_gate_enabled: false in
    # YAML to disable it (the kill switch for this gate; preserves prior behaviour).
    candidate_gate_enabled: bool = True
    # P1 EV VISIBILITY (audit "+EV IS PRODUCTION-MODEL EV, NOT ROBUST EV"). The
    # candidate gate always LOGS the production candidate EV alongside the
    # challenger / bridge / split candidate EVs. This OPTIONAL tolerance (float cc)
    # ONLY ADDS a decline: a +production-EV candidate whose WORST credible challenger
    # EV falls below it is declined too. DEFAULTS to −inf ⇒ no behaviour change
    # (worst >= −inf is always true); set a finite NEGATIVE value in YAML (e.g.
    # -50.0 to allow the worst challenger EV down to −0.50 of edge) to opt in.
    # Strictly additive — it can only flip an already-admitted confirm to a decline.
    worst_challenger_ev_tolerance_cc: float = float("-inf")
    game_loss_frac: str = "0.08"          # %-of-GAME correlated loss
    per_combo_loss_frac: str = "0.01"     # single position max_loss
    directional_frac: str = "0.10"        # net one-directional / theme
    slate_loss_frac: str = "0.08"         # Σ game loss over one slate
    daily_loss_frac: str = "0.06"         # soft daily-loss halt
    drawdown_frac: str = "0.10"           # peak-drawdown halt
    hard_trip_frac: str = "0.12"          # hard-trip KILL
    portfolio_cvar_frac: str = "0.15"     # portfolio joint-tail (governing model ES_0.99)
    portfolio_det_max_frac: str = "0.15"  # P0-3: deterministic all-hit max-loss cap
    portfolio_ruin_prob_budget: str = "0.05"  # A2: max P(equity < ruin floor this wave)
    absolute_notional_multiple: int = 3   # utilization backstop (× bankroll)
    fill_velocity_window_s: float = 2.0
    fill_velocity_soft_frac: str = "0.05"
    fill_velocity_hard_frac: str = "0.10"
    fill_velocity_max_fills: int = 8
    # After this many consecutive risk-driven declines with zero quotes issued,
    # the StarvationWatchdog warns (a mis-set cap silently declining everything).
    starvation_threshold: int = 20

    # A cap percentage is a FRACTION of bankroll: it must parse and sit in
    # (0, 1]. This catches the footgun where "8" (a typo meaning 8%) would
    # otherwise parse to Fraction(8) = 800% of bankroll — every position would
    # pass a 800%-of-bankroll cap — or a negative value would breach everything.
    @field_validator(
        "game_loss_frac",
        "per_combo_loss_frac",
        "directional_frac",
        "slate_loss_frac",
        "daily_loss_frac",
        "drawdown_frac",
        "hard_trip_frac",
        "portfolio_cvar_frac",
        "portfolio_det_max_frac",
        "portfolio_ruin_prob_budget",
        "fill_velocity_soft_frac",
        "fill_velocity_hard_frac",
    )
    @classmethod
    def _valid_cap_fraction(cls, v: str) -> str:
        try:
            d = Decimal(v)
        except InvalidOperation as exc:
            raise ValueError(f"cap fraction {v!r} is not a decimal") from exc
        # `not d.is_finite()` catches NaN/sNaN/±Infinity BEFORE the range compare:
        # a signaling-NaN comparison would itself raise decimal.InvalidOperation
        # (an opaque, field-less crash) rather than a clean ValidationError.
        if not d.is_finite() or not (Decimal(0) < d <= Decimal(1)):
            raise ValueError(
                f"cap fraction {v!r} must be a finite fraction in (0, 1] — a "
                f"percentage of bankroll (e.g. '0.08' = 8%), not {d}"
            )
        return v

    @field_validator(
        "absolute_notional_multiple", "fill_velocity_max_fills", "starvation_threshold"
    )
    @classmethod
    def _positive_int_knob(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"must be >= 1, got {v}")
        return v

    def to_risk_limits(self) -> RiskLimits:
        """Build the ``RiskLimits`` the checker uses, parsing the decimal-string
        percentages into exact Fractions (via Decimal so "0.08" is EXACTLY 8/100,
        never a binary-float approximation). The one place config → limits."""
        return RiskLimits(
            max_contracts_per_quote=self.max_contracts_per_quote,
            max_notional_per_quote_dollars=self.max_notional_per_quote_dollars,
            max_market_delta_contracts=self.max_market_delta_contracts,
            max_event_delta_contracts=self.max_event_delta_contracts,
            max_gross_notional_dollars=self.max_gross_notional_dollars,
            max_open_quotes=self.max_open_quotes,
            max_daily_loss_dollars=self.max_daily_loss_dollars,
            max_event_worst_case_loss_dollars=self.max_event_worst_case_loss_dollars,
            caps_shadow_mode=self.caps_shadow_mode,
            game_loss_frac=Fraction(Decimal(self.game_loss_frac)),
            per_combo_loss_frac=Fraction(Decimal(self.per_combo_loss_frac)),
            directional_frac=Fraction(Decimal(self.directional_frac)),
            slate_loss_frac=Fraction(Decimal(self.slate_loss_frac)),
            daily_loss_frac=Fraction(Decimal(self.daily_loss_frac)),
            drawdown_frac=Fraction(Decimal(self.drawdown_frac)),
            hard_trip_frac=Fraction(Decimal(self.hard_trip_frac)),
            portfolio_cvar_frac=Fraction(Decimal(self.portfolio_cvar_frac)),
            portfolio_det_max_frac=Fraction(Decimal(self.portfolio_det_max_frac)),
            portfolio_ruin_prob_budget=Fraction(Decimal(self.portfolio_ruin_prob_budget)),
            absolute_notional_multiple=self.absolute_notional_multiple,
            fill_velocity_window_s=self.fill_velocity_window_s,
            fill_velocity_soft_frac=Fraction(Decimal(self.fill_velocity_soft_frac)),
            fill_velocity_hard_frac=Fraction(Decimal(self.fill_velocity_hard_frac)),
            fill_velocity_max_fills=self.fill_velocity_max_fills,
        )


class ObserveConfig(StrictModel):
    rfq_poll_s: float = 30.0          # REST reconciliation cadence (no seq on WS)
    would_quote_width_cc: int = 600   # stub half-spread total ($0.06) for logging only
    db_filename: str = ""             # "" = auto: combomaker-{env}.sqlite3

    def db_name_for(self, env: Env) -> str:
        # Demo and prod data must never share a store — shadow analytics on
        # prod flow would silently blend with demo bot noise.
        return self.db_filename or f"combomaker-{env.value}.sqlite3"


class AppConfig(StrictModel):
    env: Env = Env.DEMO
    mode: Mode = Mode.OBSERVE
    endpoints: EndpointsConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    breakers: BreakerConfig = Field(default_factory=BreakerConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)
    data_dir: Path = Path("data")
    kill_file: Path = Path("KILL")
    # confirm_live comes only from the CLI flag --confirm-live, never from YAML:
    # a file can't accidentally arm production.
    confirm_live: bool = Field(default=False, exclude=True)
    # The YAML file this config was loaded from (recorded by load_config, never
    # settable from YAML itself). Subprocesses that re-load config (the safety
    # supervisor) must receive THIS path, not re-derive the base per-env file —
    # otherwise local-override values (e.g. supervisor.heartbeat_timeout_s) load
    # in the bot but silently not in the watchdog that enforces them.
    source_path: Path | None = Field(default=None, exclude=True)

    def assert_safe_to_run(self) -> None:
        """Hardcoded production guard (the STATIC go-live gates). Raises
        ProdGuardError on violation. The RUNTIME preflight (supervisor heartbeat
        established, external kill reachable, book reconciled) runs separately at
        startup in QuoteApp — those need live state this static check can't see.
        Everything defaults OFF: prod stays not-live until every gate is green."""
        if self.env is Env.PROD and self.mode is Mode.QUOTE:
            if not self.confirm_live:
                raise ProdGuardError(
                    "quoting on production requires the explicit --confirm-live flag"
                )
            if not self.safety.prod_limits_configured:
                raise ProdGuardError(
                    "quoting on production requires safety.prod_limits_configured: true "
                    "in the prod config, set after limits are reviewed"
                )
            # Phase 6 WHITELIST gate: only whitelisted series may quote on prod.
            # The leg-series allowlist must be present AND non-empty (an empty
            # list or a null disables the per-leg gate — both are unsafe on real
            # money). The collection whitelist (checked in QuoteApp for all quote
            # mode) is a separate, coarser gate; this one is the leg-series one.
            if self.safety.prod_require_series_whitelist:
                allowed = self.filters.allowed_leg_series_prefixes
                if not allowed:
                    raise ProdGuardError(
                        "quoting on production requires a non-empty "
                        "filters.allowed_leg_series_prefixes (the leg-series "
                        "allowlist) — only whitelisted series quote on prod; set "
                        "safety.prod_require_series_whitelist: false to override"
                    )


class ProdGuardError(RuntimeError):
    pass


class ConfigError(ValueError):
    pass


def load_config(
    path: Path,
    *,
    env: Env | None = None,
    mode: Mode | None = None,
    confirm_live: bool = False,
) -> AppConfig:
    """Load YAML config; CLI values override the file's env/mode."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    if "confirm_live" in raw:
        # A file must never be able to arm production; only the CLI flag can.
        raise ConfigError("confirm_live cannot be set from config files; use --confirm-live")

    if env is not None:
        raw["env"] = str(env)
    resolved_env = Env(raw.get("env", Env.DEMO))
    if mode is not None:
        raw["mode"] = str(mode)
    raw.setdefault("endpoints", EndpointsConfig.for_env(resolved_env).model_dump())

    config = AppConfig.model_validate(raw)
    return config.model_copy(update={"confirm_live": confirm_live, "source_path": path})
