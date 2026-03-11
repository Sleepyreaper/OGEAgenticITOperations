# OGE Envisioning — Architecture Notes

## Approach Options

### Option A: Custom "Ops Copilot" (No Copilot Agents Preview Needed)
Build a custom agent that uses Azure Resource Graph, Monitor, and Log Analytics APIs
behind a managed identity with scoped read access. Teams interact via a chat UI (the
Tailwind site) or Teams bot. No tenant-level preview toggle required.

**Components:**
| Resource | Purpose | SKU / Tier | Notes |
|----------|---------|-----------|-------|
| Azure Static Web Apps | Host chat/dashboard UI | Free / Standard | Tailwind frontend |
| Azure OpenAI Service | LLM for analysis/recommendations | S0 (GPT-4o) | Prompt-grounded to OGE policies |
| Azure Functions | Backend API / orchestration | Consumption | Handles auth, queries, agent logic |
| Azure Resource Graph | Cross-subscription resource queries | N/A (built-in) | Read-only by design |
| Azure Monitor / Log Analytics | Telemetry, logs, alerts | Pay-as-you-go | Scoped workspace access |
| Azure Key Vault | Secret management | Standard | MIs + RBAC |
| Managed Identity | Service auth — no creds to manage | N/A | Reader role on target scopes |
| Azure Event Grid / Logic Apps | Alert routing → support owner | Consumption | Tag-driven notification routing |

### Option B: Azure Copilot Agents (Preview Required)
Enable Deployment, Observability, and Troubleshooting agents at tenant scope.
More turnkey, but blocked on Mandy's team and preview timelines.

### Option C: Azure SRE Agent
Scheduled/continuous monitoring with alerting, paired with support-owner tag routing.
Narrower scope — primarily covers the Observability scenario.

## Recommended Path

**Start with Option A** — it's fully within Cloud Ops' control, demonstrates all
three scenarios (troubleshoot, observe, deploy-assist), and doesn't depend on
preview enablement or other teams.

If/when Copilot Agents preview becomes available, the custom agent can be compared
or retired — but the PoC doesn't stall waiting.

## High-Level Architecture (Option A)

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Tailwind UI │────▶│  Azure Functions  │────▶│  Azure OpenAI (GPT) │
│  (SWA)       │◀────│  (API + Agent)    │◀────│  grounded on OGE    │
└─────────────┘     └────────┬─────────┘     │  standards/policies │
                             │               └─────────────────────┘
                    ┌────────┴─────────┐
                    │                  │
              ┌─────▼──────┐   ┌──────▼───────┐
              │ Resource    │   │ Log Analytics │
              │ Graph       │   │ / Monitor     │
              │ (read-only) │   │ (read-only)   │
              └─────────────┘   └──────┬────────┘
                                       │
                                ┌──────▼────────┐
                                │ Event Grid /  │
                                │ Logic Apps    │──▶ Teams / Email
                                │ (tag-routed)  │   (support owner)
                                └───────────────┘
```
