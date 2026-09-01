# Telemetry (Azure Monitor OpenTelemetry)

This app can report telemetry to Application Insights via the
[`azure-monitor-opentelemetry`](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable?tabs=python)
distro — a single package that wires up the OpenTelemetry SDK, instruments
Flask automatically, and exports to Application Insights. Telemetry is a
strict opt-in keyed off one setting (`APPLICATIONINSIGHTS_CONNECTION_STRING`);
without it, the app runs exactly as before with zero telemetry overhead. This
is the normal state for local development.

## Architecture

```
Flask HTTP requests  ──(distro auto-instrumentation)──►  AppRequests
Agent OpenAI calls    ──(app/telemetry.py custom span)──►  AppDependencies
                                                            (attributes in Properties)
Standard logging      ──(distro auto-instrumentation)──►  AppTraces
Optional counters/histogram (app/telemetry.py)        ──►  AppMetrics
```

Application Insights here is **workspace-based** — provisioned by
`infra/modules/monitoring.bicep` linked to the Log Analytics workspace also
provisioned there (`WorkspaceResourceId`). All four Log Analytics tables above
live in that one workspace; query them with the KQL below from the workspace's
**Logs** blade (or the linked Application Insights resource's Logs blade —
they're the same underlying data).

* **`azure-monitor-opentelemetry` is the one-stop distro.** It already
  includes Flask instrumentation — this app does **not** separately call
  `FlaskInstrumentor().instrument_app(...)`. Doing so on top of the distro
  would double-instrument every HTTP request.
* Custom spans (below) use the SDK's default span kind and land in
  `AppDependencies` like any other outbound call the app makes.

## What's instrumented

### HTTP requests → `AppRequests`

Automatic, via the distro's Flask instrumentation. No app code involved.

### Agent OpenAI calls → `AppDependencies`

Every call to `client.chat.completions.create(...)` in
`app/agents/runner.py::call_agent` is wrapped in a custom span
(`app/telemetry.py::agent_call_span`) with these attributes:

| Attribute | Meaning |
|---|---|
| `gen_ai.operation.name` | Always `"chat"` |
| `gen_ai.provider.name` | Always `"azure.ai.openai"` |
| `gen_ai.request.model` | The deployment name requested (`AgentConfig.deployment`) |
| `gen_ai.response.model` | The model version Azure OpenAI actually reports back, if provided |
| `gen_ai.usage.input_tokens` | Prompt tokens (from the API response's `usage.prompt_tokens`) |
| `gen_ai.usage.output_tokens` | Completion tokens (from `usage.completion_tokens`) |
| `gen_ai.response.finish_reasons` | e.g. `["stop"]`, `["length"]` |
| `ops.agent.key` | The stable agent key (`orchestrator`, `cost_sentinel`, ...) |
| `ops.agent.name` | The profile's display name for that agent (e.g. "Grid Coordinator") |
| `ops.profile` | The loaded `APP_PROFILE` id |
| `ops.estimated_cost_usd` | See [MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md) — a caller-maintained pricing estimate, not billing truth |

Exceptions raised by the OpenAI call are recorded on the span
(`span.record_exception`) and the span status is set to `ERROR`
(`opentelemetry.trace.StatusCode.ERROR`) before the exception is re-raised
unchanged — `app/telemetry.py` never swallows a caller's exception. Call
latency is measured end-to-end around the `client.chat.completions.create()`
call.

> **`gen_ai.*` attribute names are Development/experimental (as of 2026).**
> They follow the [OpenTelemetry Generative AI semantic
> conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/), which are
> still evolving upstream. This app spells them out as literal string
> constants in `app/telemetry.py` rather than depending on the separate
> `opentelemetry-semantic-conventions-ai` package, specifically so a
> convention rename/relocation upstream doesn't silently break this app's
> attributes or require a new dependency. If you upgrade
> `azure-monitor-opentelemetry` and the upstream convention has stabilized,
> revisit this file.

### Standard logging → `AppTraces`

Automatic, via the distro's logging instrumentation (Python's standard
`logging` module).

### Optional metrics → `AppMetrics`

`app/telemetry.py` also emits four low-cardinality counters/histogram
(attributes limited to `ops.agent.key` + `gen_ai.request.model`, both bounded
by your own six-agent/deployment configuration — never a per-request or
per-user value):

| Instrument | Type | Unit |
|---|---|---|
| `ops_council.agent.calls` | counter | calls |
| `ops_council.agent.tokens` | counter (dimensioned by `gen_ai.token.type` = `input`/`output`) | tokens |
| `ops_council.agent.duration` | histogram | ms |
| `ops_council.agent.cost_usd` | counter | usd |

These are additive to the span attributes above, not a replacement — spans
are required for this app to be useful (correlating a specific call's
tokens/cost/latency/errors); the metrics exist only for cheap dashboard
aggregation without a KQL query per view.

## What is never captured

Prompts, model responses, system instructions, subscription IDs, Azure
resource names/IDs, and endpoint URLs are never recorded as span attributes,
log messages, or metric dimensions by this app's own code. Content capture
(prompts/responses) is intentionally disabled — if you separately enable an
OpenTelemetry instrumentation that supports the optional GenAI **event/content
capture** mode (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` or
similar env vars in other GenAI instrumentations), do not enable it here. As a
result, the **`AppGenAIContent`** table this distro can populate when content
capture is enabled will stay empty for this app — that's expected, not a bug.

## KQL reference

All queries below run against the workspace tables (Properties, not
`customDimensions` — `customDimensions` is the Application Insights-classic
name for the same data; workspace-based resources expose it as `Properties`
in `AppDependencies`/`AppRequests`/`AppTraces`).

**Agent calls, by agent and model, last 24h:**

```kusto
AppDependencies
| where TimeGenerated > ago(24h)
| where Name == "agent_chat_completion"
| extend agentKey = tostring(Properties["ops.agent.key"])
| extend model = tostring(Properties["gen_ai.request.model"])
| summarize calls = count(), avgDurationMs = avg(DurationMs) by agentKey, model
| order by calls desc
```

**P50/P95 latency by agent:**

```kusto
AppDependencies
| where TimeGenerated > ago(24h)
| where Name == "agent_chat_completion"
| extend agentKey = tostring(Properties["ops.agent.key"])
| summarize p50 = percentile(DurationMs, 50), p95 = percentile(DurationMs, 95) by agentKey
| order by p95 desc
```

**Errors by agent (failed calls + exception messages):**

```kusto
AppDependencies
| where TimeGenerated > ago(24h)
| where Name == "agent_chat_completion" and Success == false
| extend agentKey = tostring(Properties["ops.agent.key"])
| join kind=leftouter (
    AppExceptions
    | where TimeGenerated > ago(24h)
    | project OperationId, ExceptionMessage = OuterMessage
) on OperationId
| project TimeGenerated, agentKey, ResultCode, ExceptionMessage
| order by TimeGenerated desc
```

**Input/output/total tokens by agent, last 24h:**

```kusto
AppDependencies
| where TimeGenerated > ago(24h)
| where Name == "agent_chat_completion"
| extend agentKey = tostring(Properties["ops.agent.key"])
| extend inputTokens = toint(Properties["gen_ai.usage.input_tokens"])
| extend outputTokens = toint(Properties["gen_ai.usage.output_tokens"])
| summarize totalInput = sum(inputTokens), totalOutput = sum(outputTokens),
            totalTokens = sum(inputTokens) + sum(outputTokens) by agentKey
| order by totalTokens desc
```

**Estimated cost by agent, last 7 days (caller-maintained pricing — see
[MODEL_CONFIGURATION.md](MODEL_CONFIGURATION.md)):**

```kusto
AppDependencies
| where TimeGenerated > ago(7d)
| where Name == "agent_chat_completion"
| extend agentKey = tostring(Properties["ops.agent.key"])
| extend estCost = todouble(Properties["ops.estimated_cost_usd"])
| summarize estimatedCostUsd = sum(estCost) by agentKey
| order by estimatedCostUsd desc
```

**Time series — calls and estimated cost per hour:**

```kusto
AppDependencies
| where TimeGenerated > ago(7d)
| where Name == "agent_chat_completion"
| extend estCost = todouble(Properties["ops.estimated_cost_usd"])
| summarize calls = count(), estimatedCostUsd = sum(estCost) by bin(TimeGenerated, 1h)
| order by TimeGenerated asc
```

**Requests/latency/errors by HTTP route (standard `AppRequests`, not custom):**

```kusto
AppRequests
| where TimeGenerated > ago(24h)
| summarize count(), avg(DurationMs), errorRate = countif(Success == false) * 100.0 / count() by Name
| order by count_ desc
```

## Connection string / secret handling

`APPLICATIONINSIGHTS_CONNECTION_STRING` is a **sensitive value** — anyone with
it can inject arbitrary telemetry into your Application Insights resource
(cost/noise impact, and potentially misleading data for anyone relying on
these dashboards). Treat it like any other secret:

* **Local dev**: leave it unset in `.env` (the documented, expected way to
  keep telemetry off locally) or reference it via a Key Vault-backed
  mechanism if your team wants local telemetry.
* **Production**: `infra/modules/monitoring.bicep` provisions Application
  Insights and `infra/modules/web-app.bicep` wires its connection string
  directly into the App Service's `APPLICATIONINSIGHTS_CONNECTION_STRING`
  application setting as part of the same deployment — you don't hand-enter
  it. Don't move it into a plain environment variable file that could be
  committed.

## Sampling and retention

* **Sampling**: `configure_azure_monitor()` (called once, idempotently, from
  `app/telemetry.py::init_telemetry`) reads standard OpenTelemetry env vars —
  e.g. `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` — directly from the
  environment. Set these as ordinary App Service settings (no Bicep parameter
  or code change needed) if you want to sample down a high-volume deployment;
  the default is to sample everything.
* **Retention**: controlled by the Log Analytics workspace's
  `retentionInDays` (`infra/modules/monitoring.bicep`, currently `30`).
  Increase it there if you need a longer telemetry history than the default.
* **`service.name`**: reported to Application Insights as `OTEL_SERVICE_NAME`.
  If unset, `app/telemetry.py::init_telemetry` derives
  `"ops-council-<profile_id>"` (e.g. `"ops-council-power"`) — a short,
  profile-safe default that never leaks a free-text brand string unless you
  set `OTEL_SERVICE_NAME` explicitly (env var, `.env`, or the
  `otelServiceName` Bicep parameter).

## Cost-estimate caveat (repeated from MODEL_CONFIGURATION.md)

`ops.estimated_cost_usd` / `ops_council.agent.cost_usd` reflect whatever
`input_cost_per_million`/`output_cost_per_million` you configure per agent —
they are **not** a query against Azure's actual billing system, and do not
account for reserved capacity, PTU, batch, or promotional pricing. Cross-check
real spend in [Azure Cost
Management](https://learn.microsoft.com/azure/cost-management-billing/) —
filter by the Azure OpenAI/AI Foundry resource(s) this app's Managed Identity
calls, over the same time window as the KQL query above.

## Verifying telemetry is enabled

`GET /api/health` reports a `config.telemetry_enabled` boolean (`true` only
when `APPLICATIONINSIGHTS_CONNECTION_STRING` was present and
`configure_azure_monitor()` succeeded at startup) — this is the safe way to
confirm telemetry is active without exposing the connection string itself.
