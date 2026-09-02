# Agent Intelligence (app/agents/analysis.py, routing.py, evidence.py, schema.py, tools.py)

Evidence-grounded, selectively-routed agent analysis built ON TOP of the
deterministic operations layer (`app/operations/`, see
`docs/EVIDENCE_MODEL.md` and `docs/OPERATIONS_API.md`) -- never on raw
Azure API dumps. This doc covers the deterministic-vs-AI boundary, the
evidence-bundle schema, the structured-output contract, selective
routing/debate policy, the typed tool registry, the approval/task-
adherence guarantees, and the evaluation metrics. See
`docs/FOUNDRY_ARCHITECTURE.md` for the current-vs-future backend split.

## Package layout

```
app/agents/
  runner.py            Existing council orchestration (/api/ask,
                        /api/demo/*, run_council[_streaming]) --
                        UNCHANGED by this layer.
  demos.py              Existing demo scenario catalog -- unchanged.
  evidence.py            build_evidence_bundle() -- bounded, redacted
                        EvidenceBundle from an OperationsSnapshot.
  schema.py               AGENT_ANALYSIS_JSON_SCHEMA, AgentAnalysisResult,
                        parse_structured_response(), validate_evidence_ids().
  routing.py              CATEGORY_AGENT_MAP, route() -- selective
                        specialist/debate routing policy.
  backend.py              ModelBackend protocol, DirectAzureOpenAIBackend
                        (what actually runs today), FoundryAgentServiceBackend
                        (NOT implemented -- see docs/FOUNDRY_ARCHITECTURE.md).
  evaluation.py            Lightweight deterministic online evaluation +
                        in-process aggregate counters.
  analysis.py              analyze_operations() / build_briefing() --
                        the orchestrator behind /api/operations/analyze
                        and /api/operations/briefing.
  analysis_routes.py       Flask blueprint (separate from
                        app/operations/routes.py -- see below).
  tools.py                 Typed, read-only tool registry (Foundry-ready
                        boundary) wrapping app/operations/{brief,queue,
                        handoff,snapshot}.py + finding_lookup.py.
app/approval.py            ApprovalTier policy shared by this layer and
                        app/ado_integration.py's existing proposal flow.
```

## The deterministic-vs-AI boundary (extended)

`docs/EVIDENCE_MODEL.md` already draws this line for the evidence layer
itself (`Finding.confidence` is always platform-derived, never model-
assigned). This layer adds exactly ONE more deterministic-vs-AI seam,
and is explicit about which side of it every field lives on:

| Field | Who decides it | Notes |
|---|---|---|
| Which finding IDs/categories/severities exist | Deterministic (`app/operations/`) | Unchanged from the evidence layer. |
| Which specialist(s)/debate/coordinator to invoke | Deterministic (`app/agents/routing.py`) | Never asks a model "who should answer this?" |
| `conclusion`, `business_impact`, `narrative`, `recommended_actions[].description` | The model | Free-text reasoning -- grounded, but not independently verifiable claim-by-claim. |
| `confidence` (high/medium/low) | The model | The model's own self-reported confidence in ITS conclusion -- NOT the evidence layer's `ConfidenceLevel` (confirmed/derived/correlated/estimated), which stays 100% deterministic and untouched. Do not confuse the two. |
| `evidence_ids` cited | The model claims them; this layer VALIDATES them | Every id is checked against the evidence bundle's own known ids (see "Citation validation" below) -- an unsupported citation is surfaced, never silently accepted or dropped. |
| `recommended_actions[].approval_required` | The model's own opinion | Advisory only. The server independently computes an `approval` tier for every action (see "Approval tiers" below) regardless of what the model said. |
| `recommended_actions[].approval.auto_executable` | Deterministic, hardcoded | Always `False` for this read-only analysis surface -- see "Task adherence" below. |

## Evidence bundle (`app/agents/evidence.py`)

