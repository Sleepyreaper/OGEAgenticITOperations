# Deployment Guide

This guide walks through deploying this app end-to-end: from a fresh clone to a
running, branded instance monitoring your Azure subscription. It's MIT licensed —
deploy it for yourself, a customer, or redistribute it as part of your own product.

## Overview

All infrastructure is defined in `infra/` as Bicep templates. Branding, agent
identity/prompts, and per-agent model routing are controlled by the **profile
system** (`profiles/<id>/`) and environment variables — see [BRANDING.md](BRANDING.md)
for the full customization guide. This document covers the infrastructure/deployment
side.

## Step-by-step flow

1. **Prerequisites** — see below
2. **Run the setup wizard** — `python3 scripts/configure.py`
3. **Deploy model deployments** in Azure AI Foundry / Azure OpenAI
4. **Deploy infrastructure** — `cd infra && bash deploy.sh`
5. **Grant RBAC** to the Managed Identity
6. **Deploy the app code**
7. **Verify** — `GET /api/health`
8. **Customize** — prompts, models, branding (optional, any time)
9. **Upgrade** — pulling future changes into your fork

---

## 1. Prerequisites

| Item | Required | Notes |
|------|----------|-------|
| Azure CLI (`az`) | Yes | Logged in (`az login`) with access to the target subscription(s) |
| Python 3.11+ | Yes | For the setup wizard (stdlib only) and running the app locally |
| Azure subscription(s) to monitor | Yes | One or more. The Managed Identity gets Reader across all of them |
| Existing App Service Plan | Yes | Linux, P0v3 or higher recommended for production, B1 for dev/test |
| Azure OpenAI / AI Foundry account(s) | Yes | One account is enough; a second is optional (see step 3) |
| Permissions to create RBAC assignments | Yes | Requires **User Access Administrator** or **Owner** on target scopes |
| Azure DevOps project | Optional | Only for the Phase 2 (ADO proposal) integration |

## 2. Run the setup wizard

```bash
python3 scripts/configure.py
```

This interactive wizard asks for your profile, app/customer/industry details,
Azure subscription ID, Azure OpenAI endpoint(s), and per-agent
deployment/endpoint choices, then generates two **local, git-ignored** files:

- `.env` — for running the app locally (`python3 wsgi.py`)
- `infra/main.bicepparam` — for the Bicep deployment in step 4

It never modifies `.env.example` or `infra/main.bicepparam.example` (the
checked-in templates), and never prints secrets.

For automation/CI, use `--non-interactive` with CLI flags, or `--answers
answers.json` with a JSON file of pre-filled answers:

```bash
python3 scripts/configure.py --non-interactive \
  --subscription-id 11111111-2222-3333-4444-555555555555 \
  --openai-endpoint https://<account>.openai.azure.com/ \
  --openai-account-name <account> --openai-resource-group <rg> \
  --app-service-plan-id /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Web/serverfarms/<plan>
```

Run `python3 scripts/configure.py --help` for the full flag reference, or
`--list-profiles` to see checked-in profiles. To rebrand for a new customer,
answer "yes" to "Create a NEW profile" (or pass `--new-profile <id>`) — this
clones `profiles/generic/` into `profiles/<id>/`, which you then commit to your
fork alongside your other code changes.

If you'd rather not run the wizard, copy the templates by hand and edit them:

```bash
cp .env.example .env
cp infra/main.bicepparam.example infra/main.bicepparam
```

## 3. Deploy model deployments (Azure AI Foundry / Azure OpenAI)

You need **3 model deployments** by default (the profile system lets you use
more or fewer by editing `profiles/<id>/profile.json`, but the default "oge"
and "generic" profiles both expect these three):

| Deployment Name | Model Type | Used By (default profiles) | Purpose |
|----------------|-----------|---------|---------|
| `foundry-gpt` | General-purpose LLM (e.g., GPT-4o, GPT-4.1, GPT-5.x) | Orchestrator, Standards Architect | Orchestration, synthesis, standards analysis |
| `foundry-reasoning` | Reasoning model (e.g., o3, o4-mini) | Cost Analyst, Diagnostics Specialist, Compliance Auditor | Deep cost analysis, diagnostics, compliance |
| `foundry-nano` | Lightweight model (e.g., GPT-4o-mini, GPT-5-nano) | Monitoring Scout | Fast scanning, alerting |

**Setup steps:**
1. Create an Azure OpenAI resource (or AI Foundry project) in your preferred region.
2. Deploy the 3 models above with the exact deployment names shown (or your own
   names — just make sure they match what you tell the setup wizard / put in
   `AZURE_OPENAI_DEPLOYMENT` / `AGENT_<KEY>_DEPLOYMENT`).
3. If using a second account/region, note its endpoint — the wizard will ask
   whether to configure a secondary endpoint.
4. RBAC (`Cognitive Services OpenAI User`) is granted automatically by the Bicep
   deployment in the next step — you don't need to grant it by hand.

