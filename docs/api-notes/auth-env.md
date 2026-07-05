# Kalshi API — Environments, Auth (RSA), Demo, Rate Limits, WS Connection

Sources fetched (all live, none 404'd):
- `https://docs.kalshi.com/getting_started/api_environments.md`
- `https://docs.kalshi.com/getting_started/api_keys.md`
- `https://docs.kalshi.com/getting_started/demo_env.md`
- `https://docs.kalshi.com/getting_started/quick_start_authenticated_requests.md`
- Also fetched (referenced, critical): `getting_started/rate_limits.md`, `getting_started/quick_start_websockets.md`, `websockets/websocket-connection.md`, `websockets/connection-keep-alive.md`

---

## 1. Environments & Base URLs

### REST base URLs
| Env | Recommended | Alternative |
|---|---|---|
| **Production** | `https://external-api.kalshi.com/trade-api/v2` | `https://api.elections.kalshi.com/trade-api/v2` |
| **Demo** | `https://external-api.demo.kalshi.co/trade-api/v2` | `https://demo-api.kalshi.co/trade-api/v2` |

### WebSocket URLs
| Env | Recommended | Alternative |
|---|---|---|
| **Production** | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| **Demo** | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

Notes:
- Demo uses the `.co` TLD; production uses `.com` (except alternative demo `demo-api.kalshi.co`). Easy to typo.
- The `external-api*` hosts are "dedicated to external traders" and are the **recommended** hosts. `api.elections.kalshi.com` still works but is the alternative.
- "Credentials are not shared between environments, so demo API keys only work against demo endpoints and production API keys only work against production endpoints."
- AWS PrivateLink available for institutional participants — contact `institutional@kalshi.com`.

## 2. API Keys

- Create in the web UI: `https://kalshi.com/account/profile` → "Create New API Key". Format: `RSA_PRIVATE_KEY` (PEM). You receive a **private key file** (`.key`, PEM) + a **Key ID** (UUID format).
- "The private key will not be stored by our service, and you will not be able to retrieve it again once this page is closed." — save it immediately.
- Docs do NOT state a required RSA key size (2048 is what the UI generates in practice — unverified).
- Programmatic key management endpoints exist (not fetched in detail): `api-reference/api-keys/create-api-key.md`, `delete-api-key.md`, `generate-api-key.md`, `get-api-keys.md`.
- For demo, create keys via the **demo** site with a separate demo account (see §5).

## 3. Request Signing (REST) — the exact algorithm

Every authenticated request carries exactly three headers:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | your API Key ID (UUID) |
| `KALSHI-ACCESS-TIMESTAMP` | current Unix time in **milliseconds**, as a string (e.g. `"1703123456789"`) |
| `KALSHI-ACCESS-SIGNATURE` | Base64-encoded RSA-PSS signature (standard base64, not urlsafe) |

### Message to sign
Concatenation with **no separators**:

```
timestamp + HTTP_METHOD + path_without_query
```

- `HTTP_METHOD` is uppercase (`GET`, `POST`, `DELETE`, ...).
- `path` is the **full path from the host root, including the `/trade-api/v2` prefix** — e.g. `/trade-api/v2/portfolio/balance`, NOT `/portfolio/balance`.
- **Query parameters are stripped before signing** but stay in the actual HTTP request. Docs: "if your request is to `/trade-api/v2/portfolio/orders?limit=5`, sign only `/trade-api/v2/portfolio/orders`."
- Worked example message: `1703123456789GET/trade-api/v2/portfolio/balance`
- The request **body is NOT part of the signed message**.

### Crypto parameters
- RSA-**PSS** (NOT PKCS1v15) with:
  - hash: **SHA256**
  - MGF: **MGF1(SHA256)**
  - salt length: **DIGEST_LENGTH** (`padding.PSS.DIGEST_LENGTH` in Python `cryptography`; JS uses `RSA_PKCS1_PSS_PADDING`)
- Output: **Base64-encode** the raw signature bytes.

### Canonical Python signing code (verbatim from docs)
```python
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

def sign_request(private_key, timestamp, method, path):
    # Strip query parameters from path before signing
    path_without_query = path.split('?')[0]
    message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')
```

### Full docs request example (verbatim structure)
```python
import requests, datetime
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

BASE_URL = 'https://external-api.demo.kalshi.co/trade-api/v2'  # or https://external-api.kalshi.com/trade-api/v2 for prod

def load_private_key(key_path):
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def get(private_key, api_key_id, path, base_url=BASE_URL):
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    # Signing requires the full URL path from root (e.g. /trade-api/v2/portfolio/balance)
    sign_path = urlparse(base_url + path).path
    signature = create_signature(private_key, timestamp, "GET", sign_path)
    headers = {
        'KALSHI-ACCESS-KEY': api_key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp
    }
    return requests.get(base_url + path, headers=headers)
```

The quick-start example endpoint is `GET /trade-api/v2/portfolio/balance`; response has field `balance` in **cents** (docs print `balance / 100` as dollars).

### Not documented (verified absent from these pages)
- Timestamp tolerance / clock-skew window for `KALSHI-ACCESS-TIMESTAMP` — **no number given anywhere**.
- Auth-failure error codes/messages.
- RSA key size requirement.

## 4. WebSocket Authentication & Keep-Alive

- Auth happens on the **HTTP handshake** with the **same three headers** (`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`).
- Signed message for the WS handshake: `timestamp + "GET" + "/trade-api/ws/v2"` (same RSA-PSS/SHA256 scheme). Note it's the WS path, not a REST path.
- Docs' Python example uses `websockets.connect(WS_URL, additional_headers=ws_headers)` and also sets `"Content-Type": "application/json"` in the handshake headers.
- After connect, commands are JSON: `{"id": <unique int per command>, "cmd": "subscribe" | "unsubscribe" | "list_subscriptions" | "update_subscription", "params": {...}}`.
- Subscribe example (verbatim):
```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": "KXHARRIS24-LSV"}}
```
- Response message `type` values seen in the example: `subscribed`, `orderbook_snapshot`, `orderbook_delta`, `error`. Orderbook delta messages may contain `client_order_id` inside `msg` when the update is your own order.
- **Keep-alive**: "Kalshi sends Ping frames (`0x9`) every 10 seconds with body `heartbeat`"; "Clients should respond with Pong frames (`0xA`)". Clients may also initiate pings; server responds with pongs. Most WS libraries (incl. Python `websockets`) auto-pong — but verify your client does. No explicit disconnect-on-missed-pong timeout is documented.
- WS error code 18 = "Command timeout" (mentioned in the AsyncAPI schema). Connection limits per key: not documented.

## 5. Demo Environment

- Separate account, separate credentials, separate API keys from production.
- Account creation: docs point to a step-by-step tutorial hosted on **Google Slides** (link on the `demo_env` page) rather than inline instructions.
- Funds are **mock money**.
- Explicit caveat: "the price and behavior of markets in the demo environment may not be reflective of those in real markets" — different market conditions and liquidity than prod. Do not calibrate pricing/fill models on demo behavior.
- Endpoints as in §1 (demo column).

## 6. Rate Limits and Tiers (referenced doc, fetched)

Token-bucket limits, per second, for event contracts ("Predictions"; Perps has separate independent buckets):

| Tier | Read budget (tokens/sec) | Write budget (tokens/sec) |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,000 | 1,000 |
| Paragon | 2,000 | 2,000 |
| Prime | 4,000 | 4,000 |
| Prestige | 6,000 | 8,000 |

- Most requests cost the **default of 10 tokens**. Order **cancellations cost 2 tokens**. Non-default costs: query the `/account/endpoint_costs` endpoint. So Basic ≈ 20 reads/sec and ≈ 10 writes/sec in practice.
- **Read** = `GET` endpoints and anything not routed to Write. **Write** = "Order placement, amends, cancels, order groups, **the RFQ quote flow**, and block trade proposal accepts." → RFQ quoting consumes the WRITE bucket. At Basic tier a combo RFQ maker gets ~10 quote-related writes/sec.
- Exceeding limits: HTTP **`429 Too Many Requests`** with body `{"error": "too many requests"}`.
- **Burst**: write buckets above Basic, plus Advanced-and-higher Predictions read buckets, accumulate up to **two seconds** of budget → up to 2x the per-second budget in one burst. Basic-tier write has NO burst accumulation.
- Upgrades: Basic→Advanced via the "Upgrade Account API endpoint"; Expert–Prestige earned by 30-day rolling trading volume or assigned by Kalshi support.
- Docs do not say whether demo limits differ from prod.

## 7. Traps / gotchas for implementers

1. **Sign the full path including `/trade-api/v2`** (or `/trade-api/ws/v2` for WS). Signing only the sub-path (`/portfolio/balance`) fails.
2. **Strip query params from the signed path** but keep them on the wire.
3. **Timestamp is milliseconds**, not seconds — a seconds value will be rejected.
4. **PSS padding with `salt_length=DIGEST_LENGTH`**, MGF1-SHA256 — do not use PKCS1v15, and do not rely on library defaults (Python `cryptography` PSS examples elsewhere often use `MAX_LENGTH`).
5. The same timestamp string must go into both the signed message and the `KALSHI-ACCESS-TIMESTAMP` header.
6. The request **body is not signed** — only timestamp+method+path.
7. Demo keys ≠ prod keys; demo hosts are `.co`, prod `.com`.
8. RFQ quote operations count against the **write** token bucket (10 tokens each by default) — budget quoting throughput accordingly, and use `/account/endpoint_costs` to confirm per-endpoint costs.
9. WS `orderbook_delta` example ticker in docs (`KXHARRIS24-LSV`) is stale; use a live ticker.
10. Money example: `balance` is returned in cents.

## Critical facts (must get right)
- REST base URLs — prod: https://external-api.kalshi.com/trade-api/v2 (alt https://api.elections.kalshi.com/trade-api/v2); demo: https://external-api.demo.kalshi.co/trade-api/v2 (alt https://demo-api.kalshi.co/trade-api/v2). Demo is .co, prod is .com.
- WebSocket URLs — prod: wss://external-api-ws.kalshi.com/trade-api/ws/v2; demo: wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2.
- Auth headers (exact names): KALSHI-ACCESS-KEY (Key ID, UUID), KALSHI-ACCESS-TIMESTAMP (Unix time in MILLISECONDS as string), KALSHI-ACCESS-SIGNATURE (base64 signature).
- Signed message = timestamp + UPPERCASE_METHOD + path, no separators, where path is the FULL path from root including /trade-api/v2 prefix and with query parameters STRIPPED. Example: 1703123456789GET/trade-api/v2/portfolio/balance. Request body is never signed.
- Signature algorithm: RSA-PSS with SHA256, MGF1(SHA256), salt_length = DIGEST_LENGTH (padding.PSS.DIGEST_LENGTH in Python cryptography), then standard base64 encoding. NOT PKCS1v15, NOT MAX_LENGTH salt.
- WebSocket handshake auth: same three headers on the HTTP upgrade request; signed message is timestamp + "GET" + "/trade-api/ws/v2".
- Demo and production credentials are completely separate — demo API keys only work on demo endpoints and vice versa.
- Private key is shown exactly once at creation (kalshi.com/account/profile → Create New API Key) and cannot be retrieved again.
- Rate limits are token buckets per second: Basic tier = 200 read / 100 write tokens/sec; most requests cost 10 tokens (order cancels cost 2), so Basic ≈ 20 reads/sec and 10 writes/sec. The RFQ quote flow counts as WRITE. Exceeding returns HTTP 429 with body {"error": "too many requests"}. Basic write bucket has no burst; higher tiers accumulate up to 2 seconds of budget.
- WS keep-alive: server sends Ping frames (0x9) with body 'heartbeat' every 10 seconds; client must reply with Pong frames (0xA).
- WS commands are JSON {"id": <unique int>, "cmd": subscribe|unsubscribe|list_subscriptions|update_subscription, "params": {...}}; e.g. subscribe with params.channels=["orderbook_delta"] and params.market_ticker.

## Open questions (verify empirically on demo)
- Timestamp tolerance / clock-skew window for KALSHI-ACCESS-TIMESTAMP is not documented anywhere — empirically probe on demo (send requests with deliberately skewed timestamps) to find the accepted window before relying on local clocks.
- Whether RSA-PSS signatures with MAX_LENGTH salt also verify server-side, or only DIGEST_LENGTH — use DIGEST_LENGTH per docs, but verify if a non-Python library defaults differently.
- Required/accepted RSA key size (docs never state it; the UI-generated key size should be checked from the downloaded PEM).
- Whether demo rate limits match production tiers (docs do not differentiate) — measure 429 onset on demo.
- Exact path and method of the endpoint-costs endpoint (docs say 'consult the /account/endpoint_costs endpoint' — presumably GET /trade-api/v2/account/endpoint_costs) and the actual token cost of Create Quote / Delete Quote / Accept Quote / Confirm Quote for RFQ throughput budgeting.
- Exact path of the 'Upgrade Account API endpoint' for Basic→Advanced tier upgrade.
- WebSocket connection limits per API key / per account (not documented) and behavior on missed pongs (no disconnect timeout documented).
- Whether the alternative hosts (api.elections.kalshi.com, demo-api.kalshi.co) are deprecated or merely non-recommended, and whether they have different rate-limit or latency characteristics.
- Demo account creation specifics live in an external Google Slides tutorial (link on demo_env page) — whether demo signup needs a separate email from the prod account.
- Whether authentication failure returns a distinct error code/body (docs list none) — capture actual 401 payloads on demo for error handling.
