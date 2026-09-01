# Cloud Weather Ops — Architecture

## Solution Overview

A multi-agent AI operations platform running on Azure, purpose-built for Cloud Ops teams.

**Key architectural decisions:**
- Custom-built (not dependent on Azure Copilot Agents preview or SRE Agent)
- Reuses existing Azure OpenAI deployments and App Service Plan
- Secure by default — VNet, private endpoints, Managed Identity, RBAC
- Two-view design: Executive Reliability (leadership) and Ops Center (engineering)

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                      Cloud Weather Ops                         │
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
│  │ (free)   │ │(free)  │ │(free)  │ │ foundry-gpt, reasoning, nano  │  │
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

## Azure Resources ({PREFIX}_RG Resource Group)

| Resource | Type | Purpose |
|----------|------|---------|
| {prefix}-app | Web App (P0v3, shared plan) | Dashboard + API backend |
| {prefix}-id | Managed Identity | Auth to all Azure APIs + OpenAI |
| {prefix}-kv | Key Vault (private endpoint) | Secrets management |
| {prefix}-vnet | Virtual Network | Network isolation |
| {prefix}-log | Log Analytics | Telemetry collection |
| {prefix}-appi | Application Insights | App monitoring |
| {prefix}-nsg-* | NSGs | Network security + chaos demo target |

## Agent Architecture

| Agent | Foundry Deployment | Role | Token Cost/Call | Why This Model |
|-------|-------------------|------|----------------|----------------|
| Grid Dispatch (coord) | foundry-gpt | General-purpose LLM | ~$0.008 | Broad knowledge, strong synthesis |
| Meter Reader (cost) | foundry-reasoning | Reasoning model | ~$0.015 | Deep reasoning over cost data |
| The Lineman (standards) | foundry-gpt | General-purpose LLM | ~$0.008 | Explains rationale, defends decisions |
| Blackout (diagnostics) | foundry-reasoning | Reasoning model | ~$0.015 | Complex multi-step root cause analysis |
| Arc Flash (monitor) | foundry-nano | Lightweight model | ~$0.0005 | Lightweight, fast scanning |
| The Regulator (compliance) | foundry-reasoning | Reasoning model | ~$0.015 | Deep reasoning for policy classification |

**Multi-endpoint routing**: Agents can route to different Azure OpenAI accounts based on model availability. Configure `AZURE_OPENAI_ENDPOINT` for your primary account and `AZURE_OPENAI_ENDPOINT_SECONDARY` for a secondary account if models are spread across regions. The Managed Identity needs Cognitive Services OpenAI User on all accounts.

## Data Flow: Full Debate (3 rounds)

1. User asks question (or Morning Briefing triggers)
2. Grid Dispatch determines which crew members to consult
3. **Round 1**: Each specialist analyzes independently (parallel potential)
4. **Round 2**: Each specialist sees others' responses, argues/agrees
5. **Round 3**: Grid Dispatch synthesizes — "Where they agreed, where they clashed, recommendation"
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

---

## Phase 2: Azure DevOps Integration

Phase 2 adds a closed-loop workflow: The Regulator classifies policy violations → proposes ADO actions → human reviews and approves → ADO work items or PRs are created automatically.

### Phase 2 Data Flow

```
┌──────────────────────────────────────────────────────────────┐
│                    Phase 2 — ADO Workflow                     │
│                                                              │
│  1. Policy Scan         2. Inspector           3. Proposal   │
│  ┌─────────────┐       ┌──────────────┐       ┌───────────┐ │
│  │ Azure Policy │──────▶│ The Regulator│──────▶│ Proposal  │ │
│  │ Insights API │       │ classifies   │       │ (pending) │ │
│  └─────────────┘       └──────────────┘       └─────┬─────┘ │
│                                                     │       │
│                         4. Human Review             │       │
│                         ┌──────────────┐            │       │
│                         │  Cloud Weather Ops │◀───────────┘       │
│                         │  Dashboard   │                     │
│                         │  ✅ Approve  │                     │
│                         │  ❌ Reject   │                     │
│                         └──────┬───────┘                     │
│                                │                             │
│                   ┌────────────┴────────────┐                │
│                   ▼                         ▼                │
│  5a. Policy Bug                5b. Workaround/Misconfig      │
│  ┌──────────────────┐         ┌──────────────────┐          │
│  │ ADO: Create PR   │         │ ADO: Create PBI  │          │
│  │ Branch + fix     │         │ or Bug work item │          │
│  │ → Code review    │         │ → Sprint backlog │          │
│  └──────────────────┘         └──────────────────┘          │
└──────────────────────────────────────────────────────────────┘
        Human approval required at EVERY step
```

### Violation → ADO Action Mapping

| Inspector Classification | ADO Action | Work Item Type | Priority |
|-------------------------|------------|---------------|----------|
| **Policy Bug** | Create PR to fix policy definition | Pull Request | Depends on blast radius |
| **Misconfiguration** | Create Bug for resource owner | Bug | Based on risk (1-4) |
| **Workaround Abuse** | Create PBI to redesign control | Product Backlog Item | High (expired exemption = P1) |
| **Intentional Exemption** | Create Task to verify documentation | Task | Low (P4) |

### Phase 2 API Endpoints

| Endpoint | Method | Purpose |
|---------|--------|---------|
| `/api/ado/inspect-and-propose` | POST | Full pipeline: scan → classify → propose |
| `/api/ado/proposals` | GET | List proposals (filter by `?status=`) |
| `/api/ado/proposals` | POST | Create proposals from classifications |
| `/api/ado/proposals/{id}` | GET | Get single proposal detail |
| `/api/ado/proposals/{id}/approve` | POST | Human approves → generates ADO payload |
| `/api/ado/proposals/{id}/reject` | POST | Human rejects with reason |

### ADO Grid Dispatch (CI/CD)

The project includes an Azure Grid Dispatchs YAML definition (`pipelines/azure-pipelines.yml`) with three stages:

1. **Build & Test** — Install deps, compile check, run tests (auto on every push/PR)
2. **Deploy Staging** — Auto-deploy to staging App Service on main merge
3. **Deploy Production** — Requires **human approval** via ADO Environment check

### Phase 2 Components

| File | Purpose |
|------|---------|
| `app/ado_integration.py` | ADO proposal lifecycle — create, approve, reject, payload generation |
| `pipelines/azure-pipelines.yml` | ADO pipeline — build, test, stage, prod with human gate |
| `infra/modules/subscription-rbac.bicep` | Reusable module — grants Reader roles on any subscription |
| `tests/test_ado_integration.py` | 44 tests covering full proposal lifecycle |
