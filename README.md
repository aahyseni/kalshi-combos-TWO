# combomaker

Automated market maker for Kalshi **combo (multivariate) RFQs** — maker side.

Ingests RFQs over the communications WebSocket, prices the joint probability of the
legs (top-down from Kalshi's own leg orderbooks plus pluggable devigged external
odds; correlations via a Gaussian copula), applies a risk engine, quotes, handles
the accept→confirm handshake inside the 3-second High Volatility Market window,
and runs a Monte Carlo simulator over the whole book.

## Safety

- **Demo by default.** Production requires `--env prod --confirm-live` *and*
  production limits explicitly configured. This is hardcoded.
- Secrets come only from environment variables (`KALSHI_API_KEY_ID`,
  `KALSHI_PRIVATE_KEY_PATH` or `KALSHI_PRIVATE_KEY_PEM`). Nothing secret is ever
  committed or logged.

## Credentials setup

1. Create a **demo** account + API key at the Kalshi demo site (the key's
   private half is shown exactly once — save the `.pem`). For the Phase 2.5
   ground-truth harness you need a **second** demo account (requester side).
2. Put the `.pem` files somewhere outside the repo, e.g. `C:\Users\you\.kalshi\`.
3. `copy .env.example .env` in the repo root and fill in the key IDs + pem
   paths. `.env` is gitignored and loaded automatically by the CLI (existing
   environment variables always win over the file).

## Quick start

```sh
uv sync
uv run pytest                 # unit tests (no network)
uv run pytest -m integration  # demo-environment integration tests (needs creds)
uv run combomaker run --env demo --mode observe
```

See `CLAUDE.md` for architecture decisions and current phase, `NOTES.md` for
doc-verified exchange mechanics and discrepancies.
