# Public demo security (`PUBLIC_DEMO_MODE`)

`PUBLIC_DEMO_MODE=true` makes the app safe to expose anonymously by removing the
live and mutating API surface **server-side**. It is **not** operator
authentication, Easy Auth or RBAC: it does not identify callers, and no browser
secret or operator key exists. Operator access needs a separate, gated Easy
Auth / Entra / RBAC feature. Default is `false` (locally and for existing
deployments); trusted behavior is unchanged when the flag is false. A
non-boolean value fails app startup; at request time an unparseable value
fails closed (gate on).

## What is allowed when the flag is true

Enforced centrally in `app/security/public_access.py` (one `before_request`
hook; route modules add no checks). Exact paths only, never prefixes:

| Surface | Methods |
|---|---|
| Pages and `/static/*` assets | GET, HEAD |
| `/api/health` (configuration-presence booleans, no secrets) | GET, HEAD |
| `/api/agents` (catalog derived from code) | GET, HEAD |
| `/api/demos` (static scenarios) | GET, HEAD |
| `/api/operations/demo` (fixture; no Azure or model calls) | GET, HEAD |
| `/api/activity` and `/api/activity/<id>` (single opaque id segment) | GET, HEAD |
| `/mcp` | unchanged: its own `MCP_API_KEY` auth (401 / 503 preserved) |

Everything else under `/api/*` returns JSON `403`
(`{"error": "forbidden_in_public_demo_mode", ...}`), including unknown or
future routes (fail closed): subscriptions, scans, snapshot/brief/queue,
evidence, finding updates, live analyze/briefing/tools, ZeroOps overview /
escalations / probes / handoff / chaos / propose / decision, ADO, remediate,
ask, and every non-GET/HEAD method. Non-API non-read requests are also denied.

`/api/zeroops/overview` is intentionally blocked: it exposes ledger state and
demo-resource configuration.

## Deployment

Bicep: `publicDemoMode` (bool, default `false`) in `infra/modules/web-app.bicep`
maps to the `PUBLIC_DEMO_MODE` app setting. The value is not a secret. The
parent template must pass the parameter through.

## Residual risk

Anonymous callers can still read the allowlisted fixture/activity data and may
hit `/mcp` with a guessed key; use a long random `MCP_API_KEY` from Key Vault.
Add edge rate limiting (Front Door / App Gateway) for real public exposure.