> **Already have an existing Azure AI Foundry account/project with models
> deployed?** You don't need to create anything new — just point the wizard
> (or `.env`/`main.bicepparam` directly) at that account's endpoint, resource
> group, and account name, and set each agent's deployment name
> (`AZURE_OPENAI_DEPLOYMENT` / `AGENT_<KEY>_DEPLOYMENT`) to whatever your
> existing deployments are called. `--openai-account-name` /
> `--openai-resource-group` / `--openai-endpoint` (and the equivalent
> `openaiAccountName` / `openaiResourceGroup` / `openaiEndpoint` Bicep params)
> accept any account you already have access to — there's nothing
> account-specific baked into this app; it's purely configuration.

## 4. Deploy infrastructure (Bicep)

```bash
cd infra
bash deploy.sh
```

`deploy.sh` checks for `infra/main.bicepparam`, confirms your Azure CLI login,
creates the resource group, and runs `az deployment group create`. Under the
hood it deploys:

| Resource | Purpose |
|----------|---------|
| Virtual Network + NSGs | Network isolation for the web app and private endpoints |
| User-Assigned Managed Identity | Auth to Azure OpenAI, Key Vault, Azure APIs — the *only* principal with RBAC |
| Key Vault (private endpoint) | Stores ADO PAT and any other secrets |
| Log Analytics + Application Insights | Telemetry and app performance monitoring |
| Web App (Linux, Python) | Hosts the Flask app, on your existing App Service Plan |
| OpenAI RBAC (cross-RG) | Grants the Managed Identity access to your Azure OpenAI account(s) |

Key `main.bicep` parameters (all documented with `@description` in the file
and in `infra/main.bicepparam.example`):

| Parameter | Purpose | Default |
|-----------|---------|---------|
| `appProfile` | Which checked-in profile (`profiles/<id>/`) the app loads | `oge` |
| `subscriptionId` | Subscription the agents monitor (→ `AZURE_SUBSCRIPTION_ID`) | current subscription |
| `additionalOpenAiAccounts` | Extra named Azure OpenAI accounts for per-agent routing, with optional automatic RBAC | `{}` |
| `agentOverrides` | Per-agent deployment/endpoint/temperature/prompt overrides | `{}` |
| `publicNetworkAccess` | Whether the web app is reachable directly over the public internet | `Disabled` |
| `openaiApiVersion` | Default Azure OpenAI API version | `2025-01-01-preview` |

> **`publicNetworkAccess`**: the default (`Disabled`) requires the VNet/private
> networking this template provisions — you'll need a VPN, private endpoint, or
> similar to reach the app. For a simpler standalone public demo, set it to
> `Enabled` in `main.bicepparam` (weaker isolation — understand the tradeoff
> before using this in production).

> **Note**: Bicep replaces the web app's *entire* App Settings collection on
> every deployment. Don't hand-edit settings in the Azure Portal expecting them
> to persist — add them as Bicep parameters (or `agentOverrides`/
> `additionalOpenAiAccounts` entries) instead.

## 5. Grant RBAC

The Bicep deployment grants everything the Managed Identity needs on the
*infrastructure's own* resource group and the primary/additional OpenAI
accounts automatically. You still need to grant subscription-scoped roles for
each subscription the app should monitor (this is deliberately separate from
the infra deployment, since it's a different, often more sensitive, blast
radius):

```bash
PRINCIPAL_ID=$(az deployment group show \
  --resource-group <YOUR_RG> --name <deployment-name> \
  --query "properties.outputs.managedIdentityPrincipalId.value" -o tsv)

for ROLE in \
  "acdd72a7-3385-48ef-bd42-f606fba81ae7" \
  "73c42c96-874c-492b-b04d-ab87d138a893" \
  "43d0d8ad-25c7-4714-9337-8ba259a9fe05"; do
  az role assignment create --assignee "$PRINCIPAL_ID" \
    --role "$ROLE" --scope "/subscriptions/<SUBSCRIPTION_ID>"
done
```

(Reader, Log Analytics Reader, Monitoring Reader — see
`docs/rbac-implementation-guide.md` and `infra/modules/subscription-rbac.bicep`
for a reusable per-subscription Bicep module, and `infra/modules/openai-rbac.bicep`
for the OpenAI-account grant used automatically by `main.bicep`.)

## 6. Deploy the app code

```bash
az webapp deploy \
  --resource-group <YOUR_RG> \
  --name <webapp-name-from-bicep-output> \
  --src-path app.zip \
  --type zip
```

Or wire this into `pipelines/azure-pipelines.yml` for CI/CD.

## 7. Verify

```bash
curl https://<your-app>.azurewebsites.net/api/health
```

Returns configuration readiness — profile, per-agent deployment names, and
booleans for what's configured — **never** raw endpoint URLs, subscription
IDs, or other secrets:

```json
{
  "status": "ok",
  "version": "1.1.0",
  "profile": "oge",
  "agents": { "orchestrator": { "name": "Pipeline", "deployment": "foundry-gpt", "endpoint_configured": true, "supports_temperature": false }, "...": "..." },
  "config": { "openai_primary_endpoint_configured": true, "subscription_configured": true, "...": "..." }
}
```

