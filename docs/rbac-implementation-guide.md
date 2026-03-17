# OGE Ops Council — RBAC & Least-Privilege Implementation Guide

> **Guiding Principle**: AI that enhances productivity while *reinforcing* standards and governance policy. Every permission follows OGE's least-privilege model.
> 
> **Audience**: Any team deploying the Ops Council in their own Azure environment.

## Overview

The Ops Council uses a single **User-Assigned Managed Identity** to access Azure data. This identity is the *only* principal that needs RBAC assignments. End users authenticate to the web app but **never receive elevated Azure permissions** — the Managed Identity reads on their behalf.

This guide covers everything needed to deploy this RBAC model in any Azure subscription.

---

## Quick Start — Deploying to a New Environment

### Prerequisites

| Item | Required | Notes |
|------|----------|-------|
| Azure subscription(s) to monitor | Yes | One or more. Identity gets Reader across all of them. |
| Resource group for Ops Council infra | Yes | Contains: App Service, Key Vault, Managed Identity, networking |
| Azure OpenAI account(s) | Yes | One or two, depending on model availability per region |
| Azure DevOps project (Phase 2) | Optional | For automated PR/PBI creation from Inspector findings |
| Permissions to create RBAC assignments | Yes | Requires **User Access Administrator** or **Owner** on target scopes |

### Step-by-Step: New Environment Setup

```bash
# ═══════════════════════════════════════════════════════
# Step 1: Deploy infrastructure (creates the Managed Identity)
# ═══════════════════════════════════════════════════════
az deployment group create \
  --resource-group <YOUR_RG> \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam

# ═══════════════════════════════════════════════════════
# Step 2: Get the Managed Identity principal ID
# ═══════════════════════════════════════════════════════
# The prefix defaults to "ogeops" — adjust if you changed it.
MI_PRINCIPAL_ID=$(az identity show \
  --name ogeops-id \
  --resource-group <YOUR_RG> \
  --query principalId -o tsv)

echo "Managed Identity Principal ID: $MI_PRINCIPAL_ID"

# ═══════════════════════════════════════════════════════
# Step 3: Grant subscription-level Reader roles
# Run this for EACH subscription you want monitored.
# ═══════════════════════════════════════════════════════

# Option A: Use the Bicep module (recommended)
az deployment sub create \
  --location <REGION> \
  --template-file infra/modules/subscription-rbac.bicep \
  --parameters managedIdentityPrincipalId=$MI_PRINCIPAL_ID

# Option B: Azure CLI (equivalent)
SUBSCRIPTION_ID="<TARGET_SUBSCRIPTION_ID>"
for role in "Reader" "Log Analytics Reader" "Monitoring Reader"; do
  az role assignment create \
    --assignee-object-id $MI_PRINCIPAL_ID \
    --assignee-principal-type ServicePrincipal \
    --role "$role" \
    --scope "/subscriptions/$SUBSCRIPTION_ID"
done

# ═══════════════════════════════════════════════════════
# Step 4: Grant Key Vault Secrets User (Ops Council KV only)
# ═══════════════════════════════════════════════════════
# This is already handled by keyvault.bicep during infra deploy.
# Only run manually if deploying Key Vault separately.

KV_RESOURCE_ID=$(az keyvault show \
  --name <YOUR_KV_NAME> \
  --query id -o tsv)

az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "$KV_RESOURCE_ID"

# ═══════════════════════════════════════════════════════
# Step 5: Grant OpenAI User (on EACH OpenAI account)
# ═══════════════════════════════════════════════════════
# This is already handled by openai-rbac.bicep for the primary account.
# Run manually for additional regional endpoints.

OPENAI_RESOURCE_ID=$(az cognitiveservices account show \
  --name <OPENAI_ACCOUNT_NAME> \
  --resource-group <OPENAI_RG> \
  --query id -o tsv)

az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope "$OPENAI_RESOURCE_ID"

# ═══════════════════════════════════════════════════════
# Step 6 (OPTIONAL — Demo only): Network Contributor
# for the chaos demo NSG. REMOVE for production.
# ═══════════════════════════════════════════════════════
# NSG_RESOURCE_ID=$(az network nsg show \
#   --name <DEMO_NSG_NAME> \
#   --resource-group <YOUR_RG> \
#   --query id -o tsv)
# 
# az role assignment create \
#   --assignee-object-id $MI_PRINCIPAL_ID \
#   --assignee-principal-type ServicePrincipal \
#   --role "Network Contributor" \
#   --scope "$NSG_RESOURCE_ID"
```

---

## Complete Role Reference

