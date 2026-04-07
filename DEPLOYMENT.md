# Deployment Guide — Azure Resources Required

## Overview

The OGE Ops Council runs on Azure and requires the following resources. All infrastructure is defined in `infra/` as Bicep templates.

## Required Azure Resources

### 1. Azure AI Foundry / Azure OpenAI

You need **3 model deployments** in Azure AI Foundry (or Azure OpenAI):

| Deployment Name | Model Type | Used By | Purpose |
|----------------|-----------|---------|---------|
| `foundry-gpt` | General-purpose LLM (e.g., GPT-4o, GPT-4.1, GPT-5.x) | Pipeline, The Roughneck | Orchestration, synthesis, standards analysis |
| `foundry-reasoning` | Reasoning model (e.g., o3, o4-mini) | Barrel Counter, Turnaround, The Inspector | Deep cost analysis, diagnostics, compliance |
| `foundry-nano` | Lightweight model (e.g., GPT-4o-mini, GPT-5-nano) | Flare Stack | Fast scanning, alerting |

**Setup steps:**
1. Create an Azure OpenAI resource (or AI Foundry project) in your preferred region
2. Deploy the 3 models above with the exact deployment names shown
3. If using two regions, create a second account and set `AZURE_OPENAI_ENDPOINT_SECONDARY`
4. Grant the Managed Identity `Cognitive Services OpenAI User` on each account

### 2. Azure App Service

| Resource | SKU | Purpose |
|----------|-----|---------|
| Web App | P0v3 or higher | Hosts the Flask dashboard + API |
| App Service Plan | P0v3 (Linux) | Can be shared with other apps |

**Note:** The app runs Python 3.13 on Linux with Gunicorn. Minimum recommended: P0v3 for production, B1 for dev/test.

### 3. Managed Identity

| Resource | Purpose |
|----------|---------|
| User-Assigned Managed Identity | Auth to Azure OpenAI, Key Vault, Azure APIs |

**Required RBAC roles (subscription scope):**

| Role | Role Definition ID | Purpose |
|------|-------------------|---------|
| Reader | `acdd72a7-3385-48ef-bd42-f606fba81ae7` | Read all Azure resources |
| Log Analytics Reader | `73c42c96-874c-492b-b04d-ab87d138a893` | Query logs for diagnostics |
| Monitoring Reader | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` | Read Azure Monitor metrics |
| Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | Call Azure OpenAI APIs |
| Key Vault Secrets User | `4633458b-17de-408a-b874-0445c86b69e6` | Read secrets from Key Vault |
| Network Contributor | `4d97b98b-1d4f-4787-a291-c67834d212e7` | Chaos demo only (NSG rule changes) |

### 4. Key Vault

| Resource | SKU | Purpose |
|----------|-----|---------|
| Key Vault | Standard | Stores ADO PAT, any other secrets |

Private endpoint recommended for production.

### 5. Monitoring

| Resource | SKU | Purpose |
|----------|-----|---------|
| Log Analytics Workspace | PerGB2018 | Telemetry collection, agent diagnostics |
| Application Insights | (linked to Log Analytics) | App performance monitoring |

### 6. Networking (Optional but Recommended)

| Resource | Purpose |
|----------|---------|
| Virtual Network | Network isolation |
| NSGs | Network security rules + chaos demo target |
| Private Endpoints | Key Vault + optional App Service |

## Environment Variables

Set these on the App Service (or in `.env` for local dev):

| Variable | Required | Description |
|----------|----------|-------------|
| `AZURE_OPENAI_ENDPOINT` | Yes | Primary Azure OpenAI endpoint URL |
| `AZURE_OPENAI_ENDPOINT_SECONDARY` | If using 2 accounts | Secondary endpoint for models in another region |
| `AZURE_OPENAI_DEPLOYMENT` | Yes | Default deployment name (e.g., `foundry-gpt`) |
| `AZURE_CLIENT_ID` | Yes (Azure) | Managed Identity client ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Target subscription to monitor |
| `KEY_VAULT_URI` | Yes | Key Vault URI (e.g., `https://{prefix}-kv.vault.azure.net/`) |
| `LOG_ANALYTICS_WORKSPACE_ID` | Yes | Log Analytics workspace customer ID |

## Deployment Steps

### Option A: Bicep (Recommended)

```bash
# 1. Set your variables
export RESOURCE_GROUP="YourResourceGroup"
export LOCATION="eastus2"           # Pick your region
export PREFIX="yourprefix"          # Resource naming prefix

# 2. Deploy infrastructure
cd infra/
az deployment group create \
  --resource-group $RESOURCE_GROUP \
  --template-file main.bicep \
  --parameters main.bicepparam

# 3. Deploy the app
az webapp deploy \
  --resource-group $RESOURCE_GROUP \
  --name ${PREFIX}-app \
  --src-path app.zip \
  --type zip
```

### Option B: Azure Pipelines

See `pipelines/azure-pipelines.yml`. Configure these variable groups:
- `{PREFIX}-Staging` — staging environment variables
- `{PREFIX}-Production` — production environment variables

## Model Selection Guide

The deployment names are generic — map them to whichever models your Foundry account has:

| Deployment | Recommended Models | Notes |
|-----------|-------------------|-------|
| `foundry-gpt` | GPT-4o, GPT-4.1, GPT-5.x | Needs strong synthesis and broad knowledge |
| `foundry-reasoning` | o3, o3-mini, o4-mini | Needs multi-step reasoning (cost analysis, diagnostics) |
| `foundry-nano` | GPT-4o-mini, GPT-5-nano | Speed over depth — scanning and alerting |

## Cost Estimate

| Resource | Estimated Monthly Cost |
|----------|----------------------|
| App Service (P0v3) | ~$60/mo |
| Key Vault | ~$1/mo |
| Log Analytics (5GB/day) | ~$12/mo |
| Application Insights | Included with Log Analytics |
| Azure OpenAI tokens | ~$5-20/mo (depends on usage) |
| **Total** | **~$80-100/mo** |
