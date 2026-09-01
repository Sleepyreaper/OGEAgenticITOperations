# Model Configuration Guide

This document explains how each of the six agent keys is configured — endpoint,
deployment, API version, and capability controls — and why the checked-in
`profiles/power/` reference deployment maps agents to a specific GPT-5.6 model
tier. It also documents the enforceable token/response/personality controls
available to every profile (`max_completion_tokens`, `max_context_chars`,
`response_instruction`, and pricing), which layer they live in, and how to set
them without forking a profile.

See [BRANDING.md](../BRANDING.md) for the full profile schema and
[DEPLOYMENT.md](../DEPLOYMENT.md) for the deployment/environment-variable
reference this document builds on.

## The six agent keys

Every profile configures exactly six agents (`app/profiles.py::AGENT_KEYS` — the
orchestration logic and public API are wired to these keys specifically):

| Agent key | Role in the council |
|---|---|
| `orchestrator` | Routes requests, synthesizes specialist output into one recommendation |
| `cost_sentinel` | Cost/capacity optimization — rightsizing, waste, burn rate |
| `standards_architect` | Infrastructure standards/reliability rationale |
| `diagnostics_sre` | Root-cause analysis from Azure telemetry |
| `scout` | Proactive monitoring — anomalies, drift, quota |
| `compliance_inspector` | Azure Policy violation classification |

For each key, `app/config.py::Settings` resolves five layers of configuration,
each overriding the previous:

1. **Checked-in profile** (`profiles/<id>/profile.json` `agents.<key>` block) —
   `name`, `role`, `deployment`, `endpoint_ref`, `temperature`,
   `supports_temperature`, `api_version`, `prompt_file`, plus the controls
   below.
2. **Global env vars** (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
   `AZURE_OPENAI_API_VERSION`) — the fallback default when an agent doesn't
   specify its own endpoint/deployment/API version.
3. **Per-agent env vars** (`AGENT_<KEY>_<FIELD>`) — surgical overrides for one
   agent without forking the whole profile. `KEY` is the agent key
   upper-cased, e.g. `AGENT_COST_SENTINEL_DEPLOYMENT`.
4. **Bicep `agentOverrides` parameter** — the same fields, camelCase, set as
   App Service settings by `infra/main.bicep`/`infra/modules/web-app.bicep`
   (which ultimately just populate the same `AGENT_<KEY>_<FIELD>` env vars).
5. **`scripts/configure.py`** — a setup-wizard convenience layer that writes
   layers 2-4 for you (`.env` and `infra/main.bicepparam`) from `--agent
   KEY:FIELD=VALUE` flags or an `--answers` JSON file.

Any malformed value at any layer raises `app.profiles.ProfileError` at startup
— nothing here silently falls back to a guessed value.

### Endpoint

`endpoint_ref` (profile) / `AGENT_<KEY>_ENDPOINT` (env) resolves to one of:
`""`/`"primary"` (the default `AZURE_OPENAI_ENDPOINT`), `"secondary"` (a
first-class optional second account/region slot), any other name registered
via `AZURE_OPENAI_ENDPOINT_<NAME>`, or a literal `https://` URL. This lets
different agents live on different Azure OpenAI accounts/regions (e.g. a
GPT-5.6 Sol quota in one region, Terra/Luna in another) without any code
changes.

### Deployment

`deployment` (profile) / `AGENT_<KEY>_DEPLOYMENT` (env) is the Azure OpenAI
**deployment name** you created in your Foundry/OpenAI resource — it does not
have to match the underlying model's catalog name. `profiles/power/` uses
`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` as example deployment names;
your own deployment names are entirely up to you (Bicep/env vars just pass
whatever string you configure straight through to the `model=` argument on
each `chat.completions.create()` call).

### API version

`api_version` (profile) / `AGENT_<KEY>_API_VERSION` (env), falling back to
`AZURE_OPENAI_API_VERSION` (default `2025-01-01-preview`). Set this per-agent
if one agent's model requires a newer API version than the rest (e.g. a
model that needs a specific API version to accept `max_completion_tokens` or
reasoning-specific parameters) — verify the required version against your own
Azure OpenAI/AI Foundry resource before deploying.

