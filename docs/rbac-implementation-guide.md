# OGE Ops Council — RBAC & Least-Privilege Implementation Guide

> **Guiding Principle**: AI that enhances productivity while *reinforcing* standards and governance policy. This solution must not enable runaway tech debt or security holes at scale. Every permission below follows the least-privilege model that OGE Digital Foundations mandates.

## Overview

The Ops Council uses a single **User-Assigned Managed Identity** to access Azure data. This identity is the *only* principal that needs RBAC assignments beyond the resource group that hosts the application. End users authenticate to the web app but **never receive elevated Azure permissions** — the Managed Identity acts on their behalf with scoped, read-only access.

This is the core value proposition: teams get operational intelligence *without* needing more access than their current read-only permissions in Test/Prod.

---

## Managed Identity RBAC Assignments

### Subscription-Level Roles (per monitored subscription)

These roles are assigned to the Ops Council Managed Identity on each Azure subscription the crew should monitor.

| Role | Role Definition ID | Scope | Purpose | Justification |
|------|-------------------|-------|---------|---------------|
| **Reader** | `acdd72a7-3385-48ef-bd42-f606fba81ae7` | Subscription | Resource Graph queries, resource enumeration, tagging compliance, resource health | Read-only. Cannot modify any resources. Allows the crew to see what exists and its current state. |
| **Log Analytics Reader** | `73c42c96-874c-492b-b04d-ab87d138a893` | Subscription or Log Analytics Workspace(s) | Query Log Analytics for activity logs, deployment failures, performance metrics | Read-only on log data. Cannot modify workspace config, retention, or access. Enables Turnaround's diagnostic capabilities. |
| **Monitoring Reader** | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` | Subscription | Read Azure Monitor metrics, alerts, and diagnostic settings | Read-only on monitoring data. Enables Barrel Counter's utilization analysis and Flare Stack's proactive scanning. |

**Total: 3 read-only roles per subscription.** No write, delete, or admin permissions anywhere.

### Resource-Level Roles (Ops Council infrastructure only)

These roles are scoped *only* to the resources that make up the Ops Council application itself — not to customer workloads.

| Role | Role Definition ID | Scope | Purpose |
|------|-------------------|-------|---------|
| **Key Vault Secrets User** | `4633458b-17de-408a-b874-0445c86b69e6` | Ops Council Key Vault | Read secrets (OpenAI endpoint, connection strings). Cannot create, update, or delete secrets. |
| **Cognitive Services OpenAI User** | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | Azure OpenAI Account | Send inference requests to o4-mini and other deployed models. Cannot manage deployments, keys, or account settings. |

---

## What Each Agent Reads (and Why)

| Agent | Azure APIs Used | Data Accessed | Role Required |
|-------|----------------|---------------|---------------|
| 🛢️ **Barrel Counter** | Resource Graph, Monitor Metrics | Resource inventory, SKU details, CPU/memory/disk utilization | Reader + Monitoring Reader |
| 🔧 **The Roughneck** | Resource Graph | Resource tags, SKU configuration, VNet topology, NSG rules | Reader |
| 🔄 **Turnaround** | Log Analytics, Resource Graph | Activity Logs, deployment operations, error entries, resource health | Reader + Log Analytics Reader |
| 🔥 **Flare Stack** | Resource Graph, Monitor Metrics, Log Analytics | Resource health status, quota usage, anomaly patterns, security config | Reader + Monitoring Reader + Log Analytics Reader |
| ⚡ **Pipeline** | None directly | Receives data from other agents, calls Azure OpenAI for synthesis | Cognitive Services OpenAI User |

---

## What the Managed Identity CANNOT Do

This is equally important to document — it demonstrates the governance-first design.

| Action | Allowed? | Explanation |
|--------|----------|-------------|
| Modify any Azure resource | ❌ No | Reader role is read-only by definition |
| Create or delete resources | ❌ No | No Contributor or Owner roles assigned |
| Change NSG rules or network config | ❌ No | Would require Network Contributor |
| Access storage account data (blobs, files) | ❌ No | Reader doesn't grant data plane access |
| View or manage RBAC assignments | ❌ No | Would require User Access Administrator |
| Modify Log Analytics workspace settings | ❌ No | Log Analytics Reader is read-only |
| Manage Azure OpenAI deployments | ❌ No | OpenAI User can only call models, not manage them |
| Access Key Vault admin operations | ❌ No | Secrets User can only read, not create/update/delete |
| Escalate its own permissions | ❌ No | No role that grants RBAC management |

---

## End User Access Model

End users interact with the Ops Council through the web application. Their Azure RBAC permissions are **not affected and not required** by this solution.

| User Type | Current OGE Access | Ops Council Access | Change to Their Permissions |
|-----------|-------------------|-------------------|----------------------------|
| DevOps team member | Read-only on their scope in Test/Prod | Full access to the Ops Council dashboard and crew chat | ❌ None — no elevation needed |
| Cloud Ops engineer | Admin across tenant | Full access + ability to act on crew recommendations | ❌ None |
| Management / Rick | Varies | Dashboard view for operational overview | ❌ None |

**This is the key differentiator**: teams get the *insight* they currently need admin access to obtain, without getting admin access. The Managed Identity reads on their behalf and the AI agents reason over the data.

---

## Implementation Steps for OGE Environment

### 1. Create the Managed Identity

```bash
az identity create \
  --name ogeops-id \
  --resource-group <OPS_COUNCIL_RG> \
  --location <REGION>
