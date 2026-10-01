# ZeroOps: Azure SRE Agent + OGE Agentic Ops squad

> **Vision.** Nobody investigates from scratch. The **Azure SRE Agent** watches repos, logs and
> monitoring 24×7 and fixes what it safely can. When an incident is ambiguous, cross-domain or
> business-impacting, it **escalates to the OGE Agentic Ops squad**. The squad reasons about impact
> on Azure AI Foundry and returns a fix or script. **A human approves, and only then does anything change.**

## 1. How the tiers work together

```
 Azure Monitor alert / Activity Log / scheduled sweep
                │
                ▼
 ┌────────────────────────────────────────┐      SOLO (runbook-shaped)
 │ Tier 1  Azure SRE Agent                │──► skill: nsg-open-port-triage
 │ custom agent: zeroops-triage           │──► skill: webapp-bad-deploy-revert
 │ skill: zeroops-escalation-policy       │      → proposes 1 reversible fix (Review mode)
 └──────────────┬─────────────────────────┘
                │ ESCALATE  (MCP: ogeops_escalate)
                ▼
 ┌────────────────────────────────────────┐
 │ OGE app  POST /mcp  (X-API-Key)        │  escalation ledger (SQLite) → ZeroOps view
 │ Tier 2  OGE Agentic Ops squad          │
 │ Azure AI Foundry Agent Service         │  Coordinator routes → specialists in parallel
 │ grounded on the deterministic evidence │  → optional debate → evaluated final answer
 └──────────────┬─────────────────────────┘
                │ conclusion · business impact · actions · remediation script · token cost
                ▼
 ┌────────────────────────────────────────┐
 │ Human  (ZeroOps view or ADO work item) │  Approve / Reject / Resolve — audited in the ledger
 └────────────────────────────────────────┘
```

| Concern | Tier 1: Azure SRE Agent | Tier 2: OGE squad |
|---|---|---|
| Trigger | Azure Monitor alerts (response plan), scheduled tasks, chat | `ogeops_escalate` over MCP, or a human in the ZeroOps view |
| Strength | Fast triage: logs, metrics, Activity Log, `az` CLI, repos | Multi-specialist reasoning on cost, reliability, security, policy and hygiene, with business impact |
| Output | One reversible fix (Review mode) | Root cause, impact, ranked actions, remediation script, ADO proposal |
| Changes Azure? | Only after operator approval in Review mode | **Never.** It returns a proposal and a human executes it |
| Cost driver | Azure Agent Units (AAU): always-on plus active tokens | Foundry model tokens (measured per escalation) |

## 2. The MCP contract (`/mcp`)

The app exposes a stateless **Streamable HTTP** MCP server (JSON-RPC 2.0; protocol versions
`2025-06-18`, `2025-03-26` and `2024-11-05`) implemented in `app/zeroops/mcp_server.py`.

- **Auth:** `X-API-Key: <MCP_API_KEY>` (or `Authorization: Bearer <MCP_API_KEY>`), compared in constant time.
  If `MCP_API_KEY` is unset, the endpoint returns **503**, so it is never open by accident.
- The connector is named **`ogeops`**, so the SRE Agent sees the tools as `ogeops_<tool>`.

| Tool | Writes? | Purpose |
|---|---|---|
| `escalate` | ledger only | Hands an incident to the squad: `question`, `sre_summary`, `incident_ref`, `severity`, `scenario_id`, `category`, `debate`, `wait_seconds` (≤55). Returns `escalation_id` and status |
| `get_escalation` | – | Status plus result: conclusion, business impact, actions, remediation script, cost |
| `propose_fix` | proposal | Turns a finished escalation into a human-approval proposal (an ADO work item when configured) |
| `list_findings` | – | Priority-ordered findings from the deterministic evidence layer |
| `get_evidence` | – | Bounded evidence for one finding |
| `agent_catalog` | – | Who is on the squad and what each agent does |
| `list_scenarios` | – | The ZeroOps demo scenarios |
| `cost_model` | – | AAU and token cost model |

