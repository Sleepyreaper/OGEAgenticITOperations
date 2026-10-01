---
metadata:
  api_version: azuresre.ai/v2
  kind: Skill
name: nsg-open-port-triage
description: Use when an alert or Activity Log event shows a Network Security Group rule was created or changed to allow inbound management ports (22, 3389, 5985/5986) or any port from the Internet (source * / Internet / 0.0.0.0/0). Confirms the exposure, identifies who changed it, and proposes deleting the offending rule. SOLO-resolvable.
tools:
  - RunAzCliReadCommands
  - RunAzCliWriteCommands
---

## NSG open management port — triage

### 1. Confirm the exposure
- `az network nsg rule list -g <rg> --nsg-name <nsg> -o table`
- Flag rules with `direction=Inbound`, `access=Allow`, source `*`/`Internet`/`0.0.0.0/0` and a
  destination port of 22, 3389, 5985, 5986 or `*`.

### 2. Who and when
- `az monitor activity-log list -g <rg> --offset 2h --query "[?contains(operationName.value,'securityRules/write')].{who:caller,when:eventTimestamp,rule:resourceId}" -o table`
- Always query by resource group (`-g`). `--resource-id` on a rule (a child resource) returns a
  `ValidationError`.

### 2b. Blast radius
- `az network nsg show -g <rg> -n <nsg> --query "{nics:networkInterfaces[].id,subnets:subnets[].id}"`
- With no NIC or subnet attached, the rule exposes nothing yet: deletion is low-risk.
- Stop here. Once the rule, writer and attachments are known, skip App Insights and Log Analytics
  unless these checks suggest wider impact.

### 3. Decide
- Exactly one offending rule added recently and nothing depends on it → **SOLO**.
- Rule older than 24h, referenced in a change record, or several rules changed → **ESCALATE**
  per `zeroops-escalation-policy` (dependency unknown).

### 4. Fix (SOLO, operator approves in Review mode)
- `az network nsg rule delete -g <rg> --nsg-name <nsg> -n <rule>`
- Verify: re-list rules and confirm no Internet-sourced management port remains.

### 5. Report
Root cause (who/when/what), the rule removed, verification output, and a recommendation to use
Azure Bastion or just-in-time access instead of open management ports.
