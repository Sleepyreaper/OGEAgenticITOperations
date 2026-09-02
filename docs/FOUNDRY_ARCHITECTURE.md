# Foundry Architecture: current runtime vs. a concrete migration plan

**Honest, up-front statement:** this application calls **Azure OpenAI
directly** today, via `DirectAzureOpenAIBackend`
(`app/agents/backend.py`) and `app/agents/runner.py::call_agent` (the
pre-existing council path). It does **not** use Azure AI Foundry Agent
Service, Foundry threads, Foundry-managed tool-calling, or Foundry
evaluations at runtime, anywhere in this codebase, as of this writing.
Any place in this repo that mentions "Foundry" (`FoundryConfig`,
`FoundryAgentServiceBackend`, `foundry-gpt` deployment names, etc.) is
either inert configuration metadata or an explicitly-`NotImplementedError`
stub -- never a functional integration dressed up to look like one. This
doc exists so that gap is never accidentally papered over, and so a real
migration has a concrete, actionable plan to follow.

## Why "direct" today, and why that's a reasonable starting point

The existing `/api/ask` council (`app/agents/runner.py`) and this
repo's new evidence-grounded analysis layer (`app/agents/analysis.py`,
see `docs/AGENT_INTELLIGENCE.md`) both need exactly one thing from a
model backend: "given these messages, return text (ideally
schema-constrained JSON)." `DirectAzureOpenAIBackend` does that with the
`openai` Python SDK's `chat.completions.create`, `AzureADTokenProvider`-
based auth (`azure.identity`), and `response_format={"type":
"json_schema", ...}` for structured output when the deployment supports
it. This is simple, well-understood, and already has real OTEL
telemetry (`app/telemetry.py`) and per-agent model/endpoint/cost
configuration (`app/config.py`) built around it.

## The seam: `ModelBackend` protocol (`app/agents/backend.py`)

```python
class ModelBackend(Protocol):
    name: str
    def complete(self, agent_config, messages, *, json_schema=None, schema_name="") -> BackendCompletion: ...
```

`app/agents/analysis.py` (and, in the future, `app/agents/runner.py`)
only ever talk to this protocol -- never to the `openai` SDK directly.
That means a real Foundry integration is a SECOND implementation of this
same interface (`FoundryAgentServiceBackend`), not a rewrite of the
orchestration/routing/evidence layers above it. Selecting a backend is
one environment variable: `AGENT_BACKEND=direct` (default) or
`AGENT_BACKEND=foundry` (today: fails loudly with `NotImplementedError`
-> HTTP 501, on purpose -- see `app/agents/backend.py::FoundryAgentServiceBackend`).

## Concrete migration plan (not yet implemented)

### 1. Tools -> Foundry Agent Service tool definitions

`app/agents/tools.py`'s registry (`get_executive_brief`,
`list_prioritized_findings`, `get_finding_evidence`,
`get_capacity_watch`, `get_recent_changes`, `get_source_coverage`) is
already shaped for this: each `ToolDefinition.parameters_schema` is a
plain JSON Schema, which is exactly the shape Foundry Agent Service (and
OpenAI-style function calling generally) expects for a tool/function
definition. The migration work is:

* Register each `ToolDefinition` as a Foundry Agent Service tool
  (`name`, `description`, `parameters_schema` map directly).
