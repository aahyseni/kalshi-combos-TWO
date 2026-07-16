"""Public parse / invert / sample / settle structural API (RISK_ENGINE_AUDIT_
ACTION_PLAN.txt P1.5).

The risk Monte-Carlo (``sim/structural_book.py``) must reconstruct exactly the
same Dixon-Coles model the live pricer inverts and settle the same leg specs
against the same sampled scoreline states — hard rule 8c ("reuses the live
parse/invert/settle verbatim"). Before this module it did so by importing the
private, underscore-prefixed internals of ``pricing.structural`` and
``pricing.dixon_coles`` directly (``_parse_leg``, ``_parse_match``,
``_states``, ``_team_indicator``, ``_half_indicator``, ``_team_goals``,
``_States``). Reaching across the pricing<-risk seam into names the pricing
package marks private is fragile: a rename in the pricer silently breaks the
risk parity, and there is no declared, tested contract for what risk is allowed
to depend on.

This module is that declared contract: a thin, PUBLIC re-export of exactly the
parse / invert / sample / settle surface the risk MC needs. It adds NO math and
changes NO behavior — every name here is the same object as its private source
(``parse_leg is _parse_leg``), so the analytic-vs-simulated parity the structural
book test asserts is preserved to the byte. The private originals remain the
implementation; this is the only door risk (and any future consumer) walks
through, and the ``test_structural_api`` parity test pins the identity so a
future refactor of the pricer cannot quietly diverge the two paths.

Surface, grouped by the P1.5 verb it serves:

  parse   -- ``parse_match`` (game-code blob -> ``Match``), ``parse_leg``
             (ticker + match -> ``LegSpec`` or a decline reason string), and the
             opaque ``Match`` handle they exchange.
  invert  -- ``invert`` (leg marginals -> fitted ``InvertedModel``) and the
             ``ModelParams`` / ``InvertedModel`` / ``StructuralError`` it needs.
  sample  -- ``states`` (``ModelParams`` -> weighted terminal-state enumeration)
             and the ``States`` container the sampler thins.
  settle  -- ``team_indicator`` / ``half_indicator`` (0/1 leg settlement against
             states) and ``team_goals`` (per-state goal counts the player and
             advance settlement need).

Leg-spec dataclasses (``Advance``, ``PlayerScores``, the ``Half*`` family, ...)
and ``Team`` / ``MatchFormat`` re-export unchanged from ``dixon_coles``; they are
already public there and are re-exported here so a consumer needs exactly one
import for the whole structural contract.
"""

from __future__ import annotations

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
)
from combomaker.pricing.dixon_coles import (
    _half_indicator as half_indicator,
)
from combomaker.pricing.dixon_coles import (
    _States as States,
)
from combomaker.pricing.dixon_coles import (
    _states as states,
)
from combomaker.pricing.dixon_coles import (
    _team_goals as team_goals,
)
from combomaker.pricing.dixon_coles import (
    _team_indicator as team_indicator,
)

# Pricing-alias resolution is part of the PARSE surface (2026-07-16):
# ``parse_leg`` resolves internally, and any consumer that reads a game code /
# match format straight off a ticker (the risk MC's ``_try_build_game``) must
# resolve the SAME way or an aliased leg would parse in pricing and not in risk.
from combomaker.pricing.legtypes import (  # noqa: E402  (grouped re-export)
    resolve_pricing_alias,
)
from combomaker.pricing.structural import (
    _Match as Match,
)
from combomaker.pricing.structural import (
    _parse_leg as parse_leg,
)
from combomaker.pricing.structural import (
    _parse_match as parse_match,
)

__all__ = [
    # leg-spec dataclasses (settle targets)
    "Advance",
    "Btts",
    "Draw",
    "GoalSpread",
    "HalfBtts",
    "HalfDraw",
    "HalfGoalSpread",
    "HalfResult",
    "HalfTotalOver",
    "LegSpec",
    "PlayerScores",
    "TeamWin",
    "TotalOver",
    # enums / handles
    "MatchFormat",
    "Match",
    "Team",
    # invert
    "InvertedModel",
    "ModelParams",
    "StructuralError",
    "invert",
    # parse
    "parse_leg",
    "parse_match",
    "resolve_pricing_alias",
    # sample
    "States",
    "states",
    # settle
    "half_indicator",
    "team_goals",
    "team_indicator",
]
