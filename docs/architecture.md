# OGE Ops Council — Architecture

## Solution Overview

A multi-agent AI operations platform running on Azure, purpose-built for OGE Cloud Ops.

**Key architectural decisions:**
- Custom-built (not dependent on Azure Copilot Agents preview or SRE Agent)
- Reuses existing Azure OpenAI deployments and App Service Plan
- Secure by default — VNet, private endpoints, Managed Identity, RBAC
- Two-view design: Executive Reliability (leadership) and Ops Center (engineering)

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                      OGE Ops Council                         │
│                                                              │
│  ┌─────────────────┐    ┌──────────────────────────────────┐│
│  │  Reliability     │    │  Ops Center                      ││
│  │  (Executive)     │    │  (Engineering)                   ││
│  │  - Score 0-100   │    │  - Findings & drift              ││
│  │  - 4 pillars     │    │  - Chaos demo                    ││
│  │  - Service Health│    │  - Remediation (Terraform/CLI)   ││
│  │  - Top Actions   │    │  - Morning Briefing              ││
│  └────────┬─────────┘    └──────────┬───────────────────────┘│
│           └──────────┬──────────────┘                        │
│                      ▼                                       │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Flask Backend (Python 3.13 / Gunicorn)                │  │
│  │  - SSE streaming debate system                         │  │
│  │  - Scan / Chaos / Digest / Remediate APIs              │  │
│  │  - Auto-refresh every 60s (free Resource Graph)        │  │
│  └──────┬──────────┬──────────┬──────────┬────────────────┘  │
│         │          │          │          │                    │
│         ▼          ▼          ▼          ▼                    │
│  ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────────────────┐  │
│  │ Azure    │ │Resource│ │Service │ │ Azure OpenAI       │  │
│  │ Resource │ │Health  │ │Health  │ │ (5 deployments)    │  │
│  │ Graph    │ │API     │ │API     │ │ o4-mini, gpt-4.1,  │  │
│  │ (free)   │ │(free)  │ │(free)  │ │ 4.1-mini, 5-nano   │  │
│  └──────────┘ └────────┘ └────────┘ └────────────────────┘  │
│         │          │          │          │                    │
│         ▼          ▼          ▼          ▼                    │
│  ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────────────────┐  │
│  │ Azure    │ │Log     │ │Azure   │ │ Key Vault          │  │
│  │ Advisor  │ │Analyt. │ │Monitor │ │ (Private Endpoint) │  │
│  │ (free)   │ │        │ │        │ │                    │  │
│  └──────────┘ └────────┘ └────────┘ └────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         All access via Managed Identity (Reader RBAC)
```

## Azure Resources (OGE_Envisioning Resource Group)

| Resource | Type | Purpose |
|----------|------|---------|
| ogeops-app | Web App (P0v3, shared plan) | Dashboard + API backend |
| ogeops-id | Managed Identity | Auth to all Azure APIs + OpenAI |
| ogeops-kv | Key Vault (private endpoint) | Secrets management |
| ogeops-vnet | Virtual Network | Network isolation |
| ogeops-log | Log Analytics | Telemetry collection |
| ogeops-appi | Application Insights | App monitoring |
| ogeops-nsg-* | NSGs | Network security + chaos demo target |

## Agent Architecture

| Agent | Model | Token Cost/Call | Why This Model |
|-------|-------|----------------|----------------|
| Pipeline (coord) | gpt-4.1-mini | ~$0.001 | Fast routing, low latency |
| Barrel Counter (cost) | o4-mini | ~$0.005 | Deep reasoning over numbers |
| The Roughneck (standards) | gpt-4.1 | ~$0.008 | Broad knowledge, explains rationale |
| Turnaround (diagnostics) | o4-mini | ~$0.005 | Complex multi-step log analysis |
| Flare Stack (monitor) | gpt-5-nano | ~$0.0005 | Lightweight, fast scanning |

## Data Flow: Full Debate (3 rounds)

1. User asks question (or Morning Briefing triggers)
2. Pipeline determines which crew members to consult
3. **Round 1**: Each specialist analyzes independently (parallel potential)
4. **Round 2**: Each specialist sees others' responses, argues/agrees
5. **Round 3**: Pipeline synthesizes — "Where they agreed, where they clashed, recommendation"
6. User can click "Generate Terraform/CLI Fix" for remediation code

Total per query: ~$0.03-0.08 in tokens. Scanning is free.

## Deep Intelligence Queries

Beyond basic Advisor recommendations, the system runs cross-resource correlation:

- Architecture smell detection (NSG:VNet ratio, disk:VM ratio, NIC sprawl)
- Orphaned NSG detection (NSGs with no subnets or NICs)
- Idle App Service Plans (paying for zero apps)
- Empty subnet analysis (allocated but unused address space)
- Insecure storage detection (public blob access)
- Security drift (dangerous inbound NSG rules to any source)
- Resource Health per-resource status
- Azure Service Health platform incidents

## Cost Model

| Component | Daily Cost | Notes |
|-----------|-----------|-------|
| Resource Graph queries | $0 | Free, unlimited |
| Health/Advisor APIs | $0 | Free with Reader |
| Auto-refresh (1,440/day) | $0 | All free APIs |
| Web App hosting | $0 | Shared existing plan |
| Morning Briefing | ~$0.08 | 5 agent calls |
| Ad-hoc crew queries | ~$0.03-0.08 each | On-demand only |
| **Total** | **~$0.10-0.50/day** | |
