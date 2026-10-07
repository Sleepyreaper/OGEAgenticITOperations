# ZeroOps: Azure SRE Agent + OGE Agentic Ops squad

> **Vision.** Nobody investigates from scratch. The **Azure SRE Agent** can watch configured
> resources and request bounded work. When a report is ambiguous, cross-domain or
> business-impacting, the OGE app can hand it to the **OGE Agentic Ops squad**. The squad returns
> analysis and a reviewable proposal. Approval records a decision; this app does not claim that the
> change was executed or verified.

## 1. How the tiers work together

```
 Azure Monitor alert / Activity Log / scheduled sweep
                │
                ▼
 ┌────────────────────────────────────────┐      SOLO (runbook-shaped)
 │ Tier 1  Azure SRE Agent                │──► playbook template: nsg-open-port-triage
 │ custom agent: zeroops-triage           │──► playbook template: webapp-bad-deploy-revert
 │ playbook: zeroops-escalation-policy    │      → provider work is not inferred here
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
 │ Human  (ZeroOps view or ADO work item) │  Approve / Reject / Close — decision only
 └────────────────────────────────────────┘
```

| Concern | Tier 1: Azure SRE Agent | Tier 2: OGE squad |
|---|---|---|
| Trigger | Azure Monitor alerts (response plan), scheduled tasks, chat | `ogeops_escalate` over MCP, or a human in the ZeroOps view |
| Strength | Fast triage: logs, metrics, Activity Log, `az` CLI, repos | Multi-specialist reasoning on cost, reliability, security, policy and hygiene, with business impact |
| Output | A thread request and, when available, provider-returned thread ID/status | Root cause, impact, ranked actions, remediation script, ADO proposal |
| Changes Azure? | Not established by this app's thread-creation response | **Never.** Approval is not execution or verification |
| Cost driver | Azure Agent Units (AAU): hypothetical task-profile estimate here | Foundry model tokens measured for completed squad calls |

## 2. The MCP contract (`/mcp`)

The app exposes a stateless **Streamable HTTP** MCP server (JSON-RPC 2.0; protocol versions
`2025-06-18`, `2025-03-26` and `2024-11-05`) implemented in `app/zeroops/mcp_server.py`.

- **Auth:** `X-API-Key: <MCP_API_KEY>` (or `Authorization: Bearer <MCP_API_KEY>`), compared in constant time.
  If `MCP_API_KEY` is unset, the endpoint returns **503**, so it is never open by accident.
- A valid API key authenticates the configured key holder; it does not prove the caller is an Azure
  SRE Agent or make reporter-supplied triage provider history.
- The connector is named **`ogeops`**, so the SRE Agent sees the tools as `ogeops_<tool>`.

| Tool | Writes? | Purpose |
|---|---|---|
| `escalate` | ledger only | Hands an incident to the squad: `question`, `sre_summary`, `incident_ref`, `severity`, `scenario_id`, `category`, `debate`, `wait_seconds` (≤55). Returns `escalation_id` and status. `severity`/`category` are hints: if they match no evidence, the squad relaxes them (drop severity, then category) and records `result.relaxed_filters` |
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
caller polls `get_escalation`. One Activity investigation links the receipt and every filter-ladder
analysis run. MCP and browser fields are `reported`; detector and simulated origins remain distinct.
The compatibility ledger remains in `zeroops_escalations` in `OPERATIONS_STATE_DB`.

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

The UI preserves these as separate bases: the SRE figure is a **hypothetical profile estimate** and
the squad figure is measured usage. Detector and simulated escalations do not establish SRE Agent
activity or incurred SRE cost. The compatibility total may combine the estimate and measurement for
comparison, but it is not an invoice or an attribution claim. The always-on baseline is shown
separately because it is a fixed monthly estimate, not a per-incident observation.

Completed squad calls expose measured `usage_summary`; insufficient-evidence, failed, detector-only,
and SRE-thread-only receipts must not be described as Foundry or SRE provider activity.

## 4. Demo scenarios

Defined in `app/zeroops/scenarios.py`. **Inject** makes a real, reversible change to the resources from
`infra/zeroops-demo/` (only when `ZEROOPS_CHAOS_ENABLED=true`). **Clean up** restores the template
state. Alert rule names start with `ZeroOps <scenario-id>`, so the SRE response plan routes them.

| | Scenario | Tier | What breaks | Who fixes it | Squad agents |
|---|---|---|---|---|---|
| 🚪 | `open-door` | SRE solo | NSG rule allows SSH (22) from `*` | Thread request uses the `nsg-open-port-triage` playbook template | – |
| 💥 | `bad-deploy` | SRE solo | Web app startup command broken → site down | Thread request uses the `webapp-bad-deploy-revert` playbook template | – |
| ⛈️ | `storm-surge` | Escalated | Plan "panic-scaled" B1×1 → P0v3×3 | Squad weighs cost vs. SLO vs. right-size | Cost & Capacity, Incident & Change, Resilience & Hygiene |
| 🩹 | `rogue-hotfix` | Escalated | Hotfix enabled public blob access and TLS 1.0 | Squad weighs security vs. breaking the hotfix | Security & Monitoring, Policy & Governance, Resilience & Hygiene |
| ⏰ | `2am-cert` | Escalated | TLS secret expires in 48h **and** the renewal runbook failed | Squad builds the renewal plan and script before the deadline | Incident & Change, Resilience & Hygiene, Policy & Governance |

