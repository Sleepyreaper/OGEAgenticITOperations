---
metadata:
  api_version: azuresre.ai/v2
  kind: Skill
name: webapp-bad-deploy-revert
description: Use when an App Service web app starts returning HTTP 5xx or fails to start right after a configuration or deployment change (startup command, app setting, runtime stack). Correlates the failure with the change in the Activity Log and proposes reverting that single change. SOLO-resolvable.
tools:
  - RunAzCliReadCommands
  - RunAzCliWriteCommands
  - QueryAppInsightsByResourceId
---

## Web app bad deploy — triage and revert

### 1. Confirm the failure
- `az webapp show -g <rg> -n <app> --query "{state:state,host:defaultHostName}"`
- App Insights: `requests | where timestamp > ago(30m) | summarize total=count(), failed=countif(success==false) by bin(timestamp,5m)`

### 2. Find the change
- `az monitor activity-log list -g <rg> --offset 2h --query "[?contains(operationName.value,'Microsoft.Web/sites')].{op:operationName.value,who:caller,when:eventTimestamp}" -o table`
- Query the Activity Log by resource group (`-g`), not `--resource-id` on child resources such as `sites/config`.
- `az webapp config show -g <rg> -n <app> --query "{cmd:appCommandLine,stack:linuxFxVersion}"`

### 3. Decide
- Failure began within minutes of one config write (e.g. `appCommandLine` changed) → **SOLO**.
- Code deployment with schema/data changes, or failure not correlated with a change → **ESCALATE**.

### 4. Fix (SOLO, operator approves in Review mode)
- Startup command: `az webapp config set -g <rg> -n <app> --startup-file ""` (or the previous value).
- App setting: `az webapp config appsettings set -g <rg> -n <app> --settings KEY=<previous>`.
- Verify: wait 60s, confirm HTTP 200 on the site root and the 5xx rate back to baseline.

### 5. Report
The change that broke it (who/when/what), the revert, before/after failure rate, and a recommendation to
use deployment slots with swap so bad config never reaches production.
