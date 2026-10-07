# Work Routing

How to decide who handles what.

## Routing Table

| Work Type | Route To | Examples |
|-----------|----------|----------|
| Triage, architecture, design review | Lead | "should agents get a KQL tool?", routing/approval policy, PR review |
| Evidence sources & findings | Evidence Engineer | new collector, finding category, tool in `app/agents/tools.py`, brief/queue/handoff |
| Models, Foundry, tokens | Foundry Engineer | backend changes, new specialist agent, model tier, prompt version, token dashboard, Foundry Bicep/Terraform |
| ZeroOps and Azure SRE Agent | ZeroOps SRE Engineer | MCP escalation, response plans, probes, reversible runbooks, chaos scenarios, incident replay |
| Security, identity and trust boundaries | Security & Identity Engineer | Entra/Easy Auth, RBAC, Managed Identity, Key Vault, private networking, MCP/API auth, Prompt Shields |
| Durable state and workflow reliability | State & Workflow Engineer | SQLite/state migrations, idempotency, durable jobs, distributed cache, multi-worker behavior |
| Deployment, IaC and release | Platform Release Engineer | Bicep/Terraform, setup wizard, Azure Pipelines, environment promotion, rollback, dependency maintenance |
| Product UI and operations workflow | Operations UX Engineer | Executive Brief, Operations Center, SSE, accessibility, workflow controls, frontend error handling |
| Tests, evals and adversarial QA | Tester | regression test, fake backend, eval golden set, prompt injection, task adherence, model/prompt A/B, coverage review |
| Decisions & session logs | Scribe | merge `.squad/decisions/inbox/` (always background) |
| Backlog monitoring | Ralph | scheduled triage of open work items |

Preset installation adds concrete routes for the configured team. Add or edit rows
here only when their agent names also exist in the casting registry.

## Repo Notes

- CI/CD runs on **Azure DevOps** (`pipelines/`), not GitHub Actions — Squad was initialized with `--no-workflows`; do not add `.github/workflows/`.
- One logical change per commit/PR; the Lead approves before merge.
- Runtime Ops Council agents are product features, not Build Squad members. Route repository implementation to this file's build agents.

## Model Routing

The coordinator MUST choose the model per task and pass an explicit model when
spawning implementation or review agents. Role prompts do not count as model
diversity.

| Work profile | Preferred model capability |
|--------------|----------------------------|
| Architecture, Foundry orchestration, complex incident workflows | Deep reasoning |
| Python, Flask, collectors, state, IaC and release implementation | Code-specialized |
| Security review and adversarial evaluation | Independent reasoning family |
| Operations UI and accessibility | Frontend-capable; multimodal when visual review helps |
| Scribe and Ralph bookkeeping | Fast, low-cost |

When Tester, Security & Identity Engineer, Rai or Fact Checker reviews an
implementation, use a different model family from the primary author when
available. If unavailable, record the same-family exception in the work log and
require deterministic tests or direct evidence before acceptance.

## Issue Routing

| Label | Action | Who |
|-------|--------|-----|
| `squad` | Triage: analyze issue, assign `squad:{member}` label | Lead |
| `squad:{name}` | Pick up issue and complete the work | Named member |

### How Issue Assignment Works

1. When a GitHub issue gets the `squad` label, the **Lead** triages it — analyzing content, assigning the right `squad:{member}` label, and commenting with triage notes.
2. When a `squad:{member}` label is applied, that member picks up the issue in their next session.
3. Members can reassign by removing their label and adding another member's label.
4. The `squad` label is the "inbox" — untriaged issues waiting for Lead review.

## Rules

1. **Eager by default** — spawn all agents who could usefully start work, including anticipatory downstream work.
2. **Scribe always runs** after substantial work, always as `mode: "background"`. Never blocks.
3. **Quick facts → coordinator answers directly.** Don't spawn an agent for "what port does the server run on?"
4. **When two agents could handle it**, pick the one whose domain is the primary concern.
5. **"Team, ..." → fan-out.** Spawn all relevant agents in parallel as `mode: "background"`.
6. **Anticipate downstream work.** If a feature is being built, spawn the tester to write test cases from requirements simultaneously.
7. **Issue-labeled work** — when a `squad:{member}` label is applied to an issue, route to that member. The Lead handles all `squad` (base label) triage.
8. **Cross-cutting production changes require specialist review.** Auth/RBAC changes include Security & Identity; durable-state changes include State & Workflow; deployability changes include Platform Release; user-visible workflow changes include Operations UX.