* Wire Foundry's tool-call events to `app.agents.tools.execute_tool` as
  the handler -- the read-only/authorization/timeout/result-bound
  behavior in `execute_tool` needs no changes; only the transport
  (Foundry's tool-call protocol instead of a direct Python call) does.
* No new tool needs to be invented, and -- deliberately -- no generic
  ARM/KQL execution tool should EVER be added, in Foundry or otherwise;
  every tool stays a named, bounded, read-only wrapper over an existing
  operations service.

### 2. Threads: per-incident and per-day

Foundry Agent Service's thread model maps naturally onto two units this
app already has:

* **Per-incident thread** -- one Foundry thread per `Finding.id` (or per
  correlated incident group), so a specialist's reasoning about one
  finding persists/accumulates across follow-up questions instead of
  re-deriving context from scratch on every call (today's stateless
  `messages` list rebuild in `app/agents/analysis.py::_build_messages`).
* **Per-day thread** -- one Foundry thread per day per subscription
  scope, backing `/api/operations/briefing`'s daily executive briefing,
  so "what changed since yesterday's briefing" becomes a natural thread
  continuation instead of the current `app.operations.handoff`-style
  timestamp diffing.

Both would still be seeded EXCLUSIVELY from `EvidenceBundle`s (never raw
Azure data) -- the grounding discipline in `docs/AGENT_INTELLIGENCE.md`
does not change just because the transport does.

### 3. Managed identity auth

`DirectAzureOpenAIBackend` already authenticates via
`azure.identity.ManagedIdentityCredential`/`DefaultAzureCredential`
(`app/agents/runner.py::_get_client`) -- no API keys anywhere in this
app. A Foundry backend should use the exact same credential chain
against the Foundry project endpoint (`FoundryConfig.project_endpoint`),
so the migration doesn't regress this app's existing "no static
secrets for model auth" posture.

### 4. Tracing

`app/telemetry.py` already emits OTEL spans for every agent call
(`agent_call_span`), every tool call (`tool_call_span`), and routing
decisions (`record_routing_decision`) -- all content-free (tool
name/result count/duration/status only, never prompts/responses/tool
arguments). A Foundry backend should emit into the SAME
`app.telemetry` spans/counters (parent span per analysis request, child
spans per agent/tool call) rather than introducing a second, parallel
telemetry surface -- so existing KQL queries/dashboards
(`docs/TELEMETRY.md`) keep working unchanged regardless of which backend
served a given request.

### 5. Evaluations

`app/agents/evaluation.py`'s deterministic metrics (schema validity,
citation validity/coverage, unsupported-citation count, action-policy
adherence) are backend-agnostic by construction -- they're computed from
the already-parsed `AgentAnalysisResult`, not from anything
Foundry-specific. A Foundry migration should ALSO wire Foundry's own
native evaluation/observability features (if/when adopted) as an
ADDITIONAL signal, not a replacement -- this app's own deterministic
evaluation should keep running regardless of backend, since it's the
one guarantee that doesn't depend on trusting a third-party evaluation
pipeline.

### 6. Prompt Shields / Task Adherence (Azure AI Content Safety)

Two DISTINCT existing Azure AI Foundry/Content Safety features are
relevant here, and neither is implemented in this repo today:

* **Prompt Shields** -- detects prompt-injection attempts (e.g. an
  Azure Activity Log entry or Advisor recommendation title crafted to
  contain instructions for the model). Since this app's evidence bundle
  DOES include free-text fields sourced from Azure (finding titles/
  summaries/business_impact -- see `docs/AGENT_INTELLIGENCE.md`'s
  evidence-bundle schema), a real deployment ingesting untrusted/
  attacker-influenced Azure resource metadata should run the evidence
  bundle's text fields through Prompt Shields before they reach a model
  prompt. This is a genuine, currently-open gap this repo does not
  claim to close.
* **Task Adherence** (Azure AI Foundry's own agent-evaluator feature) --
  a MODEL-based check that an agent's output stayed within its intended
  task scope. This is complementary to, not a replacement for, this
  repo's own DETERMINISTIC task-adherence guarantee (`app/approval.py`'s
  `analysis_action_metadata` hardcoding `auto_executable: false` for the
  read-only analysis surface -- see `docs/AGENT_INTELLIGENCE.md`). The
  deterministic guarantee should remain the primary control even after
  adopting Foundry's Task Adherence evaluator, since it holds even if
  the evaluator itself is wrong/bypassed/unavailable.

## Model routing (current state, and what Foundry adds)

Today, "model routing" means: `app/config.py`'s per-agent
`deployment`/`endpoint`/`api_version` configuration (profile-driven, see
`docs/MODEL_CONFIGURATION.md`) plus `app/agents/routing.py`'s
category-to-specialist mapping (see `docs/AGENT_INTELLIGENCE.md`) --
i.e. "which Azure OpenAI deployment, behind which specialist persona."
A Foundry Agent Service migration would ADD a second routing dimension:
Foundry's own agent/thread orchestration could, in principle, route a
conversation to a pre-registered Foundry agent definition instead of a
raw chat-completion call -- but the CATEGORY -> SPECIALIST mapping
itself should stay exactly as deterministic as it is today (see
`app/agents/routing.py`'s `CATEGORY_AGENT_MAP`); Foundry should change
the TRANSPORT of a routing decision, never decide the routing itself.

## Non-goals of this migration plan

* This is a plan, not a partially-built integration -- there is no
  Foundry SDK dependency added to `requirements.txt` by this repo, and
  no Bicep infrastructure provisioned for Foundry, until this plan is
  actually implemented (see `.env.example`'s `FOUNDRY_*` variables,
  which are documented as inert metadata-only today).
* Foundry adoption should never become a way to relax this repo's
  existing guarantees (bounded evidence, citation validation, strict
  schema parsing, deterministic approval tiers, no auto-execution from a
  read-only surface) -- every one of those must hold identically
  regardless of which `ModelBackend` implementation is active.