REST equivalents used by the UI are under `/api/zeroops/*`:
- `GET overview`, `GET scenarios`
- `POST scenarios/<id>/{inject,cleanup,escalate}`
- `GET|POST escalations`, `GET escalations/<id>`
- `POST escalations/<id>/propose`, `POST escalations/<id>/decision`

Escalations run the squad in a background thread and wait up to `wait_seconds` (default 45, max 55),
which keeps them under the App Service and gunicorn request timeouts. If the result isn't ready, the
SRE Agent polls `get_escalation`. Every escalation, decision and cost is recorded in the
`zeroops_escalations` table in `OPERATIONS_STATE_DB`.

## 3. Cost model: what one incident costs

Implemented in `app/zeroops/cost.py` and shown in the ZeroOps view and on every escalation.

**Azure SRE Agent** is billed in **Azure Agent Units (AAU)**
([pricing](https://learn.microsoft.com/azure/sre-agent/pricing-billing)):
- **Always-on:** 4 AAU per agent-hour, from creation until deletion. At the list price this is
  730 h × 4 × $0.10 ≈ **$292 per agent per month**.
- **Active flow:** AAU per 1M tokens, which depends on the model. For gpt-5.2 that is 35 input,
  280 output and 3.5 cached.
- **Price per AAU:** the "SRE Agent Unit" meter in the
  [Azure Retail Prices API](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Azure%20SRE%20Agent%27)
  is **$0.10** in US regions and **$0.11** in some others, e.g. Sweden Central. Set
  `SRE_AGENT_AAU_PRICE_USD` for your region or agreement.

| SRE task profile (published estimate) | AAU | ≈ USD at $0.10 |
|---|---|---|
| quick_question | 1.3 | $0.13 |
| incident_investigation | 11.7 | $1.17 |
| full_remediation | 30.1 | $3.01 |

**OGE squad** cost is **measured** rather than estimated. Each escalation records the Foundry token
`usage_summary` (input/output tokens × per-agent pricing in the profile). A typical routed analysis
uses 3 specialists plus the coordinator and costs cents; a debate round adds roughly 30–60%.

**Per-incident total** = SRE active-flow estimate + measured squad cost. The always-on baseline is
shown separately because it is a fixed monthly cost, not a per-incident one.

> Driving Foundry token usage: every escalation is real Foundry Agent Service traffic, attributed per
> agent in `usage_summary` and in App Insights (see [TELEMETRY.md](TELEMETRY.md)). The token
> ledger shows *which* incidents consumed tokens and *what decision* they bought.

## 4. Demo scenarios

Defined in `app/zeroops/scenarios.py`. **Inject** makes a real, reversible change to the resources from
`infra/zeroops-demo/` (only when `ZEROOPS_CHAOS_ENABLED=true`). **Clean up** restores the template
state. Alert rule names start with `ZeroOps <scenario-id>`, so the SRE response plan routes them.

| | Scenario | Tier | What breaks | Who fixes it | Squad agents |
|---|---|---|---|---|---|
| 🚪 | `open-door` | SRE solo | NSG rule allows SSH (22) from `*` | SRE deletes the rule (skill: nsg-open-port-triage) | – |
| 💥 | `bad-deploy` | SRE solo | Web app startup command broken → site down | SRE reverts the startup command (skill: webapp-bad-deploy-revert) | – |
| ⛈️ | `storm-surge` | Escalated | Plan "panic-scaled" B1×1 → P0v3×3 | Squad weighs cost vs. SLO vs. right-size | Cost & Capacity, Incident & Change, Resilience & Hygiene |
| 🩹 | `rogue-hotfix` | Escalated | Hotfix enabled public blob access and TLS 1.0 | Squad weighs security vs. breaking the hotfix | Security & Monitoring, Policy & Governance, Resilience & Hygiene |
| ⏰ | `2am-cert` | Escalated | TLS secret expires in 48h **and** the renewal runbook failed | Squad builds the renewal plan and script before the deadline | Incident & Change, Resilience & Hygiene, Policy & Governance |

### Suggested 15-minute demo script
1. **Design (2 min).** Open **More → ZeroOps** and walk through the flow banner, the tier split and the
   cost panel (always-on baseline vs. per-incident).
2. **SRE solo (4 min).** Inject 🚪 `open-door`. The Activity Log alert fires, and the SRE Agent's
   `zeroops-triage` agent loads `nsg-open-port-triage`, names who opened port 22, and proposes the
   delete. Approve it in the SRE Agent. Point out that no squad tokens were spent.
3. **Escalation (6 min).** Inject 🩹 `rogue-hotfix`. The SRE Agent triages, the escalation policy says
   "security vs. availability trade-off", and it calls `ogeops_escalate` with `debate=true`. The
   escalation appears in the ZeroOps timeline (analyzing → proposed) with the squad's conclusion,
   business impact, remediation script and **measured token cost**. Approve it in the ZeroOps view.
4. **No SRE Agent handy?** Use **Simulate escalation** on any squad scenario. It runs the same squad
   path with `source=simulated`.
5. **Clean up (1 min).** Run **Clean up** on each injected scenario.

## 5. Set up

### 5.1 App (OGE Agentic Ops)
1. Create the MCP key in the app's Key Vault:
   `az keyvault secret set --vault-name <kv> -n mcp-api-key --value "$(openssl rand -hex 32)"`
2. Deploy with `zeroopsSettings: { mcpApiKeySecretName: 'mcp-api-key' }` (see `infra/main.bicep`),
   or set the `MCP_API_KEY` app setting as a Key Vault reference.
3. Make sure `/mcp` is reachable by the SRE Agent:
   - If App Service Authentication (Easy Auth) is on, exclude `/mcp` (`excludedPaths`) or allow
     unauthenticated requests. The API key still protects the endpoint.
   - With `publicNetworkAccess=Disabled`, the SRE Agent needs a network path to the app.

### 5.2 Demo resources (optional)
```bash
az group create -n <demo-rg> -l <location>
az deployment group create -g <demo-rg> -f infra/zeroops-demo/main.bicep \
  -p opsAppPrincipalId=<app identity principalId> sreAgentPrincipalIds='["<sre agent principalId>"]'
```
Copy the `appSettings` output to the app's settings (or to `zeroopsSettings`), and set
`ZEROOPS_CHAOS_ENABLED=true` **only** in demo environments. Add the demo resource group to the
SRE Agent's managed resources. Remove everything with `az group delete -n <demo-rg>`.

### 5.3 Azure SRE Agent
Assets are in [`sre-agent/`](../sre-agent/):

```bash
export SRE_AGENT_RESOURCE_GROUP=<rg> SRE_AGENT_NAME=<agent> \
       OGE_APP_URL=https://<your-app> MCP_API_KEY=<same key>
./sre-agent/scripts/configure-sre-agent.sh      # DRY_RUN=1 to preview
```

It creates the following:
- the `ogeops` MCP connector (ARM `Microsoft.App/agents/connectors`, bearer-token auth; prints portal steps on failure)
- three skills
- the `zeroops-triage` custom agent
- the `zeroops-response` response plan (alert title contains "ZeroOps" → zeroops-triage, Review mode)
- the `zeroops-daily-sweep` scheduled task

All calls are idempotent.

## 6. Guardrails
- The squad is read-only and returns proposals. The SRE Agent never runs squad scripts.
- Scenario inject/cleanup only touches resources named in `ZEROOPS_DEMO_*` settings and refuses
  (409) when chaos is disabled or a resource isn't configured.
- No secrets in escalations: the evidence layer redacts, and SRE skills forbid pasting secrets.
- Every decision (approve/reject/resolve) is recorded with actor, time and reason in the ledger.
