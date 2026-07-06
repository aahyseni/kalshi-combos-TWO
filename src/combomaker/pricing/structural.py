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

from combomaker.ops.config import MarginTotalConfig, StructuralConfig
from combomaker.pricing.copula import clamp_to_frechet, frechet_bounds
from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
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
from combomaker.pricing.legtypes import LegType, Sport, classify_leg, classify_sport
from combomaker.pricing.margin_total import (
    GameTotalOver,
    MTLegSpec,
    SportShape,
    TeamWins,
    invert_means,
    region_probability,
)
from combomaker.rfq.models import RfqLeg

_MT_SPORTS = (Sport.NFL, Sport.NBA, Sport.WNBA)

# 26JUL06 (+ optional 4-digit start time) then the concatenated team codes.
_GAME_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z0-9]{4,})$")
_DRAW_SUFFIXES = ("TIE", "DRAW")


@dataclass(frozen=True, slots=True)
class _Match:
    team_a: str
    team_b: str


def _parse_match(game_code: str) -> _Match | None:
    m = _GAME_CODE.match(game_code)
    if m is None:
        return None
    codes = m.group(1)
    if len(codes) % 2 != 0 or len(codes) < 4:
        return None  # can't split unambiguously -> not our problem to guess
    half = len(codes) // 2
    return _Match(team_a=codes[:half], team_b=codes[half:])


def _team_of(code: str, match: _Match) -> Team | None:
    if code == match.team_a:
        return Team.A
    if code == match.team_b:
        return Team.B
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
    string when we cannot be certain."""
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
        if knockout:
            # Kalshi's knockout game market settles on ADVANCING — ET and
            # penalty shootouts included (rules text).
            return Advance(team=team)
        if leg_type is LegType.ADVANCE:
            return "advance market on a non-knockout match"
        return TeamWin(team=team, include_et=False)  # regulation moneyline
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
        team = None
        for code, side in ((match.team_a, Team.A), (match.team_b, Team.B)):
            if player_code.startswith(code):
                team = side
                break
        if team is None:
            return f"player code {player_code!r} matches neither team"
        if not re.fullmatch(r"\d+", goals_raw):
            return f"unparseable goal count {goals_raw!r}"
        # Props settle on the full game incl. ET (pens excluded) by rule.
        return PlayerScores(team=team, min_goals=int(goals_raw), include_et=knockout)
    return f"leg type {leg_type} not representable in the scoreline model"


class StructuralPricer:
    def __init__(
        self, config: StructuralConfig, mt_config: MarginTotalConfig | None = None
    ) -> None:
        self._cfg = config
        self._mt = mt_config or MarginTotalConfig()

    def _match_format(self, ticker: str) -> MatchFormat:
        series = ticker.split("-", 1)[0].upper()
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
            raise StructuralError(f"no structural model for sport {sport}")
        except StructuralError as exc:
            return None, str(exc)

    def _price(
        self, legs: list[RfqLeg], beliefs: list[LegBelief], sides: list[str]
    ) -> JointEstimate:
        cfg = self._cfg

        matches = []
        for leg in legs:
            parts = leg.market_ticker.split("-")
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

        def solve(
            targets: list[tuple[LegSpec, float]],
            *,
            dc_rho: float | None = None,
            et_factor: float | None = None,
            pens: float | None = None,
        ) -> tuple[InvertedModel, float]:
            model = invert(
                targets,
                dc_rho=cfg.dc_rho if dc_rho is None else dc_rho,
                et_factor=cfg.et_factor if et_factor is None else et_factor,
                match_format=fmt,
                max_goals=cfg.max_goals,
                pens_win_a=cfg.pens_win_prob if pens is None else pens,
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
            notes=(
                *base_model.notes,
                f"structural: format={fmt} legs={len(legs)} "
                f"unc(leg={leg_unc:.4f} form={form_unc:.4f} "
                f"pens={pens_unc:.4f} misfit={misfit_unc:.4f})",
            ),
        )


    # --- margin/total sports (NFL, NBA, WNBA) ------------------------------

    def _parse_mt_leg(self, ticker: str, match: _Match) -> MTLegSpec | str:
        parts = ticker.split("-")
        leg_type = classify_leg(ticker)
        if leg_type is LegType.MONEYLINE:
            team = _team_of(parts[-1], match)
            if team is None:
                return f"moneyline suffix {parts[-1]!r} matches neither team"
            return TeamWins(team=team)
        if leg_type is LegType.TOTAL:
            raw = parts[-1]
            if re.fullmatch(r"\d+", raw):
                # "N or more" style integer line: T >= N, continuity-corrected.
                return GameTotalOver(threshold=float(int(raw)) - 0.5)
            if re.fullmatch(r"\d+\.5", raw):
                return GameTotalOver(threshold=float(raw))
            return f"unparseable total line {raw!r}"
        if leg_type is LegType.SPREAD:
            # The ticker does not carry the line's SIGN convention; guessing
            # it wrong silently mirrors every spread quote. Blocked until
            # real spread tickers + rules are observed in season.
            return "spread line sign convention unverified — copula fallback"
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
        shape = SportShape(
            sigma_margin=raw["sigma_margin"],
            sigma_total=raw["sigma_total"],
            rho=raw["rho"],
        )

        matches = []
        for leg in legs:
            parts = leg.market_ticker.split("-")
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

        disc_unc = (
            mt.discreteness_unc.get(str(sport), 0.01)
            if any(isinstance(s, TeamWins) for s in specs)
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
            notes=(
                *base_inv.notes,
                f"structural-mt: sport={sport} legs={len(legs)} "
                f"unc(leg={leg_unc:.4f} form={form_unc:.4f} "
                f"disc={disc_unc:.4f} misfit={misfit_unc:.4f})",
            ),
        )


def structural_applicable(
    legs: list[RfqLeg], same_event_groups: Sequence[Sequence[int]]
) -> bool:
    """Cheap pre-check: a structurally-modeled sport, all legs in ONE
    same-event group."""
    if not legs:
        return False
    sports = {classify_sport(leg.market_ticker) for leg in legs}
    if len(sports) != 1 or sports.pop() not in (Sport.SOCCER, *_MT_SPORTS):
        return False
    groups = [g for g in same_event_groups if len(g) > 1] if same_event_groups else []
    covered = set(groups[0]) if len(groups) == 1 else set()
    return covered == set(range(len(legs)))


__all__ = [
    "ModelParams",
    "StructuralPricer",
    "structural_applicable",
]
