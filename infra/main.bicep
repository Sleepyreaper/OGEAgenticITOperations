// ═══════════════════════════════════════════════════
// Reusable multi-agent Azure operations platform — infrastructure
// ═══════════════════════════════════════════════════
// OpenAI       : Reuses one or more existing Azure OpenAI accounts
// App Service  : Reuses an existing App Service Plan
// Branding/agents are controlled by the `appProfile` + `agentOverrides`
// parameters below (see profiles/ and app/config.py). Defaults reproduce
// this template's original single-account, six-agent "oge" behavior.
// ═══════════════════════════════════════════════════

targetScope = 'resourceGroup'

@description('Prefix for new resource names.')
param prefix string = 'opscouncil'

@description('Azure region for new resources.')
param location string = 'westus2'

@description('Resource ID of the existing App Service Plan to share.')
param existingAppServicePlanId string

@description('Checked-in profile (profiles/<id>/) the app should load. Controls branding and default per-agent names/models.')
param appProfile string = 'oge'

@description('Azure subscription the agents monitor. Surfaced to the app as AZURE_SUBSCRIPTION_ID. Defaults to the subscription being deployed into.')
param subscriptionId string = subscription().subscriptionId

@description('Name of the existing Azure OpenAI account.')
param openaiAccountName string

@description('Resource group containing the existing Azure OpenAI account.')
param openaiResourceGroup string

@description('Endpoint of the existing Azure OpenAI account.')
param openaiEndpoint string

@description('Name of the foundry-gpt model deployment.')
param openaiDeploymentName string = 'foundry-gpt'

@description('Default Azure OpenAI API version used for chat completions calls. Individual agents may override this via agentOverrides.')
param openaiApiVersion string = '2025-01-01-preview'

@description('''
Optional additional Azure OpenAI accounts for per-agent endpoint routing
beyond the primary account above. Object key = logical endpoint name (e.g.
"secondary"), surfaced to the app as AZURE_OPENAI_ENDPOINT_<NAME>. Each
value is { endpoint: string, accountName: string, resourceGroup: string }.
Leave accountName/resourceGroup empty on an entry to skip automatic RBAC
assignment for that account (e.g. if it was already granted).
Example: { secondary: { endpoint: 'https://acct2.openai.azure.com/', accountName: 'acct2', resourceGroup: 'rg-acct2' } }
''')
param additionalOpenAiAccounts object = {}

@description('''
Optional per-agent configuration overrides, keyed by agent key (orchestrator,
cost_sentinel, standards_architect, diagnostics_sre, scout,
compliance_inspector — matching the Python agent keys exactly). Each value
may set any subset of: deployment, endpoint, temperature, supportsTemperature,
apiVersion, name, role, promptFile. Omitted fields fall back to the loaded
profile's defaults. `endpoint` may be "primary", "secondary", any other
key present in additionalOpenAiAccounts, or a literal https:// URL.
Example: { cost_sentinel: { deployment: 'foundry-reasoning', endpoint: 'secondary' } }
''')
param agentOverrides object = {}

@description('Whether the web app is reachable directly over the public internet. "Disabled" (default) requires access via the VNet/private networking this template provisions. Set to "Enabled" for a simpler standalone public demo deployment (weaker isolation — see DEPLOYMENT.md).')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

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

// ── Additional OpenAI RBAC (optional secondary/tertiary accounts) ──
module additionalOpenaiRbac 'modules/openai-rbac.bicep' = [
  for item in items(additionalOpenAiAccounts): if (contains(item.value, 'accountName') && !empty(item.value.accountName)) {
    name: 'openai-rbac-${item.key}'
    scope: resourceGroup(item.value.resourceGroup)
    params: {
      openaiAccountName: item.value.accountName
      managedIdentityPrincipalId: identity.outputs.identityPrincipalId
    }
  }
]

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
    openaiApiVersion: openaiApiVersion
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    subscriptionId: subscriptionId
    appProfile: appProfile
    additionalOpenAiAccounts: additionalOpenAiAccounts
    agentOverrides: agentOverrides
    publicNetworkAccess: publicNetworkAccess
  }
}

// ── Outputs ──
output webAppUrl string = webApp.outputs.webAppUrl
output webAppName string = webApp.outputs.webAppName
output keyVaultName string = keyVault.outputs.keyVaultName
output managedIdentityPrincipalId string = identity.outputs.identityPrincipalId
