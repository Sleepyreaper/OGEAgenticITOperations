<p align="center">
  <img src="static/power-logo.svg" alt="Ops Council" width="160">
</p>

<h1 align="center">Ops Council</h1>

<p align="center"><strong>Multi-agent operational intelligence for Azure Cloud Operations</strong></p>

---

> This is a **reusable, open-source (MIT licensed) product**: a generic multi-agent Azure
> operations app you can deploy for any customer, with your own branding, agent
> personalities/prompts, and per-agent model routing — no code changes required. This repo
> ships with the **"power"** profile shown below (a generic power & utilities reference
> deployment on a GPT-5.6 Sol/Terra/Luna model tier — see
> [docs/MODEL_CONFIGURATION.md](docs/MODEL_CONFIGURATION.md)) as the default, a neutral
> **"generic"** profile to start a new customer deployment from, and the original
> **"oge"** (Microsoft Oil, Gas & Energy) branding/personas kept as a selectable
> legacy/example profile. See [BRANDING.md](BRANDING.md) and [DEPLOYMENT.md](DEPLOYMENT.md)
> for the full configuration/deployment workflow, including the setup wizard
> (`scripts/configure.py`), and [docs/TELEMETRY.md](docs/TELEMETRY.md) for the optional
> Azure Monitor OpenTelemetry integration.

Six AI specialists — each named after cloud operations concepts — debate, disagree, and synthesize to deliver balanced, transparent recommendations. Built for teams that need operational answers without elevated access.

