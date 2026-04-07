#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════
# Ops Council — Infrastructure Deployment
# ═══════════════════════════════════════════════════
# Region       : West US 2
# OpenAI       : Reuses existing Azure OpenAI account
# App Service  : Reuses existing <YOUR_APP_SERVICE_PLAN> (P0v3)
# Model        : foundry-gpt (foundry-gpt)
# ═══════════════════════════════════════════════════

RESOURCE_GROUP="${RESOURCE_GROUP:-{PREFIX}_RG}"
LOCATION="${LOCATION:-westus2}"
SENSITIVE_LOGGING="${SENSITIVE_LOGGING:-false}" # set to "true" to allow full resource name logging

redact() {
  local value="${1:-}"
  if [[ -z "$value" ]]; then
    echo "<none>"
    return
  fi
  if [[ "$SENSITIVE_LOGGING" == "true" ]]; then
    echo "$value"
    return
  fi
  local len=${#value}
  if (( len <= 4 )); then
    echo "****"
  else
    echo "${value:0:2}***${value: -2}"
  fi
}

echo "========================================="
echo " Ops Council — Deploy Infrastructure"
echo "========================================="
echo "Resource Group : $(redact "$RESOURCE_GROUP")"
echo "Location       : $LOCATION"
echo ""

# ── Pre-flight checks ──
command -v az >/dev/null 2>&1 || { echo "ERROR: Azure CLI not found."; exit 1; }

echo "→ Checking Azure CLI login..."
az account show --query "{Subscription:name, SubscriptionId:id}" -o table 2>/dev/null || {
  echo "ERROR: Not logged in. Run 'az login' first."
  exit 1
}

DEPLOYER_ID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || echo "")
[ -n "$DEPLOYER_ID" ] && echo "Deployer ID    : $(redact "$DEPLOYER_ID")"

echo ""
read -r -p "Continue with deployment? (y/N): " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Create resource group ──
echo ""
echo "→ Creating resource group in selected location..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# ── Deploy ──
DEPLOYMENT_NAME="{prefix}-$(date +%Y%m%d-%H%M%S)"
echo "→ Deploying infrastructure (deployment id: $(redact "$DEPLOYMENT_NAME"))..."
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters main.bicepparam \
  --parameters deployerPrincipalId="$DEPLOYER_ID" \
  --name "$DEPLOYMENT_NAME" \
  --output none

# ── Show outputs (redacted) ──
echo ""
echo "→ Deployment outputs (redacted):"
WEBAPP_URL=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --query "properties.outputs.webAppUrl.value" \
  -o tsv 2>/dev/null || true)

WEBAPP_NAME=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --query "properties.outputs.webAppName.value" \
  -o tsv 2>/dev/null || true)

KEYVAULT_NAME=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --query "properties.outputs.keyVaultName.value" \
  -o tsv 2>/dev/null || true)

MI_PRINCIPAL_ID=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --query "properties.outputs.managedIdentityPrincipalId.value" \
  -o tsv 2>/dev/null || true)

echo "WebApp_URL      : $(redact "$WEBAPP_URL")"
echo "WebApp_Name     : $(redact "$WEBAPP_NAME")"
echo "KeyVault        : $(redact "$KEYVAULT_NAME")"
echo "MI_PrincipalId  : $(redact "$MI_PRINCIPAL_ID")"

echo ""
echo "========================================="
echo " Deployment complete!"
echo "========================================="