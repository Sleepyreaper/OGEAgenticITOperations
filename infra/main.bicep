// ═══════════════════════════════════════════════════
// OGE Envisioning — Ops Agent Infrastructure
// ═══════════════════════════════════════════════════
// Region       : West US 2
// OpenAI       : Reuses existing Azure OpenAI account
// App Service  : Reuses existing nextgen-webapp-plan (P0v3)
// Model        : o4-mini (o4MiniAgent deployment)
// ═══════════════════════════════════════════════════

targetScope = 'resourceGroup'

@description('Prefix for new resource names.')
param prefix string = 'ogeops'

@description('Azure region for new resources.')
param location string = 'westus2'

@description('Resource ID of the existing App Service Plan to share.')
param existingAppServicePlanId string

@description('Name of the existing Azure OpenAI account.')
param openaiAccountName string

@description('Resource group containing the existing Azure OpenAI account.')
param openaiResourceGroup string

@description('Endpoint of the existing Azure OpenAI account.')
param openaiEndpoint string

@description('Name of the o4-mini model deployment.')
param openaiDeploymentName string = 'o4MiniAgent'

@description('Object ID of the deploying user (for Key Vault admin). Leave empty to skip.')
param deployerPrincipalId string = ''

// ── Network ──
module network 'modules/network.bicep' = {
  name: 'network'
  params: { location: location, prefix: prefix }
}

// ── Managed Identity ──
module identity 'modules/managed-identity.bicep' = {
  name: 'managed-identity'
  params: { location: location, prefix: prefix }
}

// ── Monitoring ──
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: { location: location, prefix: prefix }
}

// ── Key Vault ──
module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    prefix: prefix
    vnetId: network.outputs.vnetId
    vnetName: network.outputs.vnetName
    peSubnetId: network.outputs.peSubnetId
    webAppSubnetId: network.outputs.webAppSubnetId
    managedIdentityPrincipalId: identity.outputs.identityPrincipalId
    deployerPrincipalId: deployerPrincipalId
  }
}

// ── OpenAI RBAC (cross-RG: grant MI access to existing account) ──
module openaiRbac 'modules/openai-rbac.bicep' = {
  name: 'openai-rbac'
  scope: resourceGroup(openaiResourceGroup)
  params: {
    openaiAccountName: openaiAccountName
    managedIdentityPrincipalId: identity.outputs.identityPrincipalId
  }
}

// ── Web App ──
module webApp 'modules/web-app.bicep' = {
  name: 'web-app'
  params: {
    location: location
    prefix: prefix
    webAppSubnetId: network.outputs.webAppSubnetId
    existingAppServicePlanId: existingAppServicePlanId
    managedIdentityId: identity.outputs.identityId
    managedIdentityClientId: identity.outputs.identityClientId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    keyVaultUri: keyVault.outputs.keyVaultUri
    openaiEndpoint: openaiEndpoint
    openaiDeploymentName: openaiDeploymentName
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// ── Outputs ──
output webAppUrl string = webApp.outputs.webAppUrl
output webAppName string = webApp.outputs.webAppName
output keyVaultName string = keyVault.outputs.keyVaultName
output managedIdentityPrincipalId string = identity.outputs.identityPrincipalId
