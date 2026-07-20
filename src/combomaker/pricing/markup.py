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


def _leg_sport(ticker: str) -> str:
    # Pricing aliases apply (2026-07-16): an aliased champion leg tags as its
    # structural equivalent's sport — otherwise a KXMENWORLDCUP leg tagged
    # 'other' and the whole combo quoted with ZERO markup (observed live).
    ticker = resolve_pricing_alias(ticker)
    if ticker.startswith("KXWC"):
        return "soccer"
    if ticker.startswith("KXMLB"):
        return "mlb"
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

    @classmethod
    def from_config(cls, cfg: MarkupConfig) -> MarkupPolicy:
        by: dict[str, int] = {}
        adders: dict[str, int] = {}
        tiers: dict[str, tuple[tuple[int, int], ...]] = {}
        if cfg.enabled:
            for name, sc in (("soccer", cfg.soccer), ("mlb", cfg.mlb)):
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
        if base <= 0:
            return sport, base
        if fair_cc is not None:
            for below, cc in self.tiers_by_sport.get(sport, ()):
                if fair_cc < below:
                    base = cc
                    break
        return sport, base + self._series_adder_cc(legs)