### Subscription-Level Roles (per monitored subscription)

These three roles grant read-only access across the entire subscription. The Managed Identity needs these on **every subscription** you want the Ops Council to monitor.

| # | Role | Role Definition ID | Scope | Purpose | Bicep Module |
|---|------|-------------------|-------|---------|-------------|
| 1 | **Reader** | `acdd72a7-3385-48ef-bd42-f606fba81ae7` | Subscription | Resource Graph queries, resource enumeration, tagging, health, Advisor, Policy | `subscription-rbac.bicep` |
| 2 | **Log Analytics Reader** | `73c42c96-874c-492b-b04d-ab87d138a893` | Subscription | Activity logs, deployment failure analysis, KQL queries via Log Analytics | `subscription-rbac.bicep` |
| 3 | **Monitoring Reader** | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` | Subscription | Azure Monitor metrics, alerts, diagnostics, Resource Health, Service Health | `subscription-rbac.bicep` |

### Resource-Level Roles (Ops Council infrastructure only)

These roles are scoped to specific resources within the Ops Council's own resource group. They are **not** granted at subscription scope.

| # | Role | Role Definition ID | Scope | Purpose | Bicep Module |
|---|------|-------------------|-------|---------|-------------|
| 4 | **Key Vault Secrets User** | `4633458b-17de-408a-b874-0445c86b69e6` | Ops Council Key Vault | Read API keys and secrets | `keyvault.bicep` |
| 5 | **Cognitive Services OpenAI User** | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | Azure OpenAI Account(s) | Call deployed models (chat completions) | `openai-rbac.bicep` |
| 6 | **Network Contributor** ⚠️ | `4d97b98b-1d4f-4787-a291-c67834d212e7` | Demo NSG only | Chaos demo — create/delete NSG rule. **REMOVE IN PRODUCTION.** | Manual |

### Total: 5 read-only roles + 1 scoped demo role on 1 Managed Identity

---

## What Each Agent Reads

| Agent | Azure APIs Used | Data Accessed | Roles Required |
|-------|----------------|---------------|---------------|
| 🛢️ **Barrel Counter** (cost) | Resource Graph, Advisor | Resource inventory, SKUs, orphaned disks, cost recommendations | Reader |
| 🔧 **The Roughneck** (standards) | Resource Graph | Tags, SKU config, VNet topology, NSG rules, architecture patterns | Reader |
| 🔄 **Turnaround** (diagnostics) | Resource Graph, Activity Log, Log Analytics | Deployment errors, config changes, resource health | Reader + Log Analytics Reader |
| 🔥 **Flare Stack** (monitoring) | Resource Graph, Resource Health, Service Health | Health status, security drift, anomalies, platform incidents | Reader + Monitoring Reader |
| 📋 **The Inspector** (compliance) | Resource Graph, Policy Insights API | Policy compliance state, non-compliant resources, policy definitions | Reader |
| ⚡ **Pipeline** (orchestrator) | Azure OpenAI | Synthesizes other agents' outputs | Cognitive Services OpenAI User |

---

## What the Managed Identity CANNOT Do

| Action | Allowed? | Why |
|--------|----------|-----|
| Modify any Azure resource | ❌ | Reader is read-only |
| Create or delete resources | ❌ | No Contributor/Owner |
| Change NSG rules (production) | ❌ | Network Contributor scoped to demo NSG only |
| Access storage data (blobs, files) | ❌ | Reader doesn't include data plane |
| Manage RBAC assignments | ❌ | No User Access Administrator |
| Modify Log Analytics config | ❌ | Log Analytics Reader is read-only |
| Manage OpenAI deployments | ❌ | OpenAI User can only call models |
| Escalate its own permissions | ❌ | No role that grants RBAC management |
| Create or modify Azure Policy | ❌ | Reader can only read policy state |
| Access Key Vault keys or certs | ❌ | Secrets User only — not Keys/Certs |

---

## End User Access Model

The Ops Council does **not** change any user's existing Azure permissions.

| User Type | Current Access | Ops Council Access | Change Needed |
|-----------|---------------|-------------------|---------------|
| DevOps team | Read-only in Test/Prod | Full dashboard + crew chat | ❌ None |
| Cloud Ops engineer | Admin across tenant | Full access + act on recommendations | ❌ None |
| Executive (leadership) | Varies | Reliability dashboard view | ❌ None |
| ADO service connection (Phase 2) | Contributor on Ops Council RG | Deploy pipeline artifacts | See Phase 2 section |

---

## Multi-Subscription Deployment

The Ops Council natively supports monitoring multiple subscriptions. Resource Graph queries accept an array of subscription IDs.

```
┌─────────────────────────┐
│  Managed Identity       │
│  (ogeops-id)            │
└───┬───────┬───────┬─────┘
    │       │       │
    ▼       ▼       ▼
 Sub A   Sub B   Sub C     ← Reader + Log Analytics Reader + Monitoring Reader on each
