"""Public game-key grouping — the ONE correlation/aggregation key.

A Kalshi ``event_ticker`` is ``SERIES-GAMECODE`` (e.g.
``KXWCGAME-26JUL05MEXENG``); the GAMECODE is shared across a game's market
families (``KXWCGAME`` / ``KXWCTOTAL`` / ``KXWCBTTS`` of ONE match), so it — not
the series-specific event_ticker — is the same-game key.

This is the SAME function the relationship classifier already correlates on
(``pricing.relationships._game_key``). It is promoted here to a public, pure
export so the risk layer can aggregate on the exact key the pricer trusts,
without reaching into a private name. ``relationships._game_key`` re-exports
this symbol (a single definition, zero drift); a parity test pins them equal.

Fail-closed: a ticker with no hyphen (synthetic/degenerate) keys on the whole
string, so a leg whose event carries no game code NEVER merges with another.
"""

from __future__ import annotations


def game_key(event_ticker: str) -> str:
    """The game a leg belongs to, for correlation grouping and risk aggregation.

    ``SERIES-GAMECODE`` -> ``GAMECODE`` (shared across a game's market families).
    No hyphen (synthetic/degenerate ticker) -> the whole string, so an ungamed
    leg never merges with another game.

    Period/derived markets (first/second half — series like ``KXWC1HTOTAL``) DO
    key on the game code and rejoin the full-game same-game block, so the copula
    can correlate a modeled 1H leg with its full-time siblings. They are kept off
    the full-game STRUCTURAL inverter by a guard in ``structural.py`` — NOT by
    grouping them out here (which used to leave a real 1H x FT combo pricing at
    independence).
    """
    _series, sep, game = event_ticker.partition("-")
    if not sep:
        return event_ticker
    return game
