"""Maker markup policy: maps a combo's sport to its markup (centi-cents over fair).

v1 is a FLAT per-sport markup that self-selects the FAT tier via the market — a
taker only fills us when the combo clears at >= our ask (= fair + markup), i.e.
room >= markup, which IS the FAT flow. Competitive/NORMAL flow (room < markup)
just doesn't fill us. So no room classifier is needed for the first outing.

Designed to extend: the explicit FAT/NORMAL room predictor, per-tier markup, and
online adaptation all slot in behind ``markup_cc`` without changing the engine
seam (engine passes ``markup_cc`` into ``construct_quote``; margin = max(width,
markup)).

DARK unless ``MarkupConfig.enabled`` AND the sport is enabled — otherwise 0, and
the pricer is then bit-identical to pre-markup. An UNKNOWN/other sport returns 0
(never invents a markup); fail-safe widen/decline lives in the engine's own
UNKNOWN branches, not here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from combomaker.pricing.legtypes import resolve_pricing_alias

if TYPE_CHECKING:  # avoid a runtime pricing->ops import cycle; config is duck-typed
    from combomaker.ops.config import MarkupConfig


def _is_cross_game_ml_parlay(leg_tickers: list[str]) -> bool:
    """True iff EVERY leg is a MONEYLINE on a DISTINCT game — the popular
    cross-game ML parlay shape the ml_parlay_cc override prices (2026-08-16).
    Game identity = the ticker's middle segment (SERIES-GAMECODE-OUTCOME,
    the exchange's own convention). Fail-safe False on: fewer than 2 legs,
    any non-MONEYLINE leg, any unparseable ticker, any repeated game code
    (same-game/mutex shapes keep full tiers)."""
    from combomaker.pricing.legtypes import LegType, classify_leg

    if len(leg_tickers) < 2:
        return False
    games: set[str] = set()
    for ticker in leg_tickers:
        if classify_leg(ticker) is not LegType.MONEYLINE:
            return False
        parts = resolve_pricing_alias(ticker).split("-")
        if len(parts) < 3:
            return False
        games.add(parts[1])
    return len(games) == len(leg_tickers)


def _leg_sport(ticker: str) -> str:
    # Pricing aliases apply (2026-07-16): an aliased champion leg tags as its
    # structural equivalent's sport — otherwise a KXMENWORLDCUP leg tagged
    # 'other' and the whole combo quoted with ZERO markup (observed live).
    ticker = resolve_pricing_alias(ticker)
    if ticker.startswith("KXWC"):
        return "soccer"
    # CLUB SOCCER (2026-08-13 operator wire; EXPANDED 2026-08-15 "wire more
    # soccer leagues" — Liga MX / EFL Championship / UCL / EPL + the
    # classified-but-not-allowlisted CHNSL/CLUBF/ENGCS so no soccer leg can
    # ever ride 'other' at ZERO markup, the KXMENWORLDCUP incident class):
    # all ride the soccer markup tier — same 90'-regulation families as KXWC
    # (rules pin: docs/calibration/club_soccer_rules_pin.md); the allowlist
    # still governs admission. "KXMLS" != "KXMLB" at char 4 — no collision.
    if ticker.startswith(
        (
            "KXLALIGA",
            "KXMLS",
            "KXUECL",
            "KXLIGAMX",
            "KXEFLCHAMPIONSHIP",
            "KXUCL",
            "KXEPL",
            "KXCHNSL",
            "KXCLUBF",
            "KXENGCS",
            # 2026-08-16 operator wire (Serie A / Ligue 1) + Bundesliga/
            # Brasileiro future-proofing — all classified soccer in
            # legtypes; mapped here so none can ever ride 'other' at ZERO
            # markup (the KXMENWORLDCUP incident class).
            "KXSERIEA",
            "KXLIGUE1",
            "KXBUNDESLIGA",
            "KXBRASILEIRO",
        )
    ):
        return "soccer"
    if ticker.startswith("KXMLB"):
        return "mlb"
    # ESPORTS / RACING (2026-07-26 operator wire). Tape-verified prefixes:
    # KXLOLGAME, KXF1RACE, KXNASCARRACE (CS/VALORANT/DOTA mapped ahead of
    # any flow appearing — the allowlist still governs admission).
    if ticker.startswith(("KXLOL", "KXCSGO", "KXCS2", "KXVALORANT", "KXDOTA")):
        return "esports"
    if ticker.startswith(("KXF1", "KXNASCAR")):
        return "racing"
    return "other"


def sport_of(leg_tickers: Iterable[str]) -> str:
    """Sport tag from leg series prefixes (matches the markup config keys), but
    ONLY when EVERY leg is that one sport. A combo whose legs span sports — or
    contains any unknown leg — tags 'other' ⇒ markup 0. This is a FAIL-SAFE
    (quiet-failure rule 2): a per-sport markup must never leak a sport's validated
    edge onto another sport's leg, independent of the leg-series allowlist (so
    widening the allowlist later can't silently apply soccer's markup to an MLB
    leg). KXWC* = soccer/World Cup, KXMLB* = MLB."""
    sports = {_leg_sport(t) for t in leg_tickers}
    if len(sports) == 1:
        return sports.pop()
    return "other"


@dataclass(frozen=True, slots=True)
class MarkupPolicy:
    enabled: bool
    by_sport: dict[str, int]  # sport -> markup_cc; only ENABLED, positive sports present
    # Series-prefix -> defensive markup ADDER cc (the #37 corners edge-floor).
    # Rides ON TOP of an ACTIVE sport markup only: a dark sport (disabled or 0)
    # stays bit-identical dark. Applied once per combo (max matching adder, never
    # summed — the measured corners richness is per-COMBO, not per-leg).
    series_adders: dict[str, int]
    # sport -> ((fair_below_cc, markup_cc), ...) ascending. Fair-dependent
    # markup: the first tier whose bound exceeds the combo fair applies; above
    # every tier the flat by_sport value applies. Registered only for ENABLED
    # sports (dark stays dark).
    tiers_by_sport: dict[str, tuple[tuple[int, int], ...]]
    # ML-PARLAY OVERRIDE (2026-08-16 composition tilt, operator "do all of
    # it"): a CROSS-GAME, MONEYLINE-ONLY parlay below the fair bound prices
    # at this flat markup instead of the sport's longshot tiers. Evidence:
    # the class is 33.0% of the entire RFQ tape and we filled 0/529 quoted —
    # losing by exactly the tier markup (field clears our-fair +0.05-0.25c
    # on 1,122 matched auctions). Scope-guarded: only MLB/soccer, only when
    # EVERY leg is a MONEYLINE on a DISTINCT game (same-game/mutex shapes
    # and prop-carrying combos keep full tiers — the whale/model-risk bands).
    # 0 = dark (today's behaviour byte-identical).
    ml_parlay_cc: int = 0
    ml_parlay_fair_below_cc: int = 3500

    @classmethod
    def from_config(cls, cfg: MarkupConfig) -> MarkupPolicy:
        by: dict[str, int] = {}
        adders: dict[str, int] = {}
        tiers: dict[str, tuple[tuple[int, int], ...]] = {}
        if cfg.enabled:
            for name, sc in (
                ("soccer", cfg.soccer),
                ("mlb", cfg.mlb),
                # 2026-07-26 operator wire: winners-only esports/racing and
                # the cross-sport ("mixed") bucket.
                ("esports", cfg.esports),
                ("racing", cfg.racing),
                ("mixed", cfg.mixed),
            ):
                if sc.enabled and sc.markup_cc > 0:
                    by[name] = int(sc.markup_cc)
                    sport_tiers = tuple(
                        (int(t.fair_below_cc), int(t.markup_cc))
                        for t in getattr(sc, "tiers", [])
                    )
                    if sport_tiers:
                        tiers[name] = sport_tiers
            adders = {
                prefix: int(cc)
                for prefix, cc in getattr(cfg, "series_adders_cc", {}).items()
                if cc > 0
            }
        return cls(
            enabled=bool(cfg.enabled),
            by_sport=by,
            series_adders=adders,
            tiers_by_sport=tiers,
            ml_parlay_cc=int(getattr(cfg, "ml_parlay_cc", 0) or 0),
            ml_parlay_fair_below_cc=int(
                getattr(cfg, "ml_parlay_fair_below_cc", 3500) or 3500
            ),
        )

    def markup_cc(self, sport: str) -> int:
        """Markup in centi-cents over fair for this sport; 0 = no markup (dark)."""
        return self.by_sport.get(sport, 0)

    def _series_adder_cc(self, leg_tickers: Iterable[str]) -> int:
        """Largest configured series adder matched by any leg (prefix match).
        Max, not sum: one defensive floor per combo."""
        if not self.series_adders:
            return 0
        best = 0
        for ticker in leg_tickers:
            for prefix, cc in self.series_adders.items():
                if ticker.startswith(prefix) and cc > best:
                    best = cc
        return best

    def markup_for(
        self, leg_tickers: Iterable[str], fair_cc: int | None = None
    ) -> tuple[str, int]:
        """(sport, markup_cc) for a combo's legs — the sport markup (fair-tiered
        when ``fair_cc`` is given and tiers are configured) plus the largest
        matching series adder. The adder and tiers apply ONLY when the sport
        markup is active (base > 0), so dark stays dark. ``fair_cc`` None (older
        callers / no fair available) ⇒ the flat base, exactly as before."""
        legs = list(leg_tickers)
        sport = sport_of(legs)
        base = self.markup_cc(sport)
        if base <= 0 and sport == "other" and legs:
            # MIXED-SPORT COMBO (2026-07-26, esports/racing wire — the
            # operator's stated shape: "if someone mixes esports and racing
            # and MLB it'll go thru"). ``sport_of`` tags any multi-sport
            # combo 'other' ⇒ markup 0, which would systematically
            # UNDERPRICE exactly those combos (the KXMENWORLDCUP
            # zero-markup incident, one level up). Fail-safe intent is
            # preserved by taking the MINIMUM active markup across the
            # legs' sports — never lets a richer sport's validated edge
            # ride on a leaner sport's leg — and by requiring EVERY leg to
            # be a KNOWN sport with an ACTIVE markup (any unknown or dark
            # sport ⇒ 0, exactly as today).
            # Every leg must be a KNOWN sport with an ACTIVE markup (any
            # unknown/dark leg ⇒ 0, exactly as before). The cross-sport
            # bucket then prices from its OWN config (operator 2026-07-26:
            # 4-6c — richer than the per-sport tiers because these combos
            # are the niche, scarcely-quoted, pure-product flow), falling
            # back to the MINIMUM leg markup when 'mixed' is not configured
            # (never leaks a richer sport's edge onto a leaner leg).
            per_leg = [self.markup_cc(_leg_sport(t)) for t in legs]
            if per_leg and all(cc > 0 for cc in per_leg):
                sport = "mixed"
                base = self.markup_cc("mixed") or min(per_leg)
        if base <= 0:
            return sport, base
        if fair_cc is not None:
            for below, cc in self.tiers_by_sport.get(sport, ()):
                if fair_cc < below:
                    base = cc
                    break
            # ML-PARLAY OVERRIDE (2026-08-16, see the field's docstring):
            # replaces the tier for cross-game ML-only MLB/soccer parlays
            # below the fair bound. Fail-safe: any leg that is not a
            # MONEYLINE, any repeated game code, or any unparseable ticker
            # keeps the tier markup untouched.
            if (
                self.ml_parlay_cc > 0
                and sport in ("mlb", "soccer")
                and fair_cc < self.ml_parlay_fair_below_cc
                and _is_cross_game_ml_parlay(legs)
            ):
                base = self.ml_parlay_cc
        return sport, base + self._series_adder_cc(legs)
