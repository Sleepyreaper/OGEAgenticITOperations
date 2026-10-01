# Azure AI Foundry — Terraform

Terraform equivalent of `infra/modules/foundry.bicep` / `foundry-rbac.bicep`:
provisions (or reuses) the Azure AI Foundry project the runtime squad runs in
when the app is set to `AGENT_BACKEND=foundry`. See
[`docs/FOUNDRY_ARCHITECTURE.md`](../../../docs/FOUNDRY_ARCHITECTURE.md).

| `mode` | Creates |
|---|---|
| `existing` (default) | **Foundry User** role (formerly "Azure AI User") for each `app_principal_ids` entry on the existing account |
| `new` | AIServices account (Entra-only auth, project management enabled), project, `model_deployments`, the role assignment(s), optional diagnostics to Log Analytics |

## Usage

```bash
cd infra/terraform/foundry
cp terraform.tfvars.example terraform.tfvars   # git-ignored; fill in
terraform init
terraform plan -out foundry.tfplan
terraform apply -parallelism=1 foundry.tfplan  # deployments on one account must be serial
terraform output app_settings
```

Then set the outputs on the web app (or pass them to `infra/main.bicep` as
`agentBackend = 'foundry'` + `foundryProjectEndpoint`):

```bash
az webapp config appsettings set -g <APP_RG> -n <WEB_APP> --settings \
  AGENT_BACKEND=foundry \
  FOUNDRY_PROJECT_ENDPOINT="$(terraform output -raw project_endpoint)"
```

Map each specialist to a deployment that exists in the project with
`AGENT_<KEY>_DEPLOYMENT` (e.g. `AGENT_SCOUT_DEPLOYMENT=gpt-5.6-luna`), or
force one for all agents with `FOUNDRY_MODEL_DEPLOYMENT`.

## Notes

- Uses `azapi` for the account/project/deployments (full ARM API fidelity,
  `Microsoft.CognitiveServices@2025-06-01`) and `azurerm` for RBAC and
  diagnostics.
- State is local by default; add a `backend "azurerm" {}` block for team use.
- The app publishes its agents into the project at runtime (one versioned
  agent per specialist, prefix `FOUNDRY_AGENT_PREFIX`) — Terraform does not
  manage agent definitions, which are versioned from `profiles/*/prompts/`.
