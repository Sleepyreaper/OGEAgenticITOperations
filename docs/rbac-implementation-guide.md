# OGE Ops Council — RBAC & Least-Privilege Implementation Guide

> **Guiding Principle**: AI that enhances productivity while *reinforcing* standards and governance policy. Every permission follows OGE's least-privilege model.

## Overview

The Ops Council uses a single **User-Assigned Managed Identity** to access Azure data. This identity is the *only* principal that needs RBAC assignments. End users authenticate to the web app but **never receive elevated Azure permissions** — the Managed Identity reads on their behalf.

---

## Managed Identity RBAC Assignments

### Subscription-Level Roles (per monitored subscription)

| Role | Role Definition ID | Scope | Purpose |
|------|-------------------|-------|---------|
| **Reader** | `acdd72a7-3385-48ef-bd42-f606fba81ae7` | Subscription | Resource Graph, resource enumeration, tagging, health |
| **Log Analytics Reader** | `73c42c96-874c-492b-b04d-ab87d138a893` | Subscription | Activity logs, deployment failures, KQL queries |
| **Monitoring Reader** | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` | Subscription | Azure Monitor metrics, alerts, diagnostics |

### Resource-Level Roles (Ops Council infrastructure only)

| Role | Role Definition ID | Scope | Purpose |
|------|-------------------|-------|---------|
| **Key Vault Secrets User** | `4633458b-17de-408a-b874-0445c86b69e6` | Ops Council Key Vault | Read secrets only |
| **Cognitive Services OpenAI User** | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | Azure OpenAI Accounts (westus3 + eastus2) | Call models across both regional endpoints |
| **Network Contributor** | `4d97b98b-1d4f-4787-a291-c67834d212e7` | Demo NSG only | Chaos demo NSG rule create/delete (remove in production) |

---

## What Each Agent Reads

| Agent | Azure APIs Used | Data Accessed | Role Required |
|-------|----------------|---------------|---------------|
| 🛢️ **Barrel Counter** | Resource Graph, Advisor | Resource inventory, SKUs, orphaned disks, cost recommendations | Reader |
| 🔧 **The Roughneck** | Resource Graph | Tags, SKU config, VNet topology, NSG rules, architecture patterns | Reader |
| 🔄 **Turnaround** | Resource Graph, Log Analytics | Activity Logs, deployment errors, resource health | Reader + Log Analytics Reader |
| 🔥 **Flare Stack** | Resource Graph, Resource Health, Service Health | Health status, security drift, anomalies, platform incidents | Reader + Monitoring Reader |
| 📋 **The Inspector** | Resource Graph, Policy Insights API | Policy compliance state, non-compliant resources, policy definitions | Reader |
| ⚡ **Pipeline** | Azure OpenAI | Synthesizes other agents' outputs | Cognitive Services OpenAI User |

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

---

## End User Access Model

| User Type | Current Access | Ops Council Access | Change Needed |
|-----------|---------------|-------------------|---------------|
| DevOps team | Read-only in Test/Prod | Full dashboard + crew chat | ❌ None |
| Cloud Ops engineer | Admin across tenant | Full access + act on recommendations | ❌ None |
| Executive (Rick) | Varies | Reliability dashboard view | ❌ None |

---

## Implementation Commands

```bash
# 1. Get the Managed Identity principal ID
MI_PRINCIPAL_ID=$(az identity show --name ogeops-id \
  --resource-group <RG> --query principalId -o tsv)

# 2. Subscription-level roles (repeat per subscription)
for role in "Reader" "Log Analytics Reader" "Monitoring Reader"; do
  az role assignment create \
    --assignee-object-id $MI_PRINCIPAL_ID \
    --assignee-principal-type ServicePrincipal \
    --role "$role" \
    --scope "/subscriptions/<SUB_ID>"
done

# 3. Key Vault Secrets User (Ops Council KV only)
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "<KV_RESOURCE_ID>"

# 4. OpenAI User (BOTH regional accounts)
for openai_id in "<OPENAI_WESTUS3_ID>" "<OPENAI_EASTUS2_ID>"; do
  az role assignment create \
    --assignee-object-id $MI_PRINCIPAL_ID \
    --assignee-principal-type ServicePrincipal \
    --role "Cognitive Services OpenAI User" \
    --scope "$openai_id"
done
```

---

## Network Security

| Control | Implementation |
|---------|---------------|
| Key Vault | Private endpoint, deny public |
| Azure OpenAI | Accessible via Managed Identity token |
| Web App | VNet-integrated, HTTPS-only, TLS 1.2+ |
| Managed Identity | No passwords, Entra ID tokens, auto-rotated |

---

## Compliance with OGE Governance

| OGE Standard | How Ops Council Aligns |
|-------------|----------------------|
| Least-privilege access | 5 read-only roles on 2 OpenAI accounts. Zero write/admin. |
| Teams have read-only in Test/Prod | Unchanged. Ops Council provides insight without granting more access. |
| Terraform is the standard IaC | Infrastructure defined in Bicep. Remediation output generates Terraform. |
| Resource groups tagged with support-owner | Flare Stack uses tags for routing. The Roughneck validates compliance. |
| Changes go through governance | Ops Council recommends but never acts. Human approval required. |

---

## Summary

**5 read-only roles + 1 scoped demo role on 1 Managed Identity.** Zero changes to user permissions. Zero passwords. The Ops Council reads your environment and reasons about it — it never changes anything.
