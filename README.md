# OGE Ops Council

**Multi-agent operational intelligence for Azure Cloud Operations**

Five AI specialists — each with deep expertise in a different operations area — debate, disagree, and synthesize to deliver balanced, transparent recommendations. Built for teams that need operational answers without elevated access.

## The Crew

| | Agent | Role | Model | What They Do |
|--|-------|------|-------|-------------|
| ⚡ | **Pipeline** | Coordinator | gpt-4.1-mini | Routes requests, synthesizes the crew's takes, surfaces disagreements |
| 🛢️ | **Barrel Counter** | Cost | o4-mini | Finds waste, recommends rightsizing, shows the math — every dollar is a barrel |
| 🔧 | **The Roughneck** | Standards | gpt-4.1 | Knows *why* things are built the way they are. Defends engineering decisions. |
| 🔄 | **Turnaround** | Diagnostics | o4-mini | Root cause analysis without users needing elevated access |
| 🔥 | **Flare Stack** | Monitoring | gpt-5-nano | Proactive scanning — surfaces problems before they become incidents |

## How It Works

```
User Question or 🔥 Flare Stack Alert
         │
         ▼
    ⚡ Pipeline (routes to relevant crew)
         │
    ┌────┼────────┬──────────┐
    ▼    ▼        ▼          ▼
  🛢️     🔧       🔄         🔥
Barrel  Rough-  Turn-     Flare
Counter neck    around    Stack
    │    │        │          │
    └────┴────────┘          │
         ▼  (Round 2: Debate)│
    ⚡ Pipeline ◄────────────┘
         │
         ▼
   Transparent response:
   each crew member's perspective +
   where they agreed / clashed +
   unified recommendation
```

**Round 1**: Each specialist gives their independent analysis (3-5 sentences).
**Round 2**: Each specialist sees the others' takes and argues back (2-3 sentences).
**Round 3**: Pipeline synthesizes the full debate into a balanced recommendation.

All responses stream into the chat in real-time — you watch the crew discuss as it happens.

## Key Features

- **Live / Demo toggle** — Switch between real Azure subscription data and pre-built demo scenarios
- **Streaming debate** — Agent messages appear in real-time via SSE as each specialist finishes
- **Token cost ticker** — See the exact cost of each agent call (pennies per interaction)
- **Dynamic questions** — Suggested questions update based on your actual environment when in Live mode
- **Tag-based routing** — Flare Stack identifies support owners from resource group tags
- **Zero write permissions** — The agent reads your environment but never changes anything

## Architecture

- **Frontend**: Tailwind CSS, vanilla JS, Server-Sent Events for streaming
- **Backend**: Python/Flask on Azure Web App (P0v3 App Service Plan)
- **AI**: Azure OpenAI (o4-mini for reasoning, gpt-4.1 for standards, gpt-4.1-mini for routing, gpt-5-nano for monitoring)
- **Data**: Azure Resource Graph, Log Analytics, Azure Monitor — all via Managed Identity
- **Security**: VNet-integrated, Key Vault for secrets (private endpoint), RBAC with least-privilege

## Prerequisites

- Azure subscription with:
  - Azure OpenAI account with deployed models (o4-mini, gpt-4.1, gpt-4.1-mini, gpt-5-nano)
  - App Service Plan (P0v3 or higher recommended)
- Azure CLI installed and authenticated
- Python 3.12+

## Deployment

### 1. Configure Parameters

Edit `infra/main.bicepparam` with your environment values:
- Existing App Service Plan resource ID
- Existing Azure OpenAI account name and resource group
- OpenAI endpoint URL
- Model deployment name

### 2. Deploy Infrastructure

```bash
cd infra
bash deploy.sh
```

This creates:
- Resource group with VNet, subnets, NSGs
- Key Vault (private endpoint, RBAC-enabled)
- Log Analytics workspace + Application Insights
- User-Assigned Managed Identity
- Web App (VNet-integrated, Python 3.13)

### 3. Grant RBAC (per monitored subscription)

```bash
MI_PRINCIPAL_ID=$(az identity show --name ogeops-id \
  --resource-group OGE_Envisioning --query principalId -o tsv)

az role assignment create --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Reader" --scope "/subscriptions/<SUB_ID>"

az role assignment create --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Log Analytics Reader" --scope "/subscriptions/<SUB_ID>"

az role assignment create --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" --scope "/subscriptions/<SUB_ID>"
```

See [docs/rbac-implementation-guide.md](docs/rbac-implementation-guide.md) for the full least-privilege RBAC guide.

### 4. Deploy Application

```bash
zip -r /tmp/deploy.zip app/ templates/ static/ requirements.txt wsgi.py startup.sh -x "*__pycache__*"

TOKEN=$(az account get-access-token --resource "https://management.azure.com/" --query accessToken -o tsv)

curl -X POST "https://<YOUR_APP_NAME>.scm.azurewebsites.net/api/zipdeploy" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/zip" \
  --data-binary @/tmp/deploy.zip
```

## Project Structure

```
├── app/
│   ├── agents/
│   │   ├── demos.py          # Pre-built demo scenarios with realistic data
│   │   └── runner.py         # Agent orchestration, debate system, SSE streaming
│   ├── azure_data.py         # Azure Resource Graph, Monitor, Log Analytics queries
│   ├── config.py             # Agent configs, system prompts, personalities
│   └── main.py               # Flask app, API routes, SSE endpoints
├── infra/
│   ├── modules/              # Bicep modules (network, KV, identity, web app, RBAC)
│   ├── main.bicep            # Main infrastructure template
│   ├── main.bicepparam       # Parameters (edit for your environment)
│   └── deploy.sh             # Deployment script
├── templates/
│   └── index.html            # OGE-branded dashboard with Tailwind CSS
├── static/
│   └── oge-logo.svg          # OGE logo
├── docs/
│   ├── rbac-implementation-guide.md  # Least-privilege RBAC guide
│   ├── demo-script-3-19.md          # Demo walkthrough script
│   ├── architecture.md              # Architecture notes
│   ├── requirements.md              # Customer requirements
│   └── decisions.md                 # Decision log
├── requirements.txt          # Python dependencies
├── wsgi.py                   # WSGI entry point
└── startup.sh                # Gunicorn startup command
```

## RBAC Summary

**5 roles on 1 Managed Identity. Zero write permissions.**

| Role | Scope | Purpose |
|------|-------|---------|
| Reader | Subscription | Resource enumeration, health, tags |
| Log Analytics Reader | Subscription | Activity logs, deployment failures |
| Monitoring Reader | Subscription | Metrics, alerts, diagnostics |
| Key Vault Secrets User | Key Vault (resource) | Read secrets only |
| Cognitive Services OpenAI User | OpenAI Account (resource) | Call models only |

## Demo Scenarios

1. **"Why is this VM so big?"** — Barrel Counter vs The Roughneck debate VM sizing
2. **"My deployment failed"** — Turnaround diagnoses without elevated access
3. **"Where are we wasting money?"** — Full waste analysis with opposing perspectives
4. **"Light the Flare Stack"** — Proactive environment scan
5. **"Full environment health check"** — All crew members analyze everything

## License

Internal use. Not for redistribution.