```

### 2. Assign Subscription-Level Roles (repeat per subscription)

```bash
MI_PRINCIPAL_ID=$(az identity show --name ogeops-id \
  --resource-group <OPS_COUNCIL_RG> --query principalId -o tsv)

# Reader
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Reader" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>"

# Log Analytics Reader
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Log Analytics Reader" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>"

# Monitoring Reader
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>"
```

### 3. Assign Resource-Level Roles

```bash
# Key Vault Secrets User (scoped to Ops Council KV only)
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "/subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.KeyVault/vaults/<KV_NAME>"

# Cognitive Services OpenAI User (scoped to the OpenAI account only)
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope "/subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<OPENAI_ACCOUNT>"
```

### 4. Scope Narrowing for Production

For tighter control, OGE can scope Log Analytics Reader and Monitoring Reader to specific workspaces or resource groups rather than the full subscription:

```bash
# Example: Scope Log Analytics Reader to a specific workspace
az role assignment create \
  --assignee-object-id $MI_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Log Analytics Reader" \
  --scope "/subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.OperationalInsights/workspaces/<WORKSPACE>"
```

---

## Network Security

| Control | Implementation | Notes |
|---------|---------------|-------|
| Key Vault | Private endpoint + deny public | Only accessible from the Ops Council VNet |
| Azure OpenAI | Private endpoint or VNet service endpoint | Only accessible from the Ops Council VNet |
| Web App | VNet-integrated, HTTPS-only, TLS 1.2+ | Only public-facing component |
| Storage | Deny public, VNet rules | For app runtime only |
| Managed Identity auth | No passwords, no keys | Entra ID token-based, auto-rotated |

---

## Compliance with OGE Governance

| OGE Standard | How Ops Council Aligns |
|-------------|----------------------|
| Least-privilege access model | 3 read-only roles + 2 resource-scoped roles. Zero write/admin. |
| Teams have read-only in Test/Prod | Unchanged. Ops Council provides insight without granting more access. |
| Terraform is the standard IaC tool | All infrastructure defined in Bicep (ARM-compatible). Can be converted to Terraform modules. |
| Resource groups tagged with support-owner | Flare Stack uses these tags for alert routing. The Roughneck validates tagging compliance. |
| Changes go through governance processes | Ops Council recommends changes but cannot execute them. Human approval required. |

---

## Summary

**Total RBAC assignments needed: 5 roles on 1 Managed Identity.**

- 3 read-only roles at subscription scope (Reader, Log Analytics Reader, Monitoring Reader)
- 2 resource-scoped roles (Key Vault Secrets User, Cognitive Services OpenAI User)
- Zero write permissions anywhere
- Zero changes to end user permissions
- Zero passwords or API keys (Managed Identity + Entra ID tokens only)

The Ops Council *reads* your environment and *reasons* about it. It never *changes* anything. That's the whole point.
