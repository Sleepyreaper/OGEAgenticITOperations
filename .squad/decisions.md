# Squad Decisions

## Active Decisions

### D1 — Deterministic core, model-assisted narrative (Lead)
Routing (`app/agents/routing.py`), evidence selection (`app/agents/evidence.py`) and approval metadata (`app/approval.py`) are plain Python. Foundry/the model never decides which specialists run, which subscriptions are in scope, or whether anything executes. `auto_executable` is always False.

### D2 — Read-only tool registry only (Lead, Evidence Engineer)
Agents may only call tools in `app/agents/tools.py`. No generic ARM, KQL, shell or HTTP tool. `subscription_ids` and `force_refresh` are stripped from the model-facing schema and bound server-side from `ToolContext`.

### D3 — Foundry Agent Service is the runtime squad's backend (Foundry Engineer)
`AGENT_BACKEND=foundry` publishes one versioned Foundry agent per (specialist, output schema), named `<FOUNDRY_AGENT_PREFIX>-<agent>-<schema>`. New versions are created only when the definition hash changes. Auth is managed identity (`AZURE_CLIENT_ID`) or `DefaultAzureCredential`. The direct Azure OpenAI backend remains the default and the fallback.

### D4 — Tokens are an ops KPI (Foundry Engineer)
Every analysis returns `usage_summary` (total tokens, estimated cost, model/tool calls, per-round and per-agent tokens, tokens per cited finding) and emits OTEL token metrics. Pricing is caller-maintained per agent (`AGENT_<KEY>_INPUT_COST_PER_MILLION` / `_OUTPUT_COST_PER_MILLION`); Azure Cost Management is billing truth.

### D5 — Parallel fan-out, bounded (Lead)
Specialist and rebuttal rounds run in parallel (`ANALYSIS_MAX_PARALLEL_SPECIALISTS`, default 4, `1` = sequential), preserving routing order in output. The tool loop is bounded by `FOUNDRY_MAX_TOOL_ROUNDS`.

### D6 — Tests are plain scripts (Tester)
`tests/test_*.py` use a PASS/FAIL counter and exit 1 on failure; run with `.venv/bin/python3`. No live Azure/Foundry calls in the suite — use fakes.

### D7 — Azure DevOps, not GitHub Actions (Lead)
Squad initialized with `--no-workflows`. Pipelines live in `pipelines/`.

### D8 — Functional agent names; "what/how" derived from code (Lead)
Display names describe the job (Operations Coordinator, Cost & Capacity Analyst, Incident & Change Investigator, Security & Monitoring Analyst, Policy & Governance Advisor, Resilience & Hygiene Engineer). Agent *keys* never change. `app/agents/catalog.py` derives each agent's categories, evidence, tools and Foundry agent name from routing/tools; it feeds `/api/agents`, the Agent Squad page and the generated `docs/AGENTS.md` (`scripts/generate_agent_docs.py --check` is tested).

### D9 — Client-ready repo (Lead)
`main` must contain no tenant, subscription, resource or personal identifiers — examples use `<placeholders>`. Customer-specific overlays live on `showcase/<customer>` branches.

### D10 — ZeroOps: Azure SRE Agent is tier 1, the squad is tier 2 (Lead)
The Azure SRE Agent triages and fixes runbook-shaped incidents; it escalates ambiguous, cross-domain or business-impacting ones over MCP (`/mcp`, connector `ogeops`, API-key auth, 503 when unset). The squad never changes Azure: it returns analysis, a script and a proposal that a human approves. The escalation ledger (SQLite) is the source of truth because ADO proposals are per-worker in memory. Cost = SRE AAU estimate (published token profiles × `SRE_AGENT_AAU_PRICE_USD`) + measured Foundry tokens. Demo chaos only runs when `ZEROOPS_CHAOS_ENABLED=true` against `ZEROOPS_DEMO_*` resources. `tests/test_zeroops.py` keeps `sre-agent/` in sync with the MCP tool list.

### D11 — Repo-local production specialists and independent model review (Lead)
OGE's Build Squad includes dedicated owners for ZeroOps/SRE automation, security/identity, durable state/workflow, platform release/IaC and operations UX. These roles load only in this repository and do not expand the user's general business squad. The coordinator selects models per task; implementation and evaluation/review use different model families when available. Same-family exceptions are recorded and require deterministic validation before acceptance.

## Governance

- All meaningful changes require team consensus
- Document architectural decisions here
- Keep history focused on work, decisions focused on direction

### 2026-10-06T17:21:58.006-04:00: Activity Proof — Release Recorded

#### Activity Proof — Release Recorded

**Date:** 2026-10-06T17:21:58.006-04:00
**By:** Scribe (session)
**Status:** recorded (decisions/inbox) — awaiting Scribe merge to decisions.md

##### Context
User-authorized implementation: "make a plan and get to work." Implementation completed and validated; the following accepted decisions are recorded through the normal decisions inbox/merge process.

##### Accepted decisions
1. Activity Proof uses durable investigations, runs, append-only bounded events and artifacts in OPERATIONS_STATE_DB; no synthetic history is backfilled.
2. Public activity APIs expose allowlisted sanitized projections only. Provider payloads, prompts, chain-of-thought, resource/subscription IDs, secrets, scripts and raw model output are not activity proof.
3. Foundry/direct execution is labeled from observed run data, not configuration. Direct is an operator-selected alternative, not automatic Foundry failure fallback.
4. MCP summaries and SRE thread creation are reported/observed request facts, not proof of provider investigation, execution or verification. Approval and operator closure do not imply execution or verification.
5. PUBLIC_DEMO_MODE is a server-enforced deny-by-default public gate for live/raw/mutating APIs; it is not operator authentication or RBAC and defaults false.
6. No-evidence, invalid-output and failed runs are non-proposal outcomes. Proposal and decision activity receipts are retry-repairable and idempotent.
7. Independent reviewer rejection lockout was enforced: Evidence Engineer and State & Workflow Engineer revised artifacts originally authored by Foundry, ZeroOps and Operations UX; Lead re-review approved.

##### Validation
- 52/52 tests/test_*.py scripts passed.
- `scripts/generate_agent_docs.py --check` passed under .venv.
- `git diff --check` passed.
- `az bicep build --file infra/main.bicep` passed.
- Independent Lead reviewer APPROVED after five rejected findings were corrected.

##### Consequences & Next Steps
- Scribe (background) will merge this inbox entry into `.squad/decisions.md` per the merge process (demote headings, append, verify, delete inbox file).
- After merge, Scribe will propagate a short update to affected agent histories: Evidence Engineer, State & Workflow Engineer, Foundry, ZeroOps, Operations UX, and Lead.
- Public-facing APIs remain deny-by-default until server gate config permits.

---
Record ID: activity-proof-2026-10-06T17-21-58-006-04-00
Tags: activity-proof, release, validation, approved