This checks *configuration presence*, not live dependency health — it doesn't
call Azure OpenAI, Key Vault, etc. Use the Live scan (`/api/scan/overview`) or
the Ops Council chat to confirm agents can actually reach Azure OpenAI.

## 8. Customize (any time)

| Want to... | Where |
|---|---|
| Change agent prompts | `profiles/<id>/prompts/<agent_key>.txt` |
| Change agent names/roles/models | `profiles/<id>/profile.json` |
| Rebrand (logo, title, taglines) | `profiles/<id>/profile.json` `"brand"` block, `static/<your-logo>` |
| Route one agent to a different model/endpoint | `AGENT_<KEY>_DEPLOYMENT` / `AGENT_<KEY>_ENDPOINT` env vars, or `agentOverrides` in `main.bicepparam` |
| Switch profiles | `APP_PROFILE` env var / `appProfile` Bicep param |

See [BRANDING.md](BRANDING.md) for the full reference, including every
supported per-agent field and environment variable.

## 9. Upgrade

If you forked this repo for a customer, pull upstream changes as a normal git
merge/rebase. Your customizations live in `profiles/<your-id>/` (a new
directory) and `.env`/`infra/main.bicepparam` (git-ignored, untouched by
upstream), so they won't conflict with upstream changes to `app/`, `infra/`,
or the `oge`/`generic` profiles. Re-run `python3 scripts/configure.py` after an
upgrade if new configuration fields were added (it's safe to re-run — pass
`--force` to regenerate the local files, or edit them by hand).

---

## Environment Variables Reference

Set these on the App Service (via Bicep — see step 4) or in `.env` for local
dev. `scripts/configure.py` generates both from your answers; `.env.example`
documents every variable inline.

| Variable | Required | Description |
|----------|----------|--------------|
| `APP_PROFILE` | No (default `oge`) | Checked-in profile directory (`profiles/<id>/`) to load |
| `AZURE_OPENAI_ENDPOINT` | Yes | Primary Azure OpenAI endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT` | No (default `foundry-gpt`) | Default deployment name used when an agent doesn't specify its own |
| `AZURE_OPENAI_API_VERSION` | No (default `2025-01-01-preview`) | Default API version for chat completions calls |
| `AZURE_OPENAI_ENDPOINT_SECONDARY` | If using 2+ accounts | Secondary endpoint; referenced as `endpoint_ref: "secondary"` or `AGENT_<KEY>_ENDPOINT=secondary` |
| `AZURE_OPENAI_ENDPOINT_<NAME>` | No | Any additional named endpoint (e.g. `_TERTIARY`), referenced the same way |
| `AGENT_<KEY>_NAME` / `_ROLE` / `_DEPLOYMENT` / `_ENDPOINT` / `_TEMPERATURE` / `_SUPPORTS_TEMPERATURE` / `_API_VERSION` / `_PROMPT_FILE` | No | Per-agent overrides. `KEY` is one of `ORCHESTRATOR`, `COST_SENTINEL`, `STANDARDS_ARCHITECT`, `DIAGNOSTICS_SRE`, `SCOUT`, `COMPLIANCE_INSPECTOR` |
| `AZURE_CLIENT_ID` | Yes (Azure) | Managed Identity client ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Target subscription to monitor |
| `KEY_VAULT_URI` | Yes | Key Vault URI (e.g., `https://{prefix}-kv.vault.azure.net/`) |
| `LOG_ANALYTICS_WORKSPACE_ID` | Yes | Log Analytics workspace customer ID |
| `ADO_ORG_URL` / `ADO_PROJECT` / `ADO_REPO` / `ADO_PAT` | Optional | Azure DevOps integration (Phase 2 proposals). `ADO_PAT` is a secret — never commit it |

## Model Selection Guide

The deployment names are generic — map them to whichever models your Foundry account has:

| Deployment | Recommended Models | Notes |
|-----------|-------------------|-------|
| `foundry-gpt` | GPT-4o, GPT-4.1, GPT-5.x | Needs strong synthesis and broad knowledge |
| `foundry-reasoning` | o3, o3-mini, o4-mini | Needs multi-step reasoning (cost analysis, diagnostics) |
| `foundry-nano` | GPT-4o-mini, GPT-5-nano | Speed over depth — scanning and alerting |

By default, no agent sends a custom `temperature` (some reasoning/GPT-5-style
deployments reject anything but the default). If your deployment supports a
custom temperature, set `AGENT_<KEY>_SUPPORTS_TEMPERATURE=true` (env var) or
`supports_temperature: true` (`profiles/<id>/profile.json`).

## Cost Estimate

| Resource | Estimated Monthly Cost |
|----------|----------------------|
| App Service (P0v3) | ~$60/mo |
| Key Vault | ~$1/mo |
| Log Analytics (5GB/day) | ~$12/mo |
| Application Insights | Included with Log Analytics |
| Azure OpenAI tokens | ~$5-20/mo (depends on usage) |
| **Total** | **~$80-100/mo** |
