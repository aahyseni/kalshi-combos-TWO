"""Adapter: Kalshi soccer SGP legs -> Dixon-Coles structural JointEstimate.

Owns everything the pure model must not know about: ticker parsing (game
codes, team codes, player-to-team attachment, totals lines), settlement-window
mapping per market family, and the honest-failure contract — ANY leg this
module cannot classify with certainty makes it decline (return a reason),
sending the engine down the v1 copula path. UNKNOWN never silently prices
(quiet-failure defense #2).

Settlement windows (doc: Kalshi rules text, operator-provided 2026-07-06):
  - knockout GAME market = which team ADVANCES: ET and penalty shootouts
    included -> Advance spec (pens as a fractional factor, prob banded);
  - Regulation-Time Moneyline / Spread / Total / BTTS / Team Total / Correct
    Score settle at the END OF REGULATION (90' + stoppage) -> include_et=False
    ALWAYS for BTTS and totals, and for group-stage moneylines;
  - all other props (player goals) settle on the FULL GAME including ET,
    pens excluded -> include_et=True in knockouts (our ET stage has no pens,
    matching the rule exactly).

Uncertainty is priced by perturbation, all through re-inversion so the model
keeps hitting the (perturbed) market marginals:
  - each leg's marginal band  -> re-invert at p +/- unc, sum |d joint|
  - model form (DC rho, ET intensity) -> re-invert at the band edges
  - shootout probability (Advance legs only) -> re-invert at 0.5 +/- band
  - over-identification residual -> misfit scaled straight into width

Ticker shapes handled (grounded in observed KXWC/KXUCL/KXEPL series):
  KXWCGAME-26JUL06ENGNOR-ENG        moneyline (suffix = team code, or TIE)
  KXWCBTTS-26JUL10ENGNOR[-BTTS]     both teams to score
  KXWCTOTAL-26JUL10ENGNOR-3         total goals >= 3 ("2.5" style lines too)
  KXWCGOAL-26JUL05MEXENG-ENGHKANE9-1  player scores >= 1 (team = code prefix)
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from combomaker.ops.config import MarginTotalConfig, MlbRunsConfig, StructuralConfig
from combomaker.pricing.copula import clamp_to_frechet, frechet_bounds
from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
    GoalSpread,
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
    InvertedModel,
    LegSpec,
    MatchFormat,
    ModelParams,
    PlayerScores,
    StructuralError,
    Team,
    TeamWin,
    TotalOver,
    invert,
    joint_probability,
)
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.legtypes import (
    LegType,
    Sport,
    classify_leg,
    classify_sport,
    is_period_leg,
    resolve_pricing_alias,
)
from combomaker.pricing.margin_total import (
    GameTotalOver,
    MTLegSpec,
    SportShape,
    SpreadCover,
    TeamWins,
    invert_means,
    region_probability,
    shape_in_leg_frame,
)
from combomaker.pricing.mlb_runs import MlbShape, invert_runs
from combomaker.pricing.mlb_runs import joint_probability as mlb_joint
from combomaker.rfq.models import RfqLeg

_MT_SPORTS = (Sport.NFL, Sport.NBA, Sport.WNBA)

# The 1H scoreline specs (adapter-side, for the has-half gate on the h-band).
_HALF_SPECS = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)

# Soccer first-half families we SHIP structurally: GOAL-TIMING legs (1H total,
# 1H BTTS). The OOS gate (tools/validate_halftime_dc_oos.py) shows the DC half
# split reproduces their held-out 1H×FT conditionals within tolerance
# (P(FT-over|1H-over) diff ~0.01, base rates ~0.01).
_MODELED_FIRST_HALF = frozenset(
    {LegType.FIRST_HALF_TOTAL, LegType.FIRST_HALF_BTTS}
)

# 1H RESULT/MARGIN families (1H winner, 1H spread): representable, but the
# independent-increment split OVER-states 1H→FT result persistence — OOS gate
# P(FT-win|1H-lead) model 0.81 vs empirical 0.75 (~6pt), and NO first-half share
# h fixes it (it's the missing negative inter-half serial correlation). The
# copula carries the DIRECTLY-measured, era-stable first_half_moneyline|moneyline
# (+0.71/−0.67) and first_half_spread priors (results_soccer.md), which price
# that pathway better — so these legs DEFER to the copula (fail-closed).
_DEFERRED_FIRST_HALF = frozenset(
    {LegType.FIRST_HALF_MONEYLINE, LegType.FIRST_HALF_SPREAD}
)


def _is_modeled_first_half(ticker: str) -> bool:
    """True iff ``ticker`` is a soccer first-half leg we price structurally (a
    goal-timing 1H total / 1H BTTS that PASSES the OOS gate). A 1H result/spread
    leg, a 2H/quarter leg, or a non-soccer half returns False so it stays on the
    copula (fail-closed)."""
    return (
        classify_sport(ticker) is Sport.SOCCER
        and classify_leg(ticker) in _MODELED_FIRST_HALF
    )

# 26JUL06 (+ optional 4-digit start time) then the concatenated team codes.
_GAME_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z0-9]{4,})$")
_DRAW_SUFFIXES = ("TIE", "DRAW")

# MLB doubleheaders append a game-number token to the team-code blob (prod RFQ
# tape: KXMLBGAME-26JUL071835MILSTLG1-STL, blob "MILSTLG1" = MIL + STL + "G1").
# Without stripping it the blob ends in "G1" not "STL", so _team_of anchors the
# prefix team but NOT the suffix team → a leg naming the second team declines.
# The token is "G" + a single digit; strip it ONLY when the remainder is a pure
# ALPHA blob of >=4 chars (a valid two-team blob). Team codes are always
# alphabetic, so the sole source of a digit in the blob is this doubleheader
# suffix — the pure-alpha remainder guard means a legitimate alphanumeric code
# can never be truncated, preserving the fail-closed contract for real shapes.
_DOUBLEHEADER_SUFFIX = re.compile(r"([A-Z]{4,})G\d")


def _strip_doubleheader_suffix(blob: str) -> str:
    m = _DOUBLEHEADER_SUFFIX.fullmatch(blob)
    return m.group(1) if m is not None else blob


@dataclass(frozen=True, slots=True)
class _Match:
    """The concatenated team-code blob from the game code. Team codes vary in
    length (PHIKC = PHI+KC, CONNMIN = CONN+MIN, ENGNOR = ENG+NOR), so teams
    are resolved by anchoring at the ends: a code that prefixes the blob is
    team A, one that suffixes it is team B — ambiguity refuses."""

    code: str


def _parse_match(game_code: str) -> _Match | None:
    m = _GAME_CODE.match(game_code)
    if m is None:
        return None
    codes = _strip_doubleheader_suffix(m.group(1))
    if len(codes) < 4:
        return None
    return _Match(code=codes)


def _team_of(code: str, match: _Match) -> Team | None:
    if len(code) < 2 or len(code) >= len(match.code):
        return None
    is_a = match.code.startswith(code)
    is_b = match.code.endswith(code)
    if is_a == is_b:  # neither, or both (pathological palindrome): refuse
        return None
    return Team.A if is_a else Team.B


def _player_team(player_code: str, match: _Match) -> Team | None:
    """Player codes prefix their team code (ENGHKANE9, KCBWITT7): try the
    longest leading fragment that anchors to either end of the game code."""
    for length in range(min(4, len(player_code) - 1), 1, -1):
        team = _team_of(player_code[:length], match)
        if team is not None:
            return team
    return None


def _parse_total_line(raw: str) -> int | None:
    """'3' -> 3; '2.5' -> 3 (over 2.5 == at least 3). Anything else: None."""
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    if re.fullmatch(r"\d+\.5", raw):
        return int(raw.split(".")[0]) + 1
    return None


def _parse_leg(ticker: str, match: _Match, *, fmt: MatchFormat) -> LegSpec | str:
    """One leg's spec (with its rule-book settlement window), or a reason
    string when we cannot be certain. Pricing aliases resolve HERE (the shared
    parse boundary): the pricing adapter AND the risk MC's game plans
    (``sim.structural_book`` reuses this helper) settle an aliased champion
    leg identically — never one without the other."""
    ticker = resolve_pricing_alias(ticker)
    parts = ticker.split("-")
    leg_type = classify_leg(ticker)
    knockout = fmt is MatchFormat.KNOCKOUT
    if leg_type in (LegType.MONEYLINE, LegType.ADVANCE):
        suffix = parts[-1]
        if suffix in _DRAW_SUFFIXES:
            return Draw()  # regulation 3-way draw — same window either format
        team = _team_of(suffix, match)
        if team is None:
            return f"moneyline suffix {suffix!r} matches neither team"
        if leg_type is LegType.ADVANCE:
            # KXWCADVANCE: which team advances — ET AND pens included.
            if not knockout:
                return "advance market on a non-knockout match"
            return Advance(team=team)
        # KXWCGAME coexists with KXWCADVANCE on the same knockout matches
        # (live tape 2026-07-06), so GAME is the Regulation Time Moneyline
        # family: settles at the end of regulation in BOTH formats.
        return TeamWin(team=team, include_et=False)
    if leg_type is LegType.BTTS:
        return Btts(include_et=False)  # regulation-time market by rule
    if leg_type is LegType.TOTAL:
        line = _parse_total_line(parts[-1])
        if line is None:
            return f"unparseable total line {parts[-1]!r}"
        return TotalOver(min_total=line, include_et=False)  # regulation by rule
    if leg_type is LegType.PLAYER_GOAL:
        if len(parts) < 4:
            return "player ticker too short to carry a team code"
        player_code, goals_raw = parts[-2], parts[-1]
        team = _player_team(player_code, match)
        if team is None:
            return f"player code {player_code!r} matches neither team"
        if not re.fullmatch(r"\d+", goals_raw):
            return f"unparseable goal count {goals_raw!r}"
        # Props settle on the full game incl. ET (pens excluded) by rule.
        return PlayerScores(team=team, min_goals=int(goals_raw), include_et=knockout)
    if leg_type is LegType.SPREAD:
        # DOC-VERIFIED convention (live market metadata 2026-07-06, sister
        # sports KXMLBSPREAD/KXNFLSPREAD): suffix TEAMn = "TEAM wins by over
        # n-0.5" -> integer goal margin >= n (team-anchored, always positive,
        # no sign ambiguity). Regulation-time (90') by rule -> include_et=False.
        # A spread NAMES a team, so it also resolves DC orientation. Fail-closed
        # on any format mismatch (-> copula), so an unverified soccer spread
        # ticker shape can never mis-price -- it just declines.
        m = re.fullmatch(r"([A-Z]+?)(\d+)", parts[-1])
        if m is None:
            return f"unparseable spread suffix {parts[-1]!r}"
        team = _team_of(m.group(1), match)
        if team is None:
            return f"spread team {m.group(1)!r} matches neither team"
        return GoalSpread(team=team, min_margin=int(m.group(2)), include_et=False)
    # --- first-half (1H) families: modeled on the half-time sub-scoreline (DC
    # half split). Ground-truth ticker shapes (prod RFQ tape 2026-07-07):
    #   KXWC1H-<game>-<TEAM|TIE>     1H moneyline / draw
    #   KXWC1HTOTAL-<game>-<N>       1H goals >= N (…-1 = over 0.5, …-3 = over 2.5)
    #   KXWC1HBTTS-<game>-BTTS       both teams score in the 1H
    #   KXWC1HSPREAD-<game>-<TEAM>N  1H margin >= N (…-BEL2 = BEL leads by >1.5)
    # 1H legs settle at 45' — no ET window (design §3.4), so no include_et.
    if leg_type is LegType.FIRST_HALF_MONEYLINE:
        suffix = parts[-1]
        if suffix in _DRAW_SUFFIXES:
            return HalfDraw()
        team = _team_of(suffix, match)
        if team is None:
            return f"1H moneyline suffix {suffix!r} matches neither team"
        return HalfResult(team=team)
    if leg_type is LegType.FIRST_HALF_TOTAL:
        line = _parse_total_line(parts[-1])
        if line is None:
            return f"unparseable 1H total line {parts[-1]!r}"
        return HalfTotalOver(min_total=line)
    if leg_type is LegType.FIRST_HALF_BTTS:
        return HalfBtts()
    if leg_type is LegType.FIRST_HALF_SPREAD:
        m = re.fullmatch(r"([A-Z]+?)(\d+)", parts[-1])
        if m is None:
            return f"unparseable 1H spread suffix {parts[-1]!r}"
        team = _team_of(m.group(1), match)
        if team is None:
            return f"1H spread team {m.group(1)!r} matches neither team"
        return HalfGoalSpread(team=team, min_margin=int(m.group(2)))
    return f"leg type {leg_type} not representable in the scoreline model"


class StructuralPricer:
    def __init__(
        self,
        config: StructuralConfig,
        mt_config: MarginTotalConfig | None = None,
        mlb_config: MlbRunsConfig | None = None,
    ) -> None:
        self._cfg = config
        self._mt = mt_config or MarginTotalConfig()
        self._mlb = mlb_config or MlbRunsConfig()

    def _match_format(self, ticker: str) -> MatchFormat:
        series = resolve_pricing_alias(ticker).split("-", 1)[0].upper()
        for prefix in self._cfg.knockout_series:
            if series.startswith(prefix.upper()):
                return MatchFormat.KNOCKOUT
        return MatchFormat.GROUP

    def try_price(
        self,
        legs: list[RfqLeg],
        beliefs: list[LegBelief],
        sides: list[str],
    ) -> tuple[JointEstimate | None, str | None]:
        """(estimate, None) on success; (None, reason) -> copula fallback."""
        try:
            # Period legs (1H/2H/quarter) rejoin the same-game copula group
            # (relationships._game_key), which makes this path REACHABLE for a
            # combo carrying one. Soccer GOAL-TIMING first-half legs (1H total /
            # BTTS) are modeled on the DC half-split scoreline and price
            # structurally; 1H result/spread legs DEFER to the copula's measured
            # prior (persistence over-stated — OOS gate), and every other period
            # leg (2H, quarters, non-soccer half) has no scoreline window.
            for leg in legs:
                ticker = leg.market_ticker
                if is_period_leg(ticker) and not _is_modeled_first_half(ticker):
                    if (
                        classify_sport(ticker) is Sport.SOCCER
                        and classify_leg(ticker) in _DEFERRED_FIRST_HALF
                    ):
                        raise StructuralError(
                            f"1H result/spread leg {ticker}: independent-increment "
                            "persistence over-stated (OOS gate) — copula carries "
                            "the measured first_half prior"
                        )
                    raise StructuralError(
                        f"unmodeled period leg ({ticker}): no scoreline window"
                    )
            sports = {classify_sport(leg.market_ticker) for leg in legs}
            if len(sports) != 1:
                raise StructuralError("legs span multiple sports")
            sport = sports.pop()
            if sport is Sport.SOCCER:
                if not self._cfg.enabled:
                    raise StructuralError("soccer structural pricer disabled")
                return self._price(legs, beliefs, sides), None
            if sport in _MT_SPORTS:
                if str(sport) not in self._mt.enabled_sports:
                    raise StructuralError(f"{sport} margin-total pricer not gated on")
                return self._price_margin_total(sport, legs, beliefs, sides), None
            if sport is Sport.MLB:
                if not self._mlb.enabled:
                    raise StructuralError("mlb runs pricer not gated on")
                return self._price_mlb(legs, beliefs, sides), None
            raise StructuralError(f"no structural model for sport {sport}")
        except StructuralError as exc:
            return None, str(exc)

    def _price(
        self, legs: list[RfqLeg], beliefs: list[LegBelief], sides: list[str]
    ) -> JointEstimate:
        cfg = self._cfg

        matches = []
        for leg in legs:
            parts = resolve_pricing_alias(leg.market_ticker).split("-")
            if len(parts) < 2:
                raise StructuralError(f"malformed ticker {leg.market_ticker!r}")
            match = _parse_match(parts[1])
            if match is None:
                raise StructuralError(f"unparseable game code {parts[1]!r}")
            matches.append(match)
        match = matches[0]
        if any(m != match for m in matches):
            raise StructuralError("legs reference different matches")

        fmt = self._match_format(legs[0].market_ticker)
        specs: list[LegSpec] = []
        for leg in legs:
            spec = _parse_leg(leg.market_ticker, match, fmt=fmt)
            if isinstance(spec, str):
                raise StructuralError(f"{leg.market_ticker}: {spec}")
            specs.append(spec)

        constraints = [(spec, b.p) for spec, b in zip(specs, beliefs, strict=True)]
        selected = [(spec, side == "yes") for spec, side in zip(specs, sides, strict=True)]

        warm: tuple[float, float] | None = None

        has_half = any(isinstance(s, _HALF_SPECS) for s in specs)

        def solve(
            targets: list[tuple[LegSpec, float]],
            *,
            dc_rho: float | None = None,
            et_factor: float | None = None,
            pens: float | None = None,
            half_share: float | None = None,
        ) -> tuple[InvertedModel, float]:
            model = invert(
                targets,
                dc_rho=cfg.dc_rho if dc_rho is None else dc_rho,
                et_factor=cfg.et_factor if et_factor is None else et_factor,
                match_format=fmt,
                max_goals=cfg.max_goals,
                pens_win_a=cfg.pens_win_prob if pens is None else pens,
                half_share=cfg.half_share if half_share is None else half_share,
                warm_start=warm,  # perturbation solves start at the base fit
            )
            return model, joint_probability(model.params, selected, model.shares)

        base_model, p = solve(constraints)
        warm = (base_model.params.lam_a, base_model.params.lam_b)

        # 1. Leg-marginal bands, propagated by re-inversion (worst side each).
        leg_unc = 0.0
        for i, belief in enumerate(beliefs):
            deltas = []
            for shifted in (belief.p + belief.uncertainty, belief.p - belief.uncertainty):
                bumped = list(constraints)
                bumped[i] = (specs[i], min(0.999, max(0.001, shifted)))
                try:
                    deltas.append(abs(solve(bumped)[1] - p))
                except StructuralError:
                    continue  # a band edge outside the representable range
            if not deltas:
                raise StructuralError(
                    f"marginal band of leg {i} leaves the invertible range"
                )
            leg_unc += max(deltas)

        # 2. Model form: DC rho and ET intensity band edges (ET only matters
        #    in knockout format).
        form_probes: list[float] = []
        for rho in (cfg.dc_rho - cfg.dc_rho_band, cfg.dc_rho + cfg.dc_rho_band):
            try:
                form_probes.append(solve(constraints, dc_rho=rho)[1])
            except StructuralError:
                continue
        if fmt is MatchFormat.KNOCKOUT:
            for et in (cfg.et_factor_low, cfg.et_factor_high):
                try:
                    form_probes.append(solve(constraints, et_factor=et)[1])
                except StructuralError:
                    continue
        # First-half goal share h: a banded CONSTANT (never inverted, design §6);
        # its ±band re-prices the joint so the priced width absorbs the share
        # uncertainty PLUS the residual inter-half serial correlation the
        # independent-increment split omits (§7). Only exercised when a 1H leg
        # is present (FT-only combos never build the half grid).
        if has_half:
            for hh in (
                cfg.half_share - cfg.half_share_band,
                cfg.half_share + cfg.half_share_band,
            ):
                try:
                    form_probes.append(solve(constraints, half_share=hh)[1])
                except StructuralError:
                    continue
        form_unc = max((abs(fp - p) for fp in form_probes), default=0.0)

        # 3. Shootout probability: only Advance legs depend on it; 0.5 is the
        #    prior (slight first-kicker/keeper effects exist) — re-invert at
        #    the band edges so the advance marginal keeps its market price.
        pens_unc = 0.0
        if any(isinstance(s, Advance) for s in specs):
            probes = []
            for pw in (cfg.pens_win_prob - cfg.pens_band, cfg.pens_win_prob + cfg.pens_band):
                try:
                    probes.append(solve(constraints, pens=pw)[1])
                except StructuralError:
                    continue
            pens_unc = max((abs(pp - p) for pp in probes), default=0.0)

        misfit_unc = base_model.residual * cfg.misfit_uncertainty_scale
        uncertainty = leg_unc + form_unc + pens_unc + misfit_unc

        marginals = [
            b.p if yes else 1.0 - b.p
            for b, (_, yes) in zip(beliefs, selected, strict=True)
        ]
        lo, hi = frechet_bounds(marginals)
        return JointEstimate(
            p=clamp_to_frechet(p, marginals),
            uncertainty=uncertainty,
            frechet_lo=lo,
            frechet_hi=hi,
            residual=base_model.residual,
            notes=(
                *base_model.notes,
                f"structural: format={fmt} legs={len(legs)}"
                + (f" half_share={cfg.half_share}" if has_half else "")
                + f" unc(leg={leg_unc:.4f} form={form_unc:.4f} "
                f"pens={pens_unc:.4f} misfit={misfit_unc:.4f})",
            ),
        )


    # --- margin/total sports (NFL, NBA, WNBA) ------------------------------

    def _parse_mt_leg(self, ticker: str, match: _Match) -> MTLegSpec | str:
        # Alias-resolved for symmetry with _parse_leg (verify follow-up
        # 2026-07-16): inert today (aliases target soccer), but a future alias
        # onto an MT sport would otherwise read team/line off the raw suffix.
        parts = resolve_pricing_alias(ticker).split("-")
        leg_type = classify_leg(ticker)
        if leg_type is LegType.MONEYLINE:
            team = _team_of(parts[-1], match)
            if team is None:
                return f"moneyline suffix {parts[-1]!r} matches neither team"
            return TeamWins(team=team)
        if leg_type is LegType.TOTAL:
            raw = parts[-1]
            # DOC-VERIFIED (live market metadata 2026-07-06): integer suffix N
            # means "over N-0.5" (KXMLBTOTAL-...-5 = 'Over 4.5 runs scored',
            # KXWNBATOTAL-...-175 = 'Over 174.5 points scored').
            if re.fullmatch(r"\d+", raw):
                return GameTotalOver(threshold=float(int(raw)) - 0.5)
            if re.fullmatch(r"\d+\.5", raw):
                return GameTotalOver(threshold=float(raw))
            return f"unparseable total line {raw!r}"
        if leg_type is LegType.SPREAD:
            # DOC-VERIFIED (live market metadata 2026-07-06): suffix TEAMn
            # means "TEAM wins by over n-0.5" (KXMLBSPREAD-...-BOS4 = 'Boston
            # wins by over 3.5 runs') — team-anchored, always positive, no
            # sign ambiguity.
            m = re.fullmatch(r"([A-Z]+?)(\d+)", parts[-1])
            if m is None:
                return f"unparseable spread suffix {parts[-1]!r}"
            team = _team_of(m.group(1), match)
            if team is None:
                return f"spread team {m.group(1)!r} matches neither team"
            return SpreadCover(team=team, line=float(int(m.group(2))) - 0.5)
        return f"leg type {leg_type} not representable in the margin/total model"

    def _price_margin_total(
        self,
        sport: Sport,
        legs: list[RfqLeg],
        beliefs: list[LegBelief],
        sides: list[str],
    ) -> JointEstimate:
        mt = self._mt
        raw = mt.params.get(str(sport))
        if raw is None:
            raise StructuralError(f"no calibrated shape for {sport}")
        # config rho is CALIBRATION-frame (home - away); the leg specs put
        # Team.A = blob prefix = away, so the shape is built in the leg frame
        # (rho negated). See margin_total.shape_in_leg_frame.
        shape = shape_in_leg_frame(
            raw["sigma_margin"], raw["sigma_total"], raw["rho"]
        )

        matches = []
        for leg in legs:
            parts = resolve_pricing_alias(leg.market_ticker).split("-")
            if len(parts) < 2:
                raise StructuralError(f"malformed ticker {leg.market_ticker!r}")
            match = _parse_match(parts[1])
            if match is None:
                raise StructuralError(f"unparseable game code {parts[1]!r}")
            matches.append(match)
        match = matches[0]
        if any(m != match for m in matches):
            raise StructuralError("legs reference different games")

        specs: list[MTLegSpec] = []
        for leg in legs:
            spec = self._parse_mt_leg(leg.market_ticker, match)
            if isinstance(spec, str):
                raise StructuralError(f"{leg.market_ticker}: {spec}")
            specs.append(spec)

        constraints = [(spec, b.p) for spec, b in zip(specs, beliefs, strict=True)]
        selected = [(spec, side == "yes") for spec, side in zip(specs, sides, strict=True)]

        def solve(
            targets: list[tuple[MTLegSpec, float]],
            use_shape: SportShape,
            warm: tuple[float, float] | None,
        ) -> tuple[float, float]:
            inv = invert_means(targets, use_shape, warm_start=warm)
            return (
                region_probability(inv.mu_m, inv.mu_t, use_shape, selected),
                inv.residual,
            )

        base_inv = invert_means(constraints, shape)
        p = region_probability(base_inv.mu_m, base_inv.mu_t, shape, selected)
        warm = (base_inv.mu_m, base_inv.mu_t)

        leg_unc = 0.0
        for i, belief in enumerate(beliefs):
            deltas = []
            for shifted in (belief.p + belief.uncertainty, belief.p - belief.uncertainty):
                bumped = list(constraints)
                bumped[i] = (specs[i], min(0.999, max(0.001, shifted)))
                try:
                    deltas.append(abs(solve(bumped, shape, warm)[0] - p))
                except StructuralError:
                    continue
            if not deltas:
                raise StructuralError(f"marginal band of leg {i} leaves invertible range")
            leg_unc += max(deltas)

        form_probes: list[float] = []
        f = mt.sigma_band_frac
        for probe_shape in (
            SportShape(shape.sigma_margin * (1 + f), shape.sigma_total, shape.rho),
            SportShape(shape.sigma_margin * (1 - f), shape.sigma_total, shape.rho),
            SportShape(shape.sigma_margin, shape.sigma_total * (1 + f), shape.rho),
            SportShape(shape.sigma_margin, shape.sigma_total * (1 - f), shape.rho),
            SportShape(shape.sigma_margin, shape.sigma_total, min(0.99, shape.rho + mt.rho_band)),
            SportShape(shape.sigma_margin, shape.sigma_total, max(-0.99, shape.rho - mt.rho_band)),
        ):
            try:
                form_probes.append(solve(constraints, probe_shape, warm)[0])
            except StructuralError:
                continue
        form_unc = max((abs(fp - p) for fp in form_probes), default=0.0)

        # Discreteness band applies to any MARGIN-flavored leg (config comment:
        # "when any margin-flavored leg is present"). A SpreadCover tests the
        # margin crossing a specific line and is MORE key-number-sensitive than
        # a moneyline (which only tests margin>0), so it must not be excluded.
        disc_unc = (
            mt.discreteness_unc.get(str(sport), 0.01)
            if any(isinstance(s, (TeamWins, SpreadCover)) for s in specs)
            else 0.0
        )
        misfit_unc = base_inv.residual * mt.misfit_uncertainty_scale
        uncertainty = leg_unc + form_unc + disc_unc + misfit_unc

        marginals = [
            b.p if yes else 1.0 - b.p
            for b, (_, yes) in zip(beliefs, selected, strict=True)
        ]
        lo, hi = frechet_bounds(marginals)
        return JointEstimate(
            p=clamp_to_frechet(p, marginals),
            uncertainty=uncertainty,
            frechet_lo=lo,
            frechet_hi=hi,
            residual=base_inv.residual,
            notes=(
                *base_inv.notes,
                f"structural-mt: sport={sport} legs={len(legs)} "
                f"unc(leg={leg_unc:.4f} form={form_unc:.4f} "
                f"disc={disc_unc:.4f} misfit={misfit_unc:.4f})",
            ),
        )


    # --- MLB (NegBin runs grid) --------------------------------------------

    def _price_mlb(
        self, legs: list[RfqLeg], beliefs: list[LegBelief], sides: list[str]
    ) -> JointEstimate:
        cfg = self._mlb
        shape = MlbShape(dispersion_k=cfg.dispersion_k)

        matches = []
        for leg in legs:
            parts = resolve_pricing_alias(leg.market_ticker).split("-")
            if len(parts) < 2:
                raise StructuralError(f"malformed ticker {leg.market_ticker!r}")
            match = _parse_match(parts[1])
            if match is None:
                raise StructuralError(f"unparseable game code {parts[1]!r}")
            matches.append(match)
        match = matches[0]
        if any(m != match for m in matches):
            raise StructuralError("legs reference different games")

        specs: list[MTLegSpec] = []
        for leg in legs:
            spec = self._parse_mt_leg(leg.market_ticker, match)
            if isinstance(spec, str):
                raise StructuralError(f"{leg.market_ticker}: {spec}")
            specs.append(spec)

        constraints = [(spec, b.p) for spec, b in zip(specs, beliefs, strict=True)]
        selected = [(spec, side == "yes") for spec, side in zip(specs, sides, strict=True)]

        warm: tuple[float, float] | None = None

        def solve(
            targets: list[tuple[MTLegSpec, float]], k: float
        ) -> tuple[float, float]:
            inv = invert_runs(targets, MlbShape(dispersion_k=k), warm_start=warm)
            return (
                mlb_joint(inv.mu_a, inv.mu_b, MlbShape(dispersion_k=k), selected),
                inv.residual,
            )

        base = invert_runs(constraints, shape)
        p = mlb_joint(base.mu_a, base.mu_b, shape, selected)
        warm = (base.mu_a, base.mu_b)

        leg_unc = 0.0
        for i, belief in enumerate(beliefs):
            deltas = []
            for shifted in (belief.p + belief.uncertainty, belief.p - belief.uncertainty):
                bumped = list(constraints)
                bumped[i] = (specs[i], min(0.999, max(0.001, shifted)))
                try:
                    deltas.append(abs(solve(bumped, cfg.dispersion_k)[0] - p))
                except StructuralError:
                    continue
            if not deltas:
                raise StructuralError(f"marginal band of leg {i} leaves invertible range")
            leg_unc += max(deltas)

        form_probes: list[float] = []
        for k in (cfg.dispersion_k - cfg.k_band, cfg.dispersion_k + cfg.k_band):
            try:
                form_probes.append(solve(constraints, k)[0])
            except StructuralError:
                continue
        form_unc = max((abs(fp - p) for fp in form_probes), default=0.0)
        misfit_unc = base.residual * cfg.misfit_uncertainty_scale
        uncertainty = leg_unc + form_unc + misfit_unc

        marginals = [
            b.p if yes else 1.0 - b.p
            for b, (_, yes) in zip(beliefs, selected, strict=True)
        ]
        lo, hi = frechet_bounds(marginals)
        return JointEstimate(
            p=clamp_to_frechet(p, marginals),
            uncertainty=uncertainty,
            frechet_lo=lo,
            frechet_hi=hi,
            residual=base.residual,
            notes=(
                *base.notes,
                f"structural-mlb: legs={len(legs)} "
                f"unc(leg={leg_unc:.4f} form={form_unc:.4f} misfit={misfit_unc:.4f})",
            ),
        )


def structural_applicable(
    legs: list[RfqLeg], same_event_groups: Sequence[Sequence[int]]
) -> bool:
    """Cheap pre-check: a structurally-modeled sport, all legs in ONE
    same-event group, and every period leg a MODELED soccer first-half leg (the
    DC half split covers those; any other period — 2H, quarters, non-soccer
    half — has no scoreline window and must stay in the copula)."""
    if not legs:
        return False
    if any(
        is_period_leg(leg.market_ticker)
        and not _is_modeled_first_half(leg.market_ticker)
        for leg in legs
    ):
        return False
    sports = {classify_sport(leg.market_ticker) for leg in legs}
    if len(sports) != 1 or sports.pop() not in (Sport.SOCCER, Sport.MLB, *_MT_SPORTS):
        return False
    groups = [g for g in same_event_groups if len(g) > 1] if same_event_groups else []
    covered = set(groups[0]) if len(groups) == 1 else set()
    return covered == set(range(len(legs)))


__all__ = [
    "ModelParams",
    "StructuralPricer",
    "structural_applicable",
]
