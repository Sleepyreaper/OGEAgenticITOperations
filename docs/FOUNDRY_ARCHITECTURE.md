# Foundry Architecture: the runtime squad on Azure AI Foundry

The operations analysis layer (`/api/operations/analyze`,
`/api/operations/briefing`) is a **squad**: a deterministic coordinator
routes a bounded evidence bundle to the right specialists (scout,
cost_sentinel, diagnostics_sre, standards_architect,
compliance_inspector), optionally runs a rebuttal round, and has the
orchestrator synthesize one cited answer. Since this release the squad
can run on **Azure AI Foundry Agent Service** — each specialist is a real,
versioned Foundry agent — and every run reports a token ledger so model
spend is a first-class operations KPI.

> Pattern credit: the "squad" shape (specialists + coordinator, parallel
> fan-out, persistent team memory, human approval gate) follows the
> open-source [`bradygaster/squad`](https://github.com/bradygaster/squad) project. The repo's
> *Build Squad* (the agents that develop this app) lives in `.squad/`;
> the *runtime squad* described here lives in `profiles/<profile>/`.

## Backends

`AGENT_BACKEND` selects one implementation of the `ModelBackend`
protocol (`app/agents/backend.py`):

| Backend | What it does | When to use |
|---|---|---|
| `direct` (default) | Azure OpenAI chat completions per agent call (`app/agents/runner.py`) | Simple deployments, no Foundry project |
| `foundry` | One versioned Foundry agent per specialist, invoked through the Foundry Responses API with a bounded tool loop | Agents visible/governed in the Foundry portal, function tools, Foundry tracing/evals, token governance |

Routing (`app/agents/routing.py`), evidence selection
(`app/agents/evidence.py`), approvals (`app/approval.py`) and evaluation
(`app/agents/evaluation.py`) are **identical** for both. Foundry changes
the transport of a routing decision — it never decides routing, scope,
or execution.

The backend is selected once by the operator through `AGENT_BACKEND`.
`direct` is an explicit alternative runtime, **not** an automatic
post-failure fallback for `foundry`. Authentication, service, rate-limit,
or runtime failures from the selected backend remain failures and are
recorded as such. The direct backend's structured-output retry is narrower:
it retries the same Azure OpenAI deployment without `response_format` only
when that deployment explicitly rejects JSON-schema response format; it
does not switch model backends.

## Durable execution proof

Every synchronous analysis or briefing opens an Activity investigation and
starts a run before model work. The existing response shape is preserved and
adds:

```json
"activity": {
  "investigation_id": "inv-...",
  "run_id": "run-..."
}
```

Callers such as ZeroOps can pass an existing `investigation_id` to
`analyze_operations()` so multiple filter-ladder attempts remain separate
runs under one investigation. HTTP analysis opens a new investigation with
the closed `api` / `analysis` origin-trigger pair; briefings use
`briefing` / `briefing`.

The Activity run records only bounded proof:

| Proof | Direct Azure OpenAI | Foundry Agent Service |
|---|---|---|
| `actual_backend` | `direct_azure_openai` after an observed completion | `foundry_agent_service` after an observed completion |
| Provider response IDs | Not recorded | Every available Responses API response ID |
| Model | Direct deployment/model metadata | Actual `response.model` when returned, otherwise the agent definition deployment |
| Local tools | None | One receipt per locally executed registry tool: agent key, tool round, tool name, status, duration, result count |
| Finish evidence | Chat completion finish reason | Responses status, plus explicit `tool_round_limit` termination |

Tool receipts never include arguments, server-bound subscription scope,
tool output, resource IDs, URLs, or secrets. Run receipts contain agent and
round outcomes, schema validity, finish statuses, measured usage when the
provider returned it, and citation metadata only for finding IDs that passed
bundle validation. Raw provider/model payloads and `raw_text_snippet` are
never persisted.

Activity persistence is part of the operation, not best-effort telemetry.
If proof cannot be written, the request fails instead of returning an
unrecorded success. Zero matching evidence finishes as
`insufficient_evidence` with `actual_backend: none` and null durable usage;
the compatibility `usage_summary` in the synchronous response remains
present, but it is not persisted as invented zero-cost execution. Invalid
coordinator output finishes as `invalid_output`; provider or orchestration
exceptions finish as `failed`.

## How the Foundry backend works

```
analyze_operations()                      (deterministic Python)
  ├─ route(bundle) ──► [specialists]      CATEGORY_AGENT_MAP, never a model
  ├─ fan-out (ThreadPool, ANALYSIS_MAX_PARALLEL_SPECIALISTS)
  │    └─ FoundryAgentServiceBackend.complete(agent, messages, tool_context)
  │         ├─ ensure_agent(): publish version only if definition hash changed
  │         ├─ responses.create(agent_reference=<prefix>-<agent>-<schema>)
  │         ├─ while function_call and rounds < FOUNDRY_MAX_TOOL_ROUNDS:
  │         │     execute_tool(name, args + server-bound subscription scope)
  │         │     responses.create(function_call_output, previous_response_id)
  │         └─ usage → telemetry.record_usage + returned usage dict
  ├─ optional rebuttal round (same fan-out)
  ├─ orchestrator synthesis
  └─ citation validation, approval metadata, evaluation, usage_summary
```

### Agents

* **Naming:** `<FOUNDRY_AGENT_PREFIX>-<agent-key>-<schema>`, e.g.
  `oge-ops-cost-sentinel-agent-analysis-result` (≤ 63 chars, alphanumeric
  ends). One agent per (specialist, output schema) because Foundry
  rejects a per-call `text` format when an agent is referenced — the JSON
  schema must be baked into the agent version.
* **Definition:** `PromptAgentDefinition` = the agent's system prompt
  (instructions), model deployment, temperature (if supported), the
  structured-output JSON schema, and the read-only tool registry as
  `FunctionTool`s.
* **Versioning:** the definition is hashed; `ensure_agent()` publishes a
  new version **only** when the hash differs from the latest version's
  `metadata.definition_hash` (cached per process). Prompt edits in
  `profiles/*/prompts/` therefore become new Foundry agent versions
  automatically on next use, and are visible/diffable in the portal.
* **Model:** the agent's own deployment (`AGENT_<KEY>_DEPLOYMENT` or the
  profile default) unless `FOUNDRY_MODEL_DEPLOYMENT` forces one for all.
  Deployment names must exist in the Foundry project.