```

**To add a new subscription:**

```bash
# Get MI principal ID (one-time)
MI_PRINCIPAL_ID=$(az identity show --name ogeops-id --resource-group <RG> --query principalId -o tsv)

# Grant roles on the new subscription
az deployment sub create \
  --location <REGION> \
  --template-file infra/modules/subscription-rbac.bicep \
  --parameters managedIdentityPrincipalId=$MI_PRINCIPAL_ID \
  --subscription <NEW_SUBSCRIPTION_ID>
```

The web app discovers accessible subscriptions at runtime via `GET /api/subscriptions`. No config change needed.

---

## Multi-Region OpenAI Deployment

Different AI models may only be available in specific Azure regions. The Ops Council supports per-agent endpoint routing.

```
┌─────────────────────────┐
│  Managed Identity       │
└───┬───────────────┬─────┘
    │               │
    ▼               ▼
 OpenAI (eastus2)   OpenAI (westus3)
 ├─ gpt-5.4         └─ gpt-5-nano (LightWork5Nano)
 ├─ o3
 └─ (future models)
```

**To add a new OpenAI account:**

```bash
OPENAI_ID=$(az cognitiveservices account show \
  --name <NEW_ACCOUNT> --resource-group <RG> --query id -o tsv)

az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope "$OPENAI_ID"
```

Then update the `AZURE_OPENAI_ENDPOINT_EASTUS2` (or add a new env var) in App Service config, and map agents to the new endpoint in `app/config.py`.

---

## Phase 2: Azure DevOps Integration RBAC

Phase 2 adds the ability for The Inspector to create work items (PBIs, Bugs) and pull requests in Azure DevOps. This requires additional permissions **in ADO, not in Azure**.

### ADO Permissions Required

| Scope | Permission | Purpose |
|-------|-----------|---------|
| ADO Project | **Work Item: Read & Write** | Create PBIs, Bugs, Tasks from Inspector findings |
| ADO Repository | **Code: Read & Write** | Create branches for policy-as-code PRs |
| ADO Repository | **Pull Request: Create** | Open PRs with policy definition fixes |
| ADO Environment | **Manage Approvals** | Human approval gate on production deploys |

### ADO Authentication Options

| Method | For PoC | For Production | Notes |
|--------|---------|---------------|-------|
| **PAT (Personal Access Token)** | ✅ | ❌ | Store in Key Vault as `ADO-PAT`. Scoped to project. Expires — rotate on schedule. |
| **Managed Identity + Service Connection** | ❌ | ✅ | ADO Workload Identity Federation. No secrets to manage. Requires ADO org admin setup. |
| **OAuth App Registration** | ❌ | ✅ | App registered in Entra ID, granted ADO permissions. For multi-tenant scenarios. |

### ADO Setup Steps (PoC — PAT method)

```bash
# 1. Create a PAT in ADO with these scopes:
#    - Work Items: Read & Write
#    - Code: Read & Write  
#    - Pull Requests: Read & Write
#    (Settings → Personal Access Tokens → New Token)

# 2. Store the PAT in Key Vault
az keyvault secret set \
  --vault-name <YOUR_KV_NAME> \
  --name "ADO-PAT" \
  --value "<YOUR_PAT_VALUE>"

# 3. Add ADO config to App Service
az webapp config appsettings set \
  --name <APP_NAME> \
  --resource-group <RG> \
  --settings \
    ADO_ORG_URL="https://dev.azure.com/<YOUR_ORG>" \
    ADO_PROJECT="<YOUR_PROJECT>" \
    ADO_REPO="<POLICY_REPO_NAME>"
