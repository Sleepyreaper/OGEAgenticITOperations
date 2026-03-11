#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════
# OGE Envisioning — Infrastructure Deployment
# ═══════════════════════════════════════════════════
# Region       : West US 2
# OpenAI       : Reuses existing nextgenagentfoundry (West US 3)
# App Service  : Reuses existing nextgen-webapp-plan (P0v3)
# Model        : o4-mini (o4MiniAgent)
# ═══════════════════════════════════════════════════

RESOURCE_GROUP="${RESOURCE_GROUP:-OGE_Envisioning}"
LOCATION="${LOCATION:-westus2}"

echo "========================================="
echo " OGE Envisioning — Deploy Infrastructure"
echo "========================================="
echo "Resource Group : $RESOURCE_GROUP"
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
[ -n "$DEPLOYER_ID" ] && echo "Deployer ID    : $DEPLOYER_ID"

echo ""
read -p "Continue with deployment? (y/N): " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Create resource group ──
echo ""
echo "→ Creating resource group '$RESOURCE_GROUP' in '$LOCATION'..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# ── Deploy ──
DEPLOYMENT_NAME="ogeops-$(date +%Y%m%d-%H%M%S)"
echo "→ Deploying infrastructure (deployment: $DEPLOYMENT_NAME)..."
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters main.bicepparam \
  --parameters deployerPrincipalId="$DEPLOYER_ID" \
  --name "$DEPLOYMENT_NAME" \
  --output table

# ── Show outputs ──
echo ""
echo "→ Deployment outputs:"
az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --query "properties.outputs.{WebApp_URL:webAppUrl.value, WebApp_Name:webAppName.value, KeyVault:keyVaultName.value, MI_PrincipalId:managedIdentityPrincipalId.value}" \
  --output table

echo ""
echo "========================================="
echo " Deployment complete!"
echo "========================================="
