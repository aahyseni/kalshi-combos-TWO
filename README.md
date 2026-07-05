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

## Quick start

```sh
uv sync
uv run pytest                 # unit tests (no network)
uv run pytest -m integration  # demo-environment integration tests (needs creds)
uv run combomaker run --env demo --mode observe
```

See `CLAUDE.md` for architecture decisions and current phase, `NOTES.md` for
doc-verified exchange mechanics and discrepancies.
