# Azure SRE Agent assets: ZeroOps tier 1

Configuration that turns an existing **Azure SRE Agent** into the ZeroOps tier-1 triage agent. The
agent escalates to the OGE Agentic Ops squad through the app's MCP endpoint. For the design, cost
model and demo script, see [docs/ZEROOPS_SRE_AGENT.md](../docs/ZEROOPS_SRE_AGENT.md).

| Path | What it is |
|---|---|
| `agents/zeroops-triage.yaml` | Custom agent (`azuresre.ai/v2` ExtendedAgent): triage → classify → solo fix or `ogeops_escalate` |
| `skills/zeroops-escalation-policy/` | When to resolve solo vs. escalate, the evidence package and the human-approval contract |
| `skills/nsg-open-port-triage/` | Solo runbook: Internet-exposed management port on an NSG |
| `skills/webapp-bad-deploy-revert/` | Solo runbook: App Service broken by a config/startup change |
| `triggers/incident-filter.json` | Response plan: Azure Monitor alerts titled "ZeroOps…" → `zeroops-triage` (Review mode) |
| `triggers/scheduled-task.json` | Weekday sweep: escalate the top business-impacting open finding |
| `connectors/ogeops-mcp.json` | MCP connector shape (`ogeops` → `<app>/mcp`, `X-API-Key` header) |
| `scripts/configure-sre-agent.sh` | Idempotent installer for everything above (`DRY_RUN=1` to preview) |

```bash
export SRE_AGENT_RESOURCE_GROUP=<rg> SRE_AGENT_NAME=<agent> \
       OGE_APP_URL=https://<your-app> MCP_API_KEY=<same value as the app setting>
./sre-agent/scripts/configure-sre-agent.sh
```

Requirements: Azure CLI (signed in), python3 with PyYAML, and optionally `azmcp`
(`npm i -g @azure/mcp@latest`) for the MCP connector. Without `azmcp`, the script prints the portal steps.
The SRE Agent also needs network egress to the app and Reader access on the monitored resource groups.