`build_evidence_bundle(snapshot, *, category=, severity=, status=, finding_id=, max_items=12)`
builds a bounded, redacted view over an already-built
`OperationsSnapshot` (`app/operations/snapshot.py`) -- the SAME
priority-ordered `findings` list every other product surface
(brief/queue/handoff) already uses. It never touches `app/azure_data.py`
or any raw Azure API response directly.

```jsonc
{
  "items": [
    {
      "id": "cst-1a2b3c4d5e6f7890",
      "category": "cost", "severity": "high",
      "title": "...", "summary": "...", "business_impact": "...",
      "owner": "platform-team", "recommended_action": "...",
      "confidence": "derived",            // evidence-layer ConfidenceLevel
      "approval_required": false,
      "priority_band": "P2", "customer_impact": false,
      "workflow_status": "new",
      "evidence": [                       // at most 3, per item
        { "source": "cost_management_budget", "title": "...", "observed_at": "...", "reference": "..." }
      ]
    }
  ],
  "total_available": 7, "truncated": true,   // more items existed than max_items
  "generated_at": "...", "snapshot_id": "snap-...",
  "subscription_count": 1,                    // COUNT only, never the ids
  "coverage": { "total_sources": 22, "ok_count": 20, "...": "..." }
}
```

**Bounds/redaction, enforced in code (not just convention):**

* At most `max_items` findings (default 12, hard ceiling 25).
* At most 3 evidence references per finding.
* `resource_id` is stripped from every evidence reference (its first ARM
  path segment is the subscription GUID -- see
  `app.operations.brief.sanitize_evidence`, reused here).
* `raw_excerpt` is stripped entirely (evidence-layer sanitization already
  redacts credential-shaped substrings inside it, but a model prompt has
  no need for a raw API excerpt at all).
* `subscription_count` (an integer), never the subscription id list.
* `coverage` is the same ok/error/not_configured/not_supported COUNT
  breakdown `app.operations.service.summarize_coverage` already produces
  -- never per-source error text/detail.
* The final serialized prompt string is defense-in-depth-capped at
  ~24,000 chars (`app.agents.runner.truncate_context`, reused) even
  though the above bounds should already keep it far smaller.

An unrecognized filter value, or a `finding_id` not present in the
current snapshot, raises `EvidenceBundleError` (a `ValueError` subclass)
-- never a silently-empty bundle disguised as "not found" or "no
matches". A genuinely empty (filtered-to-nothing) bundle IS valid and
distinct: `app/agents/analysis.py` short-circuits that case to a
deterministic "insufficient evidence" answer with ZERO model calls (see
below) rather than raising.

## Structured output contract (`app/agents/schema.py`)

Every agent answer this layer treats as "the" grounded conclusion (as
opposed to free-text debate chatter) must conform to
`AGENT_ANALYSIS_JSON_SCHEMA` -- deliberately small, and strict-mode
compliant (every property is in `required`; `additionalProperties:
false` at every object level, matching Azure OpenAI/OpenAI's
`response_format={"type":"json_schema","json_schema":{"strict":true,...}}`
requirement):

| Field | Type | Notes |
|---|---|---|
| `conclusion` | string | One-sentence headline. |
| `business_impact` | string | Plain-language impact, or `""` if none. |
| `confidence` | `"high"` \| `"medium"` \| `"low"` | The model's own confidence -- see the boundary table above. |
| `evidence_ids` | string[] | Must be finding ids copied verbatim from the bundle -- validated, never trusted blindly. |
| `missing_evidence` | string[] | What would strengthen the conclusion but wasn't available. |
| `recommended_actions[]` | object[] | `{description, owner, urgency, approval_required}`. |
| `recommended_actions[].urgency` | `"immediate"` \| `"scheduled"` \| `"monitor"` | A small, closed, explainable vocabulary -- never free text. |
| `narrative` | string | 2-5 sentence grounded explanation. |