**Two views, one platform:**
- **Reliability** — Executive dashboard with reliability scores, pillar assessments, and service health (Rick's view)
- **Ops Center** — Operational findings, chaos testing, remediation code, and deep intelligence (Christopher/Shane's view)

## The Crew (default "power" profile)

| | Agent | Role | Model | What They Do |
|--|-------|------|-------|-------------|
| ⚡ | **Grid Coordinator** | Coordinator | GPT-5.6 Sol | Routes requests, synthesizes the crew's takes, delivers exec-ready summaries |
| 💰 | **Cost & Capacity Analyst** | Cost | GPT-5.6 Terra | Finds waste, recommends rightsizing, shows the math |
| 🔧 | **Reliability Engineer** | Standards | GPT-5.6 Terra | Knows *why* things are built the way they are. Writes Terraform remediation. |
| 🔄 | **Incident Investigator** | Diagnostics | GPT-5.6 Sol | Root cause analysis without users needing elevated access |
| 🛰️ | **Operations Monitor** | Monitoring | GPT-5.6 Luna | Proactive scanning — surfaces problems before they become incidents |
| 📋 | **Compliance Advisor** | Compliance | GPT-5.6 Terra | Classifies policy violations as definition bugs, misconfigurations, exemptions, or workaround abuse |

Every name, role, model deployment, endpoint, and system prompt above is defined in
`profiles/power/` and is entirely swappable via the profile system — see
[BRANDING.md](BRANDING.md) and [docs/MODEL_CONFIGURATION.md](docs/MODEL_CONFIGURATION.md)
for why agents map to the Sol/Terra/Luna tiers the way they do. The original OGE
branding/personas remain available: set `APP_PROFILE=oge`.

## How It Works

```
User Question  /  🔥 Monitor Alert  /  ☀️ Morning Briefing
         │
         ▼
    ⚡ Coordinator (routes to crew)
         │
   ┌─────┼──────────┬──────────┬──────────┐
   ▼     ▼          ▼          ▼          ▼
 💰      🔧         🔄        🛰️         📋
Cost    Reliab-   Diagnos-  Monitor    Compliance
        ility     tics
   │     │          │          │
   └─────┴──────────┘          │
        ▼  (Round 2: Debate)   │
   ⚡ Coordinator ◄─────────────┘
        │
        ▼
  Streamed live: each crew member
  speaks → rebuttals → synthesis
  → Generate Terraform / CLI fix
  → Executive Summary
```

**Round 1**: Each specialist gives their styled take (3-5 sentences).
**Round 2**: Each specialist sees the others' takes and argues back (2-3 sentences).
**Round 3**: The orchestrator delivers a crisp executive readout.

All responses stream via Server-Sent Events — you watch the crew debate live.

## Key Features

### Executive Reliability View
- Reliability score (0-100) with animated ring gauge
- Four pillar assessments: Security, Governance, Resilience, Cost Efficiency — each with score bars
- Azure Service Health events — clickable for "which of MY resources are affected?"
- Prioritized action cards (HIGH / MED / LOW)
- "Ask the orchestrator for Executive Summary" button

### Ops Center
- Real-time findings: orphaned disks, public IPs, insecure storage, NSG drift
- Deep intelligence: architecture smell detection, cross-resource correlation, orphaned NSGs, idle App Service Plans, empty subnets
- Azure Advisor integration — platform-verified evidence alongside crew analysis
- Resource Health status bar with visual progress indicator
- Auto-refresh every 60 seconds in Live mode (Resource Graph queries are free)
- Change detection — badge flashes "⚡ CHANGE DETECTED" when environment changes
- 💥 "Do Something Stupid" chaos demo — creates real security problem, detected in ~10 seconds
- ☀️ Morning Briefing — overnight digest from the crew

### Ops Council Chat
- Streaming multi-agent debate with custom-styled personalities
- 🔧 Generate Terraform / CLI Fix button after every analysis
- 📊 Executive Summary button for leadership-ready output
- Token cost ticker — transparency on every interaction (pennies per query)
- Dynamic suggested questions that update based on real environment scan

### Data Modes
- **Demo**: Pre-built demo scenarios (VM sizing, deployment failure, waste analysis)
- **Live**: Scans real Azure subscription via Managed Identity — everything real

## Architecture

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Frontend | Tailwind CSS (CDN), vanilla JS, SSE | Dashboard + chat UI |
| Backend | Python 3.13 / Flask / Gunicorn | API, scan, agent orchestration |
| Hosting | Azure Web App on P0v3 plan | Shared existing plan |
| AI | Azure OpenAI (6 model deployments across 2 regions) | Agent reasoning and synthesis |
| Data | Resource Graph, Resource Health, Service Health, Advisor, Monitor | Environment intelligence |
| Deep Analysis | Cross-resource ARG queries | Architecture smells, blast radius, correlation |
| Security | VNet, Key Vault (PE), Managed Identity, RBAC | Zero passwords, least-privilege |
| Telemetry | Azure Monitor OpenTelemetry (optional, see [docs/TELEMETRY.md](docs/TELEMETRY.md)) | Per-agent call spans, tokens, latency, estimated cost |

**Daily cost: ~$0.15-0.75** (scanning is free; reasoning models cost more per query but deliver better output)

## Quick Start

1. `python3 scripts/configure.py` — interactive setup wizard; generates `.env` and
   `infra/main.bicepparam` (both git-ignored) from your answers
2. Deploy your model deployments in Azure AI Foundry (see [DEPLOYMENT.md](DEPLOYMENT.md))
3. `cd infra && bash deploy.sh` — deploys the Bicep infrastructure
4. Grant Reader + Log Analytics Reader + Monitoring Reader to the Managed Identity
   (see [DEPLOYMENT.md](DEPLOYMENT.md) / `docs/rbac-implementation-guide.md`)
5. Zip deploy the app code, open the URL, and toggle to Live

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full step-by-step guide, and
[BRANDING.md](BRANDING.md) to customize branding/agents/prompts before deploying.

## RBAC Summary

**5 read-only roles + 1 scoped write on 1 Managed Identity. Zero changes to user permissions.**

| Role | Scope | Purpose |
|------|-------|---------|
| Reader | Subscription | Resource enumeration, health, tags |
| Log Analytics Reader | Subscription | Activity logs, deployment failures |
| Monitoring Reader | Subscription | Metrics, alerts, diagnostics |
| Key Vault Secrets User | Key Vault (resource) | Read secrets only |
| Cognitive Services OpenAI User | OpenAI Accounts (your Azure regions) | Call models across both regions |
| Network Contributor | NSG (resource, demo only) | Chaos demo NSG rule create/delete |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Safe configuration-readiness check (profile, agent deployment names, booleans — never endpoint URLs/subscription IDs) |
| `/api/scan/overview` | GET | Full scan (Resource Graph + Health + Advisor + Deep) |
| `/api/scan/security` | GET | Quick security drift scan |
| `/api/scan/compliance` | GET | Azure Policy compliance scan + violation classification |
| `/api/ask/stream` | POST | SSE streaming crew debate |
| `/api/demo/<id>/stream` | POST | SSE streaming demo scenario |
| `/api/remediate` | POST | Generate Terraform/CLI fix |
| `/api/digest` | GET | Morning briefing (SSE) |
| `/api/chaos/create` | POST | Create chaos NSG rule |
| `/api/chaos/cleanup` | POST | Remove chaos NSG rule |

## Configuration & Customization

Branding, agent names/personalities, prompts, model deployments, and per-agent Azure OpenAI
endpoint routing are all controlled by the **profile system** (`profiles/<id>/`) plus
environment variables — no code changes needed. See [BRANDING.md](BRANDING.md) for the full
guide. Quick reference:

| Want to... | Do this |
|---|---|
| Rebrand for a new customer | `python3 scripts/configure.py` → create a new profile (clones `profiles/generic/`) |
| Change an agent's name/role/model | Edit `profiles/<id>/profile.json` |
| Change an agent's system prompt | Edit `profiles/<id>/prompts/<agent_key>.txt` |
| Cap tokens/cost, set tone, or estimate spend per agent | `max_completion_tokens` / `max_context_chars` / `response_instruction` / pricing fields — see [docs/MODEL_CONFIGURATION.md](docs/MODEL_CONFIGURATION.md) |
| Route one agent to a different endpoint/deployment | Set `AGENT_<KEY>_ENDPOINT` / `AGENT_<KEY>_DEPLOYMENT` (env var), or `agentOverrides` (Bicep) |
| Switch which profile is active | Set `APP_PROFILE` (env var) / `appProfile` (Bicep param) |
| Enable/inspect telemetry | Set `APPLICATIONINSIGHTS_CONNECTION_STRING` — see [docs/TELEMETRY.md](docs/TELEMETRY.md) |

## Project Structure

```
├── app/
│   ├── agents/
│   │   ├── demos.py          # 6 demo scenarios with realistic data
│   │   └── runner.py         # Debate system, SSE streaming, remediation
│   ├── azure_data.py         # Resource Graph, Health APIs, Advisor, deep analysis
│   ├── config.py             # Settings/AgentConfig, profile + env var resolution
│   ├── profiles.py           # Profile loading/validation (stdlib only)
│   ├── telemetry.py          # Azure Monitor OpenTelemetry init + agent call spans
│   └── main.py               # Flask app, all API endpoints
├── profiles/                 # Branding + per-agent config + prompts, one dir per profile
│   ├── power/                 # Default profile — generic power-utility, GPT-5.6 Sol/Terra/Luna
│   ├── generic/                # Neutral starting point for a new customer profile
│   └── oge/                    # Legacy/example profile — this app's original branding/agents
├── scripts/configure.py      # Setup wizard — generates .env + infra/main.bicepparam
├── infra/                    # Bicep IaC (VNet, KV, identity, web app)
│   ├── main.bicepparam.example  # Checked-in template (copy or use the wizard)
│   └── main.bicepparam       # Your local deployment values (git-ignored)
├── templates/index.html      # Executive + Ops views
├── docs/                     # RBAC guide, demo script, architecture, decisions,
│                             # MODEL_CONFIGURATION.md, TELEMETRY.md
├── tests/                    # Config/profile/ADO integration tests (stdlib unittest)
├── .env.example               # Checked-in template for local .env
├── requirements.txt
├── wsgi.py
└── startup.sh
```

## License

[MIT](LICENSE) — free to use, fork, rebrand, and redistribute. See [BRANDING.md](BRANDING.md)
for how to build your own branded deployment on top of this project.