### Tools

The six read-only tools in `app/agents/tools.py` are exposed as Foundry
function tools. Safety properties:

* `subscription_ids` and `force_refresh` are **removed** from the
  model-facing schema and **bound server-side** from `ToolContext`
  (the request's snapshot scope). Any model-supplied value is overwritten.
  With no scope bound, tools return an explicit error.
* Tool output is truncated to `FOUNDRY_MAX_TOOL_OUTPUT_CHARS`.
* The loop is bounded by `FOUNDRY_MAX_TOOL_ROUNDS`; hitting the limit
  returns `finish_reason = "tool_round_limit"` (never an unbounded loop).
  Calls present on the limit response are not executed and do not receive
  tool receipts.
* `FOUNDRY_ENABLE_TOOLS=false` publishes agents with no tools.
* There is deliberately **no generic ARM/KQL/shell/HTTP tool**.

### Auth

`ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)` when
`AZURE_CLIENT_ID` is set (App Service), else `DefaultAzureCredential`
(local `az login`). No keys. The identity needs **Foundry User**
(role `53ca6127-db72-4b80-b1b0-d745d6d5456d`, formerly "Azure AI User")
on the Foundry account — it covers both publishing agent versions and
calling the Responses API.

### Live-verified API notes (`azure-ai-projects` 2.7.0, `openai` 3.x)

* Input items must carry `"type": "message"` (else 400 *Invalid value ''*).
* Per-call `text`/JSON-schema is rejected with an agent reference
  (*Not allowed when agent is specified*) → schema lives on the agent.
* The system prompt goes in agent `instructions`; any other system
  message in the conversation is sent as a `developer` message.
* Tool results go back as `{"type": "function_call_output", "call_id",
  "output"}` with `previous_response_id`.
* `usage.input_tokens`/`output_tokens` are summed across tool rounds.

## Token usage as an ops KPI

Every `/api/operations/analyze` response (and `/briefing`) includes a
`usage_summary`. Illustrative shape (5-specialist debate run on the demo
snapshot, live Foundry, `gpt-5.6-luna`, ~30 s with parallel fan-out):

```json
"usage_summary": {
  "prompt_tokens": 57800, "completion_tokens": 7600, "total_tokens": 65400,
  "estimated_cost_usd": 0.0,
  "tool_calls": 0, "model_calls": 11,
  "per_round_tokens": {"specialists": 23271, "rebuttals": 34719, "synthesis": 7410},
  "per_agent_tokens": {"scout": 10854, "cost_sentinel": 11363, "orchestrator": 7410, "...": "..."},
  "cited_finding_count": 1,
  "tokens_per_cited_finding": 65400
}
```

Note the rebuttal round is ~60% of specialist+rebuttal spend in that
run — exactly the kind of signal the KPIs below are for.

Each specialist/rebuttal/final outcome also carries its own `usage`
(`prompt_tokens`, `completion_tokens`, `tool_calls`, `tool_rounds`,
`foundry_agent`), and `app/telemetry.py` emits the same numbers as OTEL
metrics/span attributes into Application Insights (see
`docs/TELEMETRY.md`).

Suggested KPIs for the ops review / weekly retro:

| KPI | Source | Why |
|---|---|---|
| Tokens per cited finding | `usage_summary.tokens_per_cited_finding` | Efficiency of grounded output |
| Debate premium | `per_round_tokens.rebuttals / total_tokens` | Is the rebuttal round worth it? |
| Tokens per agent | `per_agent_tokens` | Right-size model tiers per specialist |
| Tool calls per run | `usage_summary.tool_calls` | Agents pulling extra evidence vs. bundle |
| Citation validity at cost | `evaluation.citation_validity_pct` vs tokens | Accuracy per token |
| Cost per resolved finding | `estimated_cost_usd` / findings resolved | Business value of agent spend |

`estimated_cost_usd` is computed from per-agent pricing
(`AGENT_<KEY>_INPUT_COST_PER_MILLION` / `_OUTPUT_COST_PER_MILLION`, see
`docs/MODEL_CONFIGURATION.md`) and is 0 until you set it. Azure Cost
Management and the Foundry portal's usage views remain billing truth.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_BACKEND` | `direct` | `foundry` to enable |
| `FOUNDRY_PROJECT_ENDPOINT` | — | `https://<account>.services.ai.azure.com/api/projects/<project>` (required) |
| `FOUNDRY_AGENT_PREFIX` | `oge-ops` | Agent name prefix (use per-environment prefixes to share a project) |
| `FOUNDRY_MODEL_DEPLOYMENT` | — | Force one deployment for all agents |
| `FOUNDRY_ENABLE_TOOLS` | `true` | Expose the read-only tool registry |
| `FOUNDRY_MAX_TOOL_ROUNDS` | `4` | Tool loop bound |
| `FOUNDRY_MAX_TOOL_OUTPUT_CHARS` | `12000` | Per-tool-result truncation |
| `ANALYSIS_MAX_PARALLEL_SPECIALISTS` | `4` | Fan-out width (both backends); `1` = sequential |

`/api/health` → `backend` reports `active_backend`, `foundry_configured`,
`foundry_implemented: true` and `foundry_agent_prefix`.

## Infrastructure

Both IaC flavors support reusing an existing Foundry project or creating one.

**Bicep** (`infra/main.bicep`):

| Param | Meaning |
|---|---|
| `foundryMode` | `none` (default) / `existing` (grant Foundry User on `foundryAccountName` in `foundryResourceGroup`) / `new` (create account + project + `foundryModelDeployments` + RBAC + diagnostics to the app's Log Analytics) |
| `foundryProjectEndpoint` | Endpoint for `existing`/`none` (derived for `new`) |
| `foundrySettings` | `{ agentPrefix, modelDeployment, enableTools, maxToolRounds, maxToolOutputChars, maxParallelSpecialists }` |
| `agentBackend` | Set to `foundry` to switch the app |

Modules: `infra/modules/foundry.bicep` (new) and
`infra/modules/foundry-rbac.bicep` (existing).

**Terraform** (`infra/terraform/foundry/`): `mode = "existing" | "new"`,
outputs `project_endpoint` and the app settings to apply. See its
`README.md`.

Example — reuse an existing project:

```bicep
param agentBackend = 'foundry'
param foundryMode = 'existing'
param foundryAccountName = '<your-foundry-account>'
param foundryResourceGroup = '<your-foundry-resource-group>'
param foundryProjectEndpoint = 'https://<your-foundry-account>.services.ai.azure.com/api/projects/<your-project>'
param agentOverrides = {
  orchestrator: { deployment: 'gpt-5.6-terra' }
  standards_architect: { deployment: 'gpt-5.6-terra' }
  cost_sentinel: { deployment: 'gpt-5.6-sol' }
  diagnostics_sre: { deployment: 'gpt-5.6-sol' }
  compliance_inspector: { deployment: 'gpt-5.6-sol' }
  scout: { deployment: 'gpt-5.6-luna' }
}
```

## Roadmap (not yet implemented)

1. **Threads per finding / per day** — persist a Foundry conversation per
   `Finding.id` and per daily briefing so follow-ups continue context
   instead of rebuilding it; still seeded only from `EvidenceBundle`s.
2. **Ralph as a scheduled job** — a Container Apps job that runs the
   daily briefing and posts a token/finding digest (Squad "Ralph" pattern).
3. **Ceremonies** — daily briefing, weekly eval + token retro driven by
   the KPIs above.
4. **Foundry evaluations** — wire Foundry's evaluators (incl. Task
   Adherence) as an *additional* signal; the deterministic
   `app/agents/evaluation.py` and `auto_executable: false` stay primary.
5. **Prompt Shields** — evidence bundles include Azure-sourced free text
   (finding titles/summaries); screen them before they reach a prompt.
   This is an open gap.
6. **New specialists** — AKS SRE, network/ExpressRoute, observability,
   FinOps — each justified by eval accuracy gain vs. token cost.
7. **Token dashboard** — Workbook over the OTEL token metrics.

## Non-negotiables (both backends)

Bounded evidence, citation validation, strict schema parsing,
deterministic routing and approval tiers, no generic query tools, and no
auto-execution from a read-only surface hold identically regardless of
which `ModelBackend` is active.