### Temperature

`temperature` / `supports_temperature` (profile) or `AGENT_<KEY>_TEMPERATURE`
/ `AGENT_<KEY>_SUPPORTS_TEMPERATURE` (env). Reasoning-style/flagship models
(o-series, GPT-5.x-style deep reasoning tiers — this includes GPT-5.6 Sol and
Terra in the recommended mapping below) commonly only accept the default
temperature (`1.0`) and error on any other value. `supports_temperature`
(default `false`) is what actually gates whether `temperature` is sent on the
call at all (`app/agents/runner.py::call_agent`) — it is not inferred from a
deployment name. All three checked-in profiles ship with
`supports_temperature: false` on every agent; set it to `true` per-agent only
after confirming your specific deployment accepts a custom temperature.

## Enforceable token/response/personality controls

Five more optional `agents.<key>` fields make length, cost, and tone
enforceable per-agent instead of relying on prompt wording alone:

| Field | Type | Convention | Env var override |
|---|---|---|---|
| `max_completion_tokens` | non-negative integer | `0` (or omitted) = provider default (the argument is not sent at all); any positive value is a hard cap | `AGENT_<KEY>_MAX_COMPLETION_TOKENS` |
| `max_context_chars` | non-negative integer | `0` (or omitted) = no truncation; any positive value truncates `context_data` to that many characters | `AGENT_<KEY>_MAX_CONTEXT_CHARS` |
| `response_instruction` | non-empty string, optional | appended as its own instruction message on every call | `AGENT_<KEY>_RESPONSE_INSTRUCTION` |
| `input_cost_per_million` | non-negative number | USD per 1M **prompt** tokens, for the telemetry cost estimate. `0.0` (default) = don't estimate | `AGENT_<KEY>_INPUT_COST_PER_MILLION` |
| `output_cost_per_million` | non-negative number | USD per 1M **completion** tokens, same estimate | `AGENT_<KEY>_OUTPUT_COST_PER_MILLION` |

Validation is strict at both layers: `app/profiles.py::load_profile_document`
rejects a malformed value in `profile.json` (wrong type, negative number, or an
empty-string `response_instruction`) with a clear, itemized `ProfileError` at
startup; `app/config.py::Settings` applies the identical rule to the
corresponding env var override.

### `max_completion_tokens`

