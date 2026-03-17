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
│  │ Resource │ │Health  │ │Health  │ │ (6 deployments,    │  │
│  │ Graph    │ │API     │ │API     │ │  2 regions)        │  │
│  │ (free)   │ │(free)  │ │(free)  │ │ gpt-5.4, o3, nano  │  │
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

| Agent | Model | Endpoint | Token Cost/Call | Why This Model |
|-------|-------|----------|----------------|----------------|
| Pipeline (coord) | gpt-5.4 | eastus2 | ~$0.008 | Broad knowledge, strong synthesis |
| Barrel Counter (cost) | o3 | eastus2 | ~$0.015 | Deep reasoning over cost data |
| The Roughneck (standards) | gpt-5.4 | eastus2 | ~$0.008 | Explains rationale, defends decisions |
| Turnaround (diagnostics) | o3 | eastus2 | ~$0.015 | Complex multi-step root cause analysis |
| Flare Stack (monitor) | gpt-5-nano | westus3 | ~$0.0005 | Lightweight, fast scanning |
| The Inspector (compliance) | o3 | eastus2 | ~$0.015 | Deep reasoning for policy classification |

**Multi-endpoint routing**: Agents route to different Azure OpenAI accounts based on model availability. gpt-5.4 and o3 are on `springfield-ai-eastus2` (eastus2). gpt-5-nano stays on `nextgenagentfoundry` (westus3). The Managed Identity has Cognitive Services OpenAI User on both accounts.

## Data Flow: Full Debate (3 rounds)

1. User asks question (or Morning Briefing triggers)
2. Pipeline determines which crew members to consult
3. **Round 1**: Each specialist analyzes independently (parallel potential)
4. **Round 2**: Each specialist sees others' responses, argues/agrees
5. **Round 3**: Pipeline synthesizes — "Where they agreed, where they clashed, recommendation"
6. User can click "Generate Terraform/CLI Fix" for remediation code

Total per query: ~$0.06-0.12 in tokens. Scanning is free. Compliance scans ~$0.03-0.05.

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
| Morning Briefing | ~$0.12 | 4 agent calls |
| Ad-hoc crew queries | ~$0.06-0.12 each | On-demand only |
| **Total** | **~$0.15-0.75/day** | |