```

### ADO Pipeline Environments (Human Gates)

The pipeline uses ADO Environments with approval checks for human-in-the-loop control:

| Environment | Auto-Deploy? | Approval Required | Approvers |
|------------|-------------|-------------------|-----------|
| `ops-council-staging` | Yes (on main merge) | No | — |
| `ops-council-production` | No | **Yes** | Cloud Ops leads |

**To configure approvals in ADO:**
1. Go to Pipelines → Environments → `ops-council-production`
2. Click "Approvals and checks" → Add → Approvals
3. Add approvers (Christopher, Shane, or equivalent)
4. Set timeout (e.g., 72 hours)

---

## Network Security

| Control | Implementation |
|---------|---------------|
| Key Vault | Private endpoint, deny public access |
| Azure OpenAI | Accessible via Managed Identity bearer token, no API keys |
| Web App | VNet-integrated, HTTPS-only, TLS 1.2+, public access configurable |
| Managed Identity | No passwords, Entra ID tokens, automatically rotated |
| ADO PAT (Phase 2) | Stored in Key Vault, read via Managed Identity, never in code |

---

## Compliance with Governance Standards

| Standard | How Ops Council Aligns |
|----------|----------------------|
| Least-privilege access | 5 read-only roles + 1 scoped demo role. Zero write permissions. |
| Teams have read-only in Test/Prod | Unchanged. Ops Council provides insight without granting more access. |
| Terraform is the standard IaC | Infra in Bicep, remediation output generates Terraform for the team's existing workflow. |
| Resource groups tagged with support-owner | Flare Stack uses tags for alert routing. The Roughneck validates tagging compliance. |
| Changes go through governance | Ops Council recommends but never auto-executes. Human approval required at every step. |
| ADO work items follow process | Phase 2 proposals are PENDING until human approves. ADO work items follow existing board/sprint process. |
| Policy changes require PR review | Policy bug PRs go through normal code review. Inspector writes the PR, humans review and merge. |

---

## Validation Checklist — New Environment

Run this after deploying to a new environment to verify all RBAC is correct:

```bash
# ═══════════════════════════════════════════════════════
# Validation Script — Run after deployment
# ═══════════════════════════════════════════════════════

APP_URL="https://<YOUR_APP>.azurewebsites.net"

echo "1. Health check..."
curl -s "$APP_URL/api/health" | python3 -m json.tool

echo ""
echo "2. Subscription discovery..."
curl -s "$APP_URL/api/subscriptions" | python3 -m json.tool

echo ""
echo "3. Overview scan (tests Reader + Monitoring Reader)..."
curl -s "$APP_URL/api/scan/overview" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Resources: {d.get(\"total_resources\", \"ERROR\")}')
print(f'Resource Groups: {d.get(\"resource_groups\", \"ERROR\")}')
print(f'Health monitored: {d.get(\"resource_health\", {}).get(\"total_monitored\", \"ERROR\")}')
print(f'Advisor recs: {d.get(\"advisor\", {}).get(\"total\", \"ERROR\")}')
print(f'Policy compliance: {d.get(\"policy_compliance\", {}).get(\"compliance_pct\", \"ERROR\")}%')
"

echo ""
echo "4. Security scan..."
curl -s "$APP_URL/api/scan/security" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Drift findings: {d.get(\"count\", \"ERROR\")}')
"

echo ""
echo "5. Compliance scan (tests Reader + Policy)..."
curl -s "$APP_URL/api/scan/compliance" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Non-compliant: {d.get(\"count\", \"ERROR\")}')
print(f'Compliance: {d.get(\"summary\", {}).get(\"compliance_pct\", \"ERROR\")}%')
"

echo ""
echo "✅ If all checks returned data (not ERROR), RBAC is configured correctly."
echo "❌ If any show ERROR, check role assignments for the Managed Identity."
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `AuthorizationFailed` on Resource Graph | Missing **Reader** on target subscription | Run `subscription-rbac.bicep` for that sub |
| `AuthorizationFailed` on Activity Logs | Missing **Log Analytics Reader** | Run `subscription-rbac.bicep` for that sub |
| Empty metrics / health data | Missing **Monitoring Reader** | Run `subscription-rbac.bicep` for that sub |
| `AuthenticationError` on OpenAI calls | Missing **Cognitive Services OpenAI User** | Grant on each OpenAI account |
| `SecretNotFound` from Key Vault | Missing **Key Vault Secrets User** | Check KV role + ensure secret exists |
| `ForbiddenError` on Policy Insights | Missing **Reader** (Policy Insights needs Reader) | Run `subscription-rbac.bicep` |
| No subscriptions discovered | Identity has no Reader on any subscription | Grant Reader on at least one sub |
| Can't create chaos demo rules | Missing **Network Contributor** on demo NSG | Grant scoped to specific NSG (demo only) |

---

## Summary

**5 read-only roles + 1 scoped demo role on 1 Managed Identity.** Zero changes to user permissions. Zero passwords. Zero write access to customer resources.

The Ops Council reads your environment and reasons about it — it never changes anything. Phase 2 adds the ability to *propose* changes (PRs and work items in ADO), but every proposal requires human approval before anything is created.