`parse_structured_response(raw_text)` is the single parser used BOTH
when the backend successfully requested `response_format=json_schema`
(as a final sanity check -- a provider is not contractually obligated to
guarantee strict-mode conformance) AND as the fallback parser when a
deployment rejected that request format. It tries, in order: the raw
text as-is, a ```` ```json ```` fenced block, and the first balanced
`{...}` span in the text -- but a response that still fails to parse, or
that has a missing/extra/mistyped field, raises `AnalysisSchemaError`.
**Malformed model output is never coerced into a "successful" grounded
answer** -- `app/agents/analysis.py` surfaces `schema_valid: false` and a
bounded `schema_error`/`raw_text_snippet` instead of guessing.

### Citation validation

`schema.validate_evidence_ids(cited_ids, bundle.known_ids())` splits
every cited id into `valid_ids`/`unsupported_ids`. The final API
response always includes BOTH `evidence_ids` (what the model claimed)
and `unsupported_evidence_ids` (the subset that doesn't exist in the
bundle it was given) -- an unsupported citation is a real, visible
signal (and drives the `citation_validity_pct`/`unsupported_citation_count`
evaluation metrics below), never silently dropped or "fixed."

## Selective routing + debate policy (`app/agents/routing.py`)

`CATEGORY_AGENT_MAP` is a fixed, deterministic table (never model-
decided) from every `FindingCategory` to exactly one specialist agent
key:

| Category | Specialist | Rationale |
|---|---|---|
| `cost`, `capacity` | `cost_sentinel` | "Finds waste, tracks burn rate, recommends rightsizing" -- capacity/quota headroom is the same cost-and-capacity-planning concern. |
| `incident`, `reliability`, `change` | `diagnostics_sre` | "Root-cause analysis ... timeline, symptoms, root cause, remediation" -- a change is the leading root-cause hypothesis for a health event. |
| `security`, `telemetry` | `scout` | "Continuously scans for anomalies, health degradation, and security drift" -- telemetry coverage gaps are a monitoring-hygiene signal scout already owns. |
| `compliance`, `ownership` | `compliance_inspector` | "Classifies Azure Policy violations ... recommends the right fix" -- a governance concern, not a reliability one. |
| `backup`, `patch`, `certificate`, `automation` | `standards_architect` | "Validates changes against ... standards, flags what a change would break" -- all "is this configured to standard?" questions. |

`route(bundle, *, requested_agents=None, force_debate=False)` returns a
`RoutingDecision` (`specialist_agents`, `coordinator_included`, `debate`,
`factors`) using this deterministic policy:

* **Routine (single specialist, coordinator only if needed, no debate)**
  when the matched evidence is single-domain, low/medium severity, and
  not customer-impacting. The coordinator (`orchestrator`) joins ONLY
  when there's more than one finding to synthesize within that single
  domain -- a single finding's own structured answer already IS the
  final answer otherwise (saving a model call).
* **Debate (every mapped specialist consulted, a rebuttal round, then a
  coordinator synthesis)** whenever ANY of:
  * `cross_domain` -- the matched evidence spans 2+ specialist domains.
  * `high_or_critical` -- any matched finding is high/critical severity.
  * `customer_impact` -- any matched finding is customer-impacting
    (`app.operations.priority.is_customer_impacting`, already computed
    per-finding and carried into the bundle as `customer_impact`).
  * `ambiguous` -- the evidence spans 3+ specialist domains (no single
    clear owner).
  * `explicit_request` -- the caller passed `agents: [...]` with 2+
    entries, or `debate: true`.

Every `RoutingDecision.factors` dict is returned in the API response
verbatim -- callers see exactly which of the above tripped (and can
audit it), never just a bare "debate: true/false" with no explanation.

### Full council is unaffected

`app/agents/runner.py::run_council`/`run_council_streaming` (used by
`/api/ask`, `/api/ask/stream`, `/api/demo/*`) are completely unchanged --
they still consult the full default specialist set on every call. This
selective-routing policy is scoped entirely to the NEW
`/api/operations/analyze`/`/briefing` endpoints; it does not alter the
existing demo/ask API's behavior.

### One coordinator voice for the executive brief

`build_briefing()` (backing `/api/operations/briefing`) always returns
exactly one `coordinator` answer (the same structured `AgentAnalysisResult`
shape as `analyze()`'s `final`) plus a `supporting_analysis` list bounded
to `{agent_key, agent, role, confidence, conclusion}` per specialist --
never each specialist's full narrative/persona text. The goal is a
single, coherent executive voice with visible (but not theatrical)
supporting detail, matching how `app.operations.brief.build_brief`
already behaves for the deterministic executive brief.

## Typed tool registry (`app/agents/tools.py`)

A Foundry-ready, read-only tool boundary -- see
`docs/FOUNDRY_ARCHITECTURE.md` for how this maps onto Foundry Agent
Service's tool-calling model. Six tools, each wrapping exactly one
already-bounded operations service:

| Tool | Wraps | Max items |
|---|---|---|
| `get_executive_brief` | `app.operations.brief.build_brief` | 1 |
| `list_prioritized_findings` | `app.operations.queue.build_queue` | 25 |
| `get_finding_evidence` | `app.operations.finding_lookup.bounded_evidence_view` | 10 |
| `get_capacity_watch` | `app.operations.handoff.capacity_watch` | 25 |
| `get_recent_changes` | `app.operations.handoff.recent_changes_since` | 25 |
| `get_source_coverage` | `OperationsSnapshot.coverage` | 1 |

There is deliberately **no generic "run this ARM/KQL query" tool** --
every tool has a fixed, explicit JSON Schema for its arguments (hand-
rolled validation in `tools.py`, no external `jsonschema` dependency for
six small fixed schemas), is marked `read_only: true`, declares a
`required_role` (`"operations_reader"` for all six today -- this app has
no real RBAC layer yet; see `docs/FOUNDRY_ARCHITECTURE.md`), a
`timeout_seconds` wall-clock bound (enforced via a small thread pool --
Python cannot forcibly cancel a running thread, so this bounds how long
the CALLER waits, not a guarantee the underlying call stops), and a
`max_result_items` bound (enforced twice: the handler's own
pagination/slicing, AND a defensive backstop that truncates any
top-level list in the result regardless).

`GET /api/operations/tools` lists every definition + schema
(introspection only). `POST /api/operations/tools/<name>` executes one
directly (`{"arguments": {...}, "roles": [...]}`) -- a tool's own
failure (denied/invalid_arguments/timeout/error) is DATA (still HTTP
200), not an HTTP error; only an unknown tool name is a 404.

Every tool execution is wrapped in an OTEL span
(`app.telemetry.tool_call_span`) recording ONLY the tool name, result
count, status, and duration -- never arguments or result content, the
same "no content capture" convention `docs/TELEMETRY.md` already
documents for agent calls. `app.telemetry.record_routing_decision` adds
the same kind of shape-only auditability for routing decisions (agent
count/debate/coordinator flags), and every model call already goes
through the existing `agent_call_span`.

## Approval tiers + task adherence (`app/approval.py`)

Six explicit tiers (never an opaque score), deterministically classified
from an action's free text (`classify_action_text`, keyword-based, most-
restrictive-tier-wins):

| Tier | Approval | Notes |
|---|---|---|
| `read_only` | Never required | Purely informational (review/monitor/document). |
| `autonomous` | Required UNLESS allowlisted + the caller is execution-capable | No caller in this app is execution-capable today -- see below. |
| `draft_ticket` | Required UNLESS allowlisted | Propose an ADO work item. |
| `draft_pr` | Required UNLESS allowlisted | Propose an ADO pull request. |
| `reversible_nonprod` | Required UNLESS allowlisted | Restart/resize/scale a non-production resource. |
| `production_write` | **ALWAYS required** | Any write/delete, network topology change, RBAC/role change, or cost commitment -- no allowlist exception, ever. |

**Task adherence guarantee:** `analysis_action_metadata(description)` --
used by `app/agents/analysis.py` for every `recommended_actions[]` entry
-- hardcodes `execution_capable=False`, because the read-only analysis
endpoint has NO code path that executes anything. This makes
`auto_executable: false` a structural guarantee, not just a convention:
every action from `/api/operations/analyze`/`/briefing` carries
`approval: {tier, human_approval_required, auto_executable: false,
allowlisted: false}` regardless of the model's own self-reported
`approval_required`, and a defensive `TaskAdherenceError` guards the
(unreachable, given the above) case where that invariant is ever broken.

**Existing ADO proposal flow (`app/ado_integration.py`) is unchanged
behaviorally** -- every proposal is still created `PENDING` and requires
an explicit human `approve_proposal`/`reject_proposal` call. This layer
only ATTACHES descriptive `approval_tier`/`approval` metadata (via
`proposal_approval_tier`) to `AdoProposal.to_dict()`, so the existing
human-gated flow is now self-describing about WHY a human must act.

## Evaluation (`app/agents/evaluation.py`)

Lightweight, deterministic, computed from already-validated structured
data -- never by re-reading prompt/response text, and never persisting
prompt/response content:

| Metric | Meaning |
|---|---|
| `schema_valid` | Did the final answer parse/validate against `AGENT_ANALYSIS_JSON_SCHEMA`? |
| `citation_count` / `valid_citation_count` / `unsupported_citation_count` | How many ids were cited, how many existed in the bundle, how many didn't. |
| `citation_validity_pct` | `valid_citation_count / citation_count` -- "of what it cited, how much was real?" |
| `citation_coverage_pct` | `distinct valid citations / bundle size` -- "of what it HAD, how much did it actually use?" |
| `action_policy_adherent` | Did every recommended action's approval metadata come back `auto_executable: false`? (see task adherence above) |

Every `/api/operations/analyze`/`/briefing` response includes its OWN
`evaluation` object, AND updates an in-process, thread-safe aggregate
counter (`get_aggregate_summary()`, surfaced on `/api/health`) plus OTEL
counters (`app.telemetry.record_evaluation_metrics`, a no-op unless
telemetry is configured) -- satisfying "persist aggregate counters OR
emit OTEL metrics" with both, since the in-process counters are directly
testable without a real Application Insights backend.

## Current runtime (honest statement)

This app calls **Azure OpenAI directly** via
`DirectAzureOpenAIBackend` (`app/agents/backend.py`) for every agent
call in this layer, exactly like the existing `/api/ask` council. It
does **not** use Azure AI Foundry Agent Service at runtime.
`FoundryAgentServiceBackend` exists as a second implementation of the
same `ModelBackend` protocol, but its `.complete()` always raises
`NotImplementedError` -- setting `AGENT_BACKEND=foundry` fails loudly
(HTTP 501) rather than silently continuing to use Direct while claiming
otherwise. See `docs/FOUNDRY_ARCHITECTURE.md` for the concrete migration
plan.

`/api/health` and every analysis/briefing response include
`agent_definition_version` (a version fingerprint for the loaded
profile's agent definitions) and per-agent `prompt_version` (see
`app/config.py` -- both default to a deterministic hash so they're never
blank/fabricated) plus `model_metadata.backend` (which backend actually
served this request).

## Autonomy matrix (summary)

| Capability | Today | Notes |
|---|---|---|
| Read deterministic evidence (findings/brief/queue/coverage) | Autonomous, always | No approval needed -- pure reads via `app/operations/`. |
| Produce a grounded conclusion/narrative/recommended actions | Autonomous, always | The MODEL runs freely; every output is still citation-checked and schema-validated before being trusted. |
| Propose an ADO ticket/PR (`draft_ticket`/`draft_pr`) | Requires human approval | Existing `app/ado_integration.py` flow -- `PENDING` until a human approves. |
| Restart/resize/scale a non-prod resource (`reversible_nonprod`) | Requires human approval | No allowlist is enabled by default anywhere in this app. |
| Any production write/delete/network/RBAC/cost commitment | **Always requires human approval** | No exception, no allowlist, ever (`app/approval.py`). |
| Auto-execute anything from `/api/operations/analyze`/`/briefing` | **Never** | Structural guarantee -- see "Task adherence" above. |