Sent as the OpenAI `chat.completions.create(..., max_completion_tokens=N)`
argument only when `N > 0` (`app/agents/runner.py::call_agent`) — `0` is never
sent as a literal value (most providers would treat that as "produce no
output" or reject it outright). This is the single lever for keeping a
verbose, deep-reasoning model (like GPT-5.6 Sol) from generating a needlessly
long response, and for keeping a fast/efficient model (like GPT-5.6 Luna)
snappy and cheap.

### `max_context_chars`

Applied **only** to `context_data` (the pre-fetched Azure environment
JSON/telemetry) via `app/agents/runner.py::truncate_context` — the system
prompt and the user's question are never truncated. Character counts are an
**approximation** of token counts, not exact token accounting: roughly 3-4
characters per token for typical English/JSON text, so a 20,000-character cap
is roughly 5,000-6,500 tokens, not exactly. When truncation happens, an
explicit marker is appended:

```
[...CONTEXT TRUNCATED at 20000 characters (original length: 47213 characters).
Data beyond this point was omitted and may leave this truncated to an
incomplete/invalid JSON document...]
```

This never attempts to "repair" the cut text into valid-looking JSON — the
point is to make truncation obvious to both the model and anyone reading logs,
not to fabricate a plausible-but-fictitious document.

> **Known nuance**: the orchestrator's Round 3 synthesis call
> (`app/agents/runner.py::run_council`) re-embeds the full Round 1/Round 2
> recap — including the original context data — as part of its **user
> message**, not as `context_data`. Per the rule above ("only `context_data` is
> truncated"), that recap is not subject to `max_context_chars`. Give the
> orchestrator a generous `max_context_chars` regardless (for the rare case a
> caller invokes `call_agent` on it directly with `context_data`), and rely on
> `max_completion_tokens` to bound the orchestrator's own output length.

### `response_instruction`

Appended as its own `{"role": "user", "content": "RESPONSE INSTRUCTION: ..."}`
message on every call (`app/agents/runner.py::call_agent`), after the system
prompt, the context data (if any), and the user's question. This replaces what
used to be a hardcoded table in `runner.py` keyed by the **OGE** persona
display names (`"Barrel Counter"`, `"Pipeline"`, ...) — a profile with
different agent names would previously fall through to one generic default
line. Every profile now sets an explicit `response_instruction` per agent, so
tone/length/personality is fully profile-owned and never tied to a specific
persona name.

### Pricing (`input_cost_per_million` / `output_cost_per_million`)

Used to compute `usage.estimated_cost_usd` on every agent response
(`app/agents/runner.py::estimate_cost_usd`):

```
estimated_cost_usd = (prompt_tokens / 1_000_000) * input_cost_per_million
                   + (completion_tokens / 1_000_000) * output_cost_per_million
```

**This is a caller-maintained estimate, not billing truth.** It reflects
whatever price-per-million you configure here — it does not query Azure's
actual billing/rate card, does not account for reserved capacity/PTU
discounts, batch pricing, or promotional rates, and will drift if your
negotiated price changes without updating the profile. Cross-check against
[Azure Cost Management](https://learn.microsoft.com/azure/cost-management-billing/)
for actual spend — see [TELEMETRY.md](TELEMETRY.md) for how this estimate
flows into Application Insights alongside real dependency-call telemetry.

## Recommended power-utility deployment: GPT-5.6 Sol / Terra / Luna

`profiles/power/` (the default profile) demonstrates a deliberate mapping of
the six agents onto three tiers of a single model family:

| Agent key | Display name | Model tier | Example deployment name | `max_completion_tokens` | `max_context_chars` |
|---|---|---|---|--:|--:|
| `orchestrator` | Grid Coordinator | **GPT-5.6 Sol** | `gpt-5.6-sol` | 1400 | 30000 |
| `diagnostics_sre` | Incident Investigator | **GPT-5.6 Sol** | `gpt-5.6-sol` | 1500 | 30000 |
| `cost_sentinel` | Cost & Capacity Analyst | **GPT-5.6 Terra** | `gpt-5.6-terra` | 900 | 20000 |
| `standards_architect` | Reliability Engineer | **GPT-5.6 Terra** | `gpt-5.6-terra` | 800 | 20000 |
| `compliance_inspector` | Compliance Advisor | **GPT-5.6 Terra** | `gpt-5.6-terra` | 850 | 20000 |
| `scout` | Operations Monitor | **GPT-5.6 Luna** | `gpt-5.6-luna` | 400 | 12000 |

> **Naming note**: the Azure AI Foundry model catalog entry for the mid tier
> is **"GPT-5.6 Terra"** — that is its actual model/deployment name. Some
> internal conversations have referred to it informally as "Terra Firma"; that
> is not the catalog name and should not be used as the deployment name when
> creating the Azure OpenAI/AI Foundry deployment. Deployment names remain
> entirely customer-defined — `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna`
> above are illustrative examples, not requirements.

### Why this mapping

* **Sol = deep/flagship.** The orchestrator synthesizes every specialist's
  output (and any Round 2 debate) into one coherent recommendation, and the
  incident investigator performs multi-step root-cause analysis across
  correlated Azure telemetry. Both benefit from the strongest available
  reasoning depth more than any other agent in the council — a shallow
  synthesis or a missed correlation here undermines the whole council's
  output. Sol is the most expensive tier per token; it's used on exactly the
  two agents where that depth measurably changes the answer.
* **Terra = balanced production tier.** Cost/capacity analysis, standards
  rationale, and compliance classification all require solid reasoning
  (showing math, citing the right policy, defending an engineering tradeoff)
  but not the deepest multi-step chains the orchestrator/investigator need.
  Terra is the default choice for "most of the council, most of the time" —
  it's meaningfully cheaper than Sol per token while still handling
  structured, rules-based analysis reliably.
* **Luna = fast/efficient.** The monitor scans continuously and produces
  short, severity-tagged alerts — throughput and latency matter more than
  reasoning depth, and a lighter/faster tier keeps continuous scanning cheap.
  This is the same rationale the original `foundry-nano` deployment slot
  followed in the `oge`/`generic` profiles.

### Cost/tradeoff rationale

Putting six agents on the flagship tier would maximize quality on every call
but multiply cost across the highest-volume, lowest-complexity agent (the
monitor) for no measurable benefit. Putting everything on the cheapest tier
would save money but under-power the two agents (orchestrator, diagnostics)
whose output quality most affects what the user actually sees and acts on.
The Sol/Terra/Luna split is a deliberate per-agent cost/quality tradeoff, not a
uniform default — adjust it for your own workload and budget via
`agentOverrides` (env vars or Bicep) without forking the profile.

## Existing `oge` / `generic` profiles

Both ship with explicit, conservative values for every new field — this does
not change their personality/tone (the exact wording of the old hardcoded
per-persona style hints was moved into `response_instruction` verbatim for
`oge`; `generic` keeps the same "3-5 concise sentences" default it always
used). `input_cost_per_million`/`output_cost_per_million` default to `0.0`
(no cost estimate) since neither profile has a real GPT-5.6-tier pricing
example attached — set them per your own negotiated Azure OpenAI pricing if
you want `estimated_cost_usd` populated for `foundry-gpt`/`foundry-reasoning`/
`foundry-nano` deployments.

## Setting these fields

**Verifying an override took effect** — `GET /api/health` reports, per
agent, `max_completion_tokens`, `max_context_chars`,
`response_instruction_configured` (boolean), and `pricing_configured`
(boolean) alongside the existing `name`/`deployment`/`endpoint_configured`/
`supports_temperature` fields. None of these are secrets, so they're safe
to include in this endpoint's configuration-presence check — it's the
fastest way to confirm a `--agent`/env-var/Bicep override actually reached
the running app without needing to trigger a real (billed) agent call.

**Editing a profile directly** — add/edit the fields in
`profiles/<id>/profile.json` under the relevant `agents.<key>` block (see
`profiles/power/profile.json` for a full example).

**Without forking a profile** — set the env var override:

```bash
AGENT_COST_SENTINEL_MAX_COMPLETION_TOKENS=900
AGENT_COST_SENTINEL_RESPONSE_INSTRUCTION="Lead with the dollar figure, 3-5 sentences."
```

**Via the setup wizard** (`scripts/configure.py`) — the same fields are
available through the existing per-agent override mechanism:

```bash
python3 scripts/configure.py --non-interactive \
  --agent cost_sentinel:max_completion_tokens=900 \
  --agent cost_sentinel:response_instruction="Lead with the dollar figure, 3-5 sentences." \
  ... # other required flags, see --help
```

or the equivalent in an `--answers answers.json` file:

```json
{
  "agent_overrides": {
    "cost_sentinel": {
      "max_completion_tokens": "900",
      "response_instruction": "Lead with the dollar figure, 3-5 sentences."
    }
  }
}
```

**Via Bicep** (`infra/main.bicepparam`'s `agentOverrides` parameter) — same
fields, camelCase:

```bicep
param agentOverrides = {
  cost_sentinel: {
    maxCompletionTokens: '900'
    responseInstruction: 'Lead with the dollar figure, 3-5 sentences.'
  }
}
```

`scripts/configure.py` translates the snake_case field names used everywhere
else (env vars, `--agent` flags, `--answers` JSON) into the camelCase Bicep
expects when it generates `infra/main.bicepparam` for you — you don't need to
do this translation by hand unless you're editing the `.bicepparam` file
directly.
