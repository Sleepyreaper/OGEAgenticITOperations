# OGE Envisioning — Requirements

## Org Context

- **Scope**: Cloud Ops/Engineering — Digital Foundations organization
- **Goal**: PoC with positive, visible organizational impact
- **Guiding Principle**: AI that enhances productivity while *reinforcing* standards and governance — not enabling runaway tech debt or security holes at scale

## Current State

- Customer already uses **Azure MCP server + GitHub Copilot** for admin tasks (recon, one-time scripts, mass updates)
- That workflow requires **individual licensing + admin permissions** across the tenant
- Other teams have **least-privilege permissions** to specific scopes, read-only in Test/Prod
- Resource groups are close to being fully tagged with **support owner** metadata
- **Azure Copilot Agents preview is NOT enabled** — would need Mandy's team to enable at tenant scope (timing risk)

## Three Candidate Scenarios (Ranked by Customer Interest)

### 1. Troubleshooting Agent (Highest Impact)
- Diagnose issues in Test/Prod **without giving users elevated access**
- Core problem: DevOps teams spend too much time lobbying for more access instead of learning to work within governance guardrails
- AI performs targeted analysis and recommendations on behalf of teams
- **Win-win**: better adherence to standards + smoother operations

### 2. Observability Agent
- Investigations, initial analysis summaries, and routing notifications to responsible parties
- Aligns with SRE Agent concept — scheduled/continuous monitoring + alerting
- Leverages resource group support-owner tags for notification routing
- Produce write-ups for appropriate teams

### 3. Deployment Agent
- Speed up teams' ability to deploy following **OGE standards with Terraform**
- Off-load common issues from Cloud Ops team
- Free up Cloud Ops for higher-value work

## Key Challenges

- Azure Copilot Agents preview not enabled — tenant-scope change needed (Mandy's team), timing uncertain
- Least-privilege model means most teams can't self-serve diagnostics today
- No standardized operational processes across DevOps teams
- Teams default to requesting more access rather than improving their workflows

## Success Criteria

- Demonstrate a PoC that visibly reduces time-to-resolution for a common operational scenario
- Show that AI can operate within governance guardrails (no privilege escalation needed)
- Prove that teams can get actionable answers without elevated access
- Maintain (or improve) adherence to OGE standards and governance policy

## Dependencies & Blockers

| Item | Owner | Status | Risk |
|------|-------|--------|------|
| Azure Copilot Agents preview enablement | Mandy's team | Not started | Timing — tenant-scope change |
| Resource group support-owner tagging | Cloud Ops | Near complete | Low |
| Azure SRE Agent evaluation | Customer | Not started | Capability fit unclear |