> **Key Vault behind policy.** `2am-cert` writes and reads the demo secret through ARM (control plane),
> so it works when Azure Policy disables the vault's public network access. Cleanup "renews" the
> bundle (new version, expiry +1 year) because ARM cannot delete secrets.

> **Azure Policy may already help.** Many tenants assign a *modify* policy that forces
> `allowBlobPublicAccess=false`. In that case `rogue-hotfix` lands only half-way: public access is
> reverted by policy in the same request, while TLS 1.0 sticks. The SRE Agent will find the
> `policies/modify/action` entry in the Activity Log and escalate only the TLS question. This is a
> good talking point: policy, SRE Agent and squad each cover a different layer.

### Fast detection: seconds, not minutes
Activity Log alerts take about 5 minutes to reach the SRE Agent, which is too slow for a live demo.
After **Inject**, the console starts a timer and polls a read-only **probe**
(`GET /api/zeroops/scenarios/<id>/probe`) every 2 seconds. Each probe looks for the *symptom* with an
independent signal; it does not read back what inject wrote.

| Scenario | Probe | Typical time |
|---|---|---|
| `open-door` | Azure Resource Graph: inbound allow on 22/3389/* from `*`/Internet in the demo NSG | 5–20 s |
| `bad-deploy` | HTTP synthetic probe of the web app (5xx or timeout) | 30–90 s (restart) |
| `storm-surge` | Azure Resource Graph: plan SKU ≠ B1 or capacity > 1 | 5–20 s |
| `rogue-hotfix` | Azure Resource Graph: anonymous blob access or min TLS 1.0/1.1 | 5–20 s |
| `2am-cert` | Key Vault secret expiry ≤ 7 days, read via ARM (recent failed renewal jobs add context) | 5–15 s |

On a hit, the card shows **⚡ Detected in N s via &lt;source&gt;** and hands off by tier
(`POST /api/zeroops/scenarios/<id>/handoff`):
- **SRE solo**: requests an Azure SRE Agent thread (`POST {agentEndpoint}/api/v1/threads`) addressed
  to `zeroops-triage`, carrying the browser-reported detection. The Activity receipt records only
  this app's request result and the returned thread ID/status. A created thread is not evidence of
  investigation, remediation, execution or verification. Requires `SRE_AGENT_ENDPOINT` and the
  **SRE Agent Standard User** role for the app identity on the agent. Without them the hand-off
  reports `not_configured`, and the Activity Log alert remains the path.
- **Escalated**: bypasses the SRE thread request and goes straight to the squad (`origin=detector`).
  Browser-supplied detection time and signal remain reported claims unless current server code
  independently observes them.

Each escalated scenario pins its `expected_agents` specialist set so the demo remains deterministic;
the standard evidence filters, debate and coordinator synthesis still apply. The SRE playbook entries
shown here are templates/configuration, not proof that a provider selected or ran them.

The Activity Log alert still fires about 5 minutes later. It is the production backstop, and in the
demo it shows the SRE Agent's own detection path. **⚡ Detect now** runs a single probe without injecting
or handing off, which is useful after changes made in the portal or CLI.

### Suggested 15-minute demo script
1. **Design (2 min).** Open **More → ZeroOps** and walk through the flow banner, the tier split and the
   cost panel (always-on baseline vs. per-incident).
2. **SRE solo (4 min).** Inject 🚪 `open-door`. Within seconds the card shows **⚡ Detected in N s**
   and requests an SRE Agent thread. The app can show the returned thread ID/status; inspect the
   provider separately for any subsequent work. Point out that no squad tokens were spent.
3. **Escalation (6 min).** Inject 🩹 `rogue-hotfix`. The probe flags TLS 1.0 within seconds and escalates
   to the squad (`⚡ Fast detector` in the timeline: analyzing → proposed) with the conclusion, business
   impact, remediation script and **measured squad token cost**. Approve it in the ZeroOps view as a
   decision record; execution and verification occur outside this workflow. About
   5 minutes later the Activity Log alert reaches the SRE Agent, which can escalate the same issue
   itself over MCP (`ogeops_escalate`).
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
Copy the `appSettings` output to the app's settings (or to `zeroopsSettings`). For the fast-detection
hand-off, also set `SRE_AGENT_ENDPOINT` (the agent's `properties.agentEndpoint`) and grant the app
identity **SRE Agent Standard User** on the agent:
```bash
az role assignment create --assignee-object-id <app identity principalId> --assignee-principal-type ServicePrincipal \
  --role "SRE Agent Standard User" --scope <sre agent resource id>
```
Then set
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
- Probes are read-only and work with chaos disabled. A probe failure is returned as an error, never as
  "not detected".
- No secrets in escalations: the evidence layer redacts, and SRE skills forbid pasting secrets.
- Every decision (approve/reject/resolve) is recorded in the ledger. Activity maps approve to
  `approved`, reject to `rejected`, and legacy resolve to `closed_unverified`; it emits no executed
  or verified event.
