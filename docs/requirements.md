# OGE Ops Council — Requirements

## Org Context

- **Scope**: Cloud Ops/Engineering — Digital Foundations organization
- **Goal**: PoC with positive, visible organizational impact — demo to Rick on 3/19
- **Guiding Principle**: AI that enhances productivity while *reinforcing* standards and governance — not enabling runaway tech debt or security holes at scale
- **Competition**: Also demoing ServiceNow Agentic AI and AWS Kiro — need a strong Azure showing

## Current State

- Customer uses **Azure MCP server + GitHub Copilot** for admin tasks (requires individual licensing + admin permissions)
- Teams have **least-privilege permissions** — read-only in Test/Prod
- Resource groups are close to being fully tagged with **support-owner** metadata
- **Azure Copilot Agents preview NOT enabled** — would need Mandy's team at tenant scope
- Christopher playing with **Azure SRE Agent** in sandbox — specifically RBAC role classification
- Shane identified value in Kiro finding WAF rules and cost items they didn't see before
- ServiceNow doing predictive failure + outage cost analysis

## What We Built

### Executive Reliability View (Rick's view)
- Reliability score 0-100 with four pillar assessments
- Azure Service Health integration — which of MY resources are affected
- Prioritized action cards

### Ops Center (Christopher/Shane's view)
- Real-time environment scanning with auto-refresh
- Deep intelligence beyond Azure Advisor (architecture smells, cross-resource correlation)
- Chaos demo — create a real security problem, detect in 10 seconds
- Morning Briefing — overnight digest from the crew
- Generate Remediation — Terraform/CLI code following OGE standards

### Ops Council (Chat)
- 6 AI agents with OGE-themed personalities that debate and argue
- Streaming responses via SSE — watch the debate live
- Transparent disagreement between Cost Sentinel and Standards Architect
- Compliance classification by The Inspector (policy bug vs workaround abuse)
- Token cost ticker for full transparency

## Customer-Requested Use Cases

### 1. Troubleshooting Without Elevated Access (Highest Impact)
- **Status**: ✅ Built — Turnaround agent diagnoses from Activity Logs, Resource Health
- **Demo**: "My deployment failed" scenario

### 2. Observability / Proactive Monitoring
- **Status**: ✅ Built — Flare Stack + auto-refresh + Morning Briefing + Service Health
- **Demo**: Morning Briefing, chaos detection, service health events

### 3. Cost Optimization with Standards Balance
- **Status**: ✅ Built — Barrel Counter vs The Roughneck debate system
- **Demo**: VM sizing scenario, waste analysis scenario

### 5. Continuous Compliance (Rick/Shane's ask)
- **Status**: ✅ Phase 1 built — The Inspector agent classifies policy violations
- **Demo**: Compliance inspection scenario (policy bugs, expired exemptions, workaround abuse)
- **Phase 2**: ✅ ADO integration built — Inspector → proposals → human approval → ADO work items/PRs
  - `POST /api/ado/inspect-and-propose` — full pipeline: scan → classify → propose
  - Human reviews proposals in dashboard, approves or rejects
  - Approved proposals generate ADO REST API payloads (PBI, Bug, Task, or PR)
  - ADO pipeline (`pipelines/azure-pipelines.yml`) with staging + production human gate

### 6. RBAC Role Classification (Christopher's personal interest)
- **Status**: 🔲 Planned for Phase 3
- **Notes**: Christopher already exploring with SRE Agent; strong candidate for follow-up
- **Path**: Add a new crew member specializing in RBAC analysis (architecture supports it)

### 7. Wiz Findings Validation (Christopher suggested)
- **Status**: 🔲 Phase 3 roadmap
- **Notes**: Feed Wiz output to crew for validation/enrichment

## Success Criteria (from the call)

- ✅ "Eye-catching and potentially have significant use" — OGE branding, crew personalities, chaos demo
- ✅ "Super scalable — one person could do it, AI manages the rest" — Managed Identity scans everything automatically
- ✅ "Spark curiosity with leadership" — Executive Reliability view with scores
- ✅ "Art of the possible" — deep intelligence, streaming debate, remediation code
- ✅ "Human in the loop — always validate" — crew recommends, never acts; Generate Remediation requires human approval

## Dependencies

| Item | Status | Notes |
|------|--------|-------|
| Azure OpenAI models | ✅ Done | Per-agent endpoint routing: eastus2 (gpt-5.4, o3) + westus3 (gpt-5-nano) |
| App Service Plan | ✅ Done | Sharing existing P0v3 in West US 2 |
| Managed Identity + RBAC | ✅ Done | Reader + Log Analytics Reader + Monitoring Reader |
| Bicep IaC | ✅ Done | Parameterized for customer deployment |
| OGE Branding | ✅ Done | Logo, colors (#101820, #DA291C, #CEE5E8), Inter font |
| Copilot Agents preview | ❌ Not needed | Custom solution doesn't depend on tenant preview |
