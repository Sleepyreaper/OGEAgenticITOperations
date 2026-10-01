---
metadata:
  api_version: azuresre.ai/v2
  kind: Skill
name: zeroops-escalation-policy
description: Use on every incident after initial triage to decide whether the SRE Agent resolves it SOLO with a known runbook or ESCALATES to the OGE Agentic Ops squad via the ogeops_escalate MCP tool. Defines the escalation criteria, the evidence package and the human-approval contract.
tools:
  - RunAzCliReadCommands
---

## ZeroOps escalation policy

ZeroOps means **no human investigates from scratch**. The SRE Agent fixes the simple, well-understood
issues; the OGE Agentic Ops squad reasons about the rest; a human only approves.

### Resolve SOLO when ALL are true
- The cause is a **single, recent, reversible change** visible in the Activity Log
  (e.g. a new NSG rule, a startup command / app setting change).
- A triage skill exists for it (`nsg-open-port-triage`, `webapp-bad-deploy-revert`).
- The fix is one write that restores the previous known-good value.
- No data, identity, certificate or cost-commitment decision is involved.

### ESCALATE when ANY is true
| Signal | Example |
|---|---|
| Cross-domain | Performance problem that is also a cost decision (scale up vs. optimize) |
| Business impact unclear | Customer-facing degradation with unknown blast radius |
| No safe runbook | Unknown cause, several simultaneous changes, or a fix that is not a simple revert |
| Security/compliance trade-off | A hotfix weakened security config and reverting may break the hotfix |
| Deadline + failed automation | Certificate/secret expiring soon AND the renewal automation failed |
| Repeat incident | Same alert fired 3+ times in 24h — needs a durable fix, not another revert |

### Evidence package for `ogeops_escalate`
- `question`: the decision you need, in one sentence.
- `sre_summary`: what you checked (queries, CLI commands), what you found, what you ruled out,
  and the affected ARM resource IDs.
- `incident_ref`: alert / incident id or URL.
- `severity`: `critical` | `high` | `medium` | `low` (Sev0–1 → critical/high, Sev2 → medium, Sev3–4 → low).
- `scenario_id`: only for demo alerts whose title contains a ZeroOps scenario id
  (call `ogeops_list_scenarios` to see them).
- `debate`: `true` when the decision is a trade-off (cost vs. reliability, security vs. availability).

### Human-approval contract
- The squad returns an analysis, a remediation script and (via `ogeops_propose_fix`) a proposal.
- You **never** execute the squad's script. Post it in the thread and point the operator to the
  ZeroOps view of the OGE Ops console to Approve / Reject / Resolve.
