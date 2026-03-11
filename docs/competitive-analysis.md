# OGE Ops Council — Competitive Analysis

> **Context**: OGE is demoing three AI solutions to Rick on 3/19. This document provides competitive positioning for the Azure demo (Ops Council) against ServiceNow Agentic AI and AWS Kiro.

---

## The Three Demos at a Glance

| | **ServiceNow Agentic AI** | **AWS Kiro** | **OGE Ops Council (Azure)** |
|--|--------------------------|-------------|---------------------------|
| **What it is** | AI agents for ITSM workflows — incident triage, ticket routing, request fulfillment | AI-powered IDE — spec-driven coding, turns natural language into structured code | Multi-agent operational intelligence — 5 specialized AI agents that debate and analyze Azure cloud operations |
| **Category** | IT Service Management automation | Developer productivity / code generation | Cloud Operations / Observability / Cost / Governance |
| **Platform** | ServiceNow SaaS | Desktop IDE (AWS-hosted models) | Azure-native (Web App, OpenAI, Resource Graph, Monitor) |
| **Target user** | Help desk, ITSM teams | Software developers | Cloud Ops, DevOps teams, platform engineering |
| **Cloud provider** | Cloud-agnostic (SaaS) | AWS-centric | Azure-native |

---

## What They'll Likely Demo

### ServiceNow Agentic AI
- Incident auto-triage: ticket comes in → AI categorizes, routes to correct team
- Request auto-fulfillment: password reset, access provisioning
- AI Agent Orchestrator coordinating multiple workflow agents
- AI Agent Studio: build agents with natural language
- **Strengths**: Mature platform, production-ready, strong ITSM integration
- **Weakness for this use case**: Manages tickets *about* cloud issues but can't *diagnose* them. Doesn't read Azure Monitor, Resource Graph, or Activity Logs.

### AWS Kiro
- Developer describes feature in natural language → Kiro generates EARS-notation requirements
- Creates architecture, implementation plan, then writes code
- Spec-driven development: "vibe coding to viable code"
- Agent hooks that auto-run tests and docs on file save
- **Strengths**: Strong developer experience, spec-driven approach, good for greenfield coding
- **Weakness for this use case**: It's an IDE. Doesn't touch production operations, monitoring, cost, or governance. Also: it's AWS in an Azure-centric demo.

---

## Why Ops Council Wins for OGE's Specific Ask

### 1. Directly addresses their #1 pain point

The customer said: *"Diagnosing issues without giving users elevated access to Test/Prod would be a massive win for the company."*

- **ServiceNow**: Knows a ticket was opened. Can't tell you *why* the deployment failed.
- **Kiro**: Helps write code. Doesn't touch production operations.
- **Ops Council**: Turnaround reads Activity Logs, correlates resource health, and produces root cause analysis — all through a Managed Identity. Users never need elevated access.

### 2. Reinforces governance (their guiding principle)

The customer said: *"AI that enhances productivity while reinforcing standards and governance policy."*

- **ServiceNow**: Governance-neutral for cloud ops. Automates ITSM workflows but doesn't understand Azure configurations or Terraform standards.
- **Kiro**: No concept of organizational standards. Generates code based on generic best practices.
- **Ops Council**: The Roughneck is explicitly grounded in OGE's standards, Terraform conventions, and governance policies. Barrel Counter can't cut costs without The Roughneck weighing in.

### 3. Multi-perspective reasoning vs. single answers

- **ServiceNow**: AI Agent Orchestrator coordinates sequential task execution. Doesn't have agents with opposing viewpoints.
- **Kiro**: Single-agent. One AI writes your code.
- **Ops Council**: Barrel Counter says "downsize this VM." The Roughneck responds "that VM peaks at 94% CPU on month-end batch." Pipeline shows both perspectives and lets the human decide. Transparent disagreement builds trust.

### 4. Azure-native, purpose-built for their environment

- **ServiceNow**: Separate SaaS. Another license, integration, vendor.
- **Kiro**: AWS product. Showing AWS tooling in an Azure operations demo sends the wrong signal.
- **Ops Council**: Runs *on* Azure, queries *their* subscription, uses *their* OpenAI deployments, Managed Identity with least-privilege RBAC. Deployable via Bicep IaC.

### 5. Already running as a PoC

- **ServiceNow**: Would require procurement, licensing, CMDB setup.
- **Kiro**: Developer adoption program, AWS accounts, nothing to do with operations.
- **Ops Council**: Live at a URL. Real subscription data. 5 agents operational. Demo-ready *today*.

---

## The Knockout Framing

> **ServiceNow answers**: "How do we automate ticket routing?"
> **Kiro answers**: "How do we write code faster?"
> **Ops Council answers**: "How do we give teams operational intelligence without giving them admin access — while ensuring every recommendation respects our engineering standards?"

Only one of these matches what OGE's Cloud Ops team asked for.

---

## Objection Handling

| Objection | Response |
|-----------|----------|
| "ServiceNow is enterprise-proven at scale" | Agreed — for ITSM. But this demo is about cloud operational intelligence, not ticket management. The Ops Council could *feed findings into* ServiceNow as incidents. They complement, not compete. |
| "Kiro has spec-driven development which is innovative" | Kiro is impressive for developers. But the audience is Cloud Ops, and the ask is operational diagnostics without access elevation. Different problem space entirely. |
| "This is a PoC, not a product" | Correct — and that's the point. We built this in a day, tailored to OGE's exact requirements, running on their Azure patterns. A product would take months to customize to this level. |
| "Can this scale to multiple subscriptions?" | Yes. Add Reader + Log Analytics Reader + Monitoring Reader per subscription. The Managed Identity model is designed for multi-subscription scoping. See the RBAC guide. |
| "What about cost data?" | Azure Cost Management Reader role can be added. We scoped the PoC to operational intelligence first, but cost analysis is a natural extension — Barrel Counter is already designed for it. |
| "We already use Azure Copilot" | Azure Copilot requires individual licensing and the user to have permissions on the resources. Ops Council gives the team insight *without* individual licensing or elevated permissions. It's the force multiplier for least-privilege environments. |
