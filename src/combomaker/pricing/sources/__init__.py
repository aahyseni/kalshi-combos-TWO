"""External OddsSource adapters — the ONLY modules allowed to import devig.

Sportsbook-style odds carry margin; each adapter devigs inside itself and
exposes fair YES-side marginals through the OddsSource protocol. Kalshi-side
code never sees raw external odds (CLAUDE.md decision #8, enforced by
tests/test_architecture.py).
"""
