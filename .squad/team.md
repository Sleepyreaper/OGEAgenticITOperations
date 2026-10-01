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
| Tester | Tester / Eval Owner | `.squad/agents/tester/charter.md` | active |
| Scribe | Session Logger & Decision Merger | `.squad/agents/scribe/charter.md` | active |
| Ralph | Work Monitor | `.squad/agents/ralph/charter.md` | active |

## Project Context

- **Project:** OGEAgenticITOperations — OGE agentic Azure IT operations assistant (Flask, Azure App Service)
- **Stack:** Python 3 / Flask, Azure SDKs, Azure AI Foundry Agent Service (`azure-ai-projects`), Bicep + Terraform, Azure DevOps pipelines
- **Two squads, one pattern:** this *Build Squad* develops the app; the *runtime squad* (orchestrator + scout, cost_sentinel, diagnostics_sre, standards_architect, compliance_inspector in `profiles/oge/`) runs inside it on Foundry. See `docs/FOUNDRY_ARCHITECTURE.md`.
- **Created:** 2026-10-01
