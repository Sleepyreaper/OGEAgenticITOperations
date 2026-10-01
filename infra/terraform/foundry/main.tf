# Azure AI Foundry for the OGE runtime squad — Terraform equivalent of
# infra/modules/foundry.bicep (mode = "new") and
# infra/modules/foundry-rbac.bicep (mode = "existing").
# See docs/FOUNDRY_ARCHITECTURE.md.

locals {
  is_new = var.mode == "new"
  # Foundry User (formerly "Azure AI User"): Microsoft.CognitiveServices/* data actions.
  foundry_user_role_id = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
  project_endpoint     = "https://${var.account_name}.services.ai.azure.com/api/projects/${var.project_name}"
  deployments          = local.is_new ? { for d in var.model_deployments : d.name => d } : {}
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

data "azurerm_cognitive_account" "existing" {
  count               = local.is_new ? 0 : 1
  name                = var.account_name
  resource_group_name = var.resource_group_name
}

resource "azapi_resource" "account" {
  count     = local.is_new ? 1 : 0
  type      = "Microsoft.CognitiveServices/accounts@2025-06-01"
  name      = var.account_name
  parent_id = data.azurerm_resource_group.rg.id
  location  = var.location

  identity {
    type = "SystemAssigned"
  }

  body = {
    kind = "AIServices"
    sku  = { name = "S0" }
    properties = {
      customSubDomainName    = var.account_name
      allowProjectManagement = true
      disableLocalAuth       = true
      publicNetworkAccess    = var.public_network_access
    }
  }
}

resource "azapi_resource" "project" {
  count     = local.is_new ? 1 : 0
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name      = var.project_name
  parent_id = azapi_resource.account[0].id
  location  = var.location

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      displayName = var.project_name
      description = "OGE agentic operations runtime squad (orchestrator + specialists)."
    }
  }
}

# Deployments on one account must be created serially — run the first
# `terraform apply` with -parallelism=1 (see README.md).
resource "azapi_resource" "deployment" {
  for_each  = local.deployments
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  name      = each.value.name
  parent_id = azapi_resource.account[0].id

  body = {
    sku = {
      name     = each.value.sku
      capacity = each.value.capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = each.value.model
        version = each.value.version
      }
    }
  }

  depends_on = [azapi_resource.project]
}

locals {
  account_id = local.is_new ? azapi_resource.account[0].id : data.azurerm_cognitive_account.existing[0].id
}

resource "azurerm_role_assignment" "foundry_user" {
  for_each           = toset(var.app_principal_ids)
  scope              = local.account_id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.foundry_user_role_id}"
  principal_id       = each.value
  principal_type     = "ServicePrincipal"
}

resource "azurerm_monitor_diagnostic_setting" "foundry" {
  count                      = local.is_new && var.log_analytics_workspace_id != "" ? 1 : 0
  name                       = "foundry-to-law"
  target_resource_id         = local.account_id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category_group = "allLogs"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
