# Squad Team

> OGEAgenticITOperations

## Coordinator

| Name | Role | Notes |
|------|------|-------|
| Squad | Coordinator | Routes work, enforces handoffs and reviewer gates. |

## Members

| Name | Role | Charter | Status |
|------|------|---------|--------|
| Lead | Lead / Architect | `.squad/agents/lead/charter.md` | active |
| Evidence Engineer | Evidence Engineer | `.squad/agents/evidence-engineer/charter.md` | active |
| Foundry Engineer | Foundry / Model Platform Engineer | `.squad/agents/foundry-engineer/charter.md` | active |
| ZeroOps SRE Engineer | ZeroOps / Azure SRE Automation Engineer | `.squad/agents/zeroops-sre-engineer/charter.md` | active |
| Security & Identity Engineer | Cloud Security / Identity Engineer | `.squad/agents/security-identity-engineer/charter.md` | active |
| State & Workflow Engineer | Durable State / Workflow Reliability Engineer | `.squad/agents/state-workflow-engineer/charter.md` | active |
| Platform Release Engineer | Azure Platform / IaC / Release Engineer | `.squad/agents/platform-release-engineer/charter.md` | active |
| Operations UX Engineer | Operations Product UX Engineer | `.squad/agents/operations-ux-engineer/charter.md` | active |
| Tester | Agent Evaluation & Adversarial QA Engineer | `.squad/agents/tester/charter.md` | active |
| Scribe | Session Logger & Decision Merger | `.squad/agents/scribe/charter.md` | active |
| Ralph | Work Monitor | `.squad/agents/ralph/charter.md` | active |
| Rai | Responsible AI Reviewer | `.squad/agents/Rai/charter.md` | active |
| Fact Checker | Verification / Devil's Advocate | `.squad/agents/fact-checker/charter.md` | active |

## Project Context

- **Project:** OGEAgenticITOperations — OGE agentic Azure IT operations assistant (Flask, Azure App Service)
- **Stack:** Python 3 / Flask, Azure SDKs, Azure AI Foundry Agent Service (`azure-ai-projects`), Bicep + Terraform, Azure DevOps pipelines
- **Two squads, one pattern:** this *Build Squad* develops the app; the *runtime squad* (orchestrator + scout, cost_sentinel, diagnostics_sre, standards_architect, compliance_inspector in `profiles/oge/`) runs inside it on Foundry. See `docs/FOUNDRY_ARCHITECTURE.md`.
- **Loading rule:** the specialist roster in this file is repo-local. Squad loads it only when started from OGEAgenticITOperations; it does not expand the user's general-purpose business squad.
- **Created:** 2026-10-01
