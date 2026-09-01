// ──────────────────────────────────────────────
// Web App — Python, on existing App Service Plan
// VNet-integrated, Managed Identity, KV references
// ──────────────────────────────────────────────

param location string
param prefix string
param webAppSubnetId string
param existingAppServicePlanId string
param managedIdentityId string
param managedIdentityClientId string
param appInsightsConnectionString string
param keyVaultUri string
param openaiEndpoint string
param openaiDeploymentName string
param logAnalyticsWorkspaceId string

@description('Default Azure OpenAI API version for chat completions calls.')
param openaiApiVersion string = '2025-01-01-preview'

@description('Azure subscription the agents monitor. Empty is allowed (falls back to whatever the app is configured with at runtime).')
param subscriptionId string = ''

@description('Checked-in profile (profiles/<id>/) the app should load.')
param appProfile string = 'oge'

@description('Additional Azure OpenAI accounts for per-agent endpoint routing (see main.bicep). Only .endpoint is used here, surfaced as AZURE_OPENAI_ENDPOINT_<NAME>.')
param additionalOpenAiAccounts object = {}

@description('Per-agent configuration overrides. See main.bicep for the full field list.')
param agentOverrides object = {}

@description('Whether the web app is reachable directly over the public internet.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

var webAppName = '${prefix}-app-${take(uniqueString(resourceGroup().id), 6)}'

// Base application settings — always present, unchanged from prior releases
// (plus the AZURE_SUBSCRIPTION_ID/APP_PROFILE/AZURE_OPENAI_API_VERSION
// settings this template previously omitted).
var baseAppSettings = [
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'AZURE_CLIENT_ID', value: managedIdentityClientId }
  { name: 'KEY_VAULT_URI', value: keyVaultUri }
  { name: 'AZURE_OPENAI_ENDPOINT', value: openaiEndpoint }
  { name: 'AZURE_OPENAI_DEPLOYMENT', value: openaiDeploymentName }
  { name: 'AZURE_OPENAI_API_VERSION', value: openaiApiVersion }
  { name: 'LOG_ANALYTICS_WORKSPACE_ID', value: logAnalyticsWorkspaceId }
  { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
  { name: 'APP_PROFILE', value: appProfile }
  { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
]

// Named endpoint overrides, e.g. key "secondary" -> AZURE_OPENAI_ENDPOINT_SECONDARY.
var namedEndpointSettings = [for item in items(additionalOpenAiAccounts): {
  name: 'AZURE_OPENAI_ENDPOINT_${toUpper(item.key)}'
  value: item.value.endpoint
}]

// Per-agent override fields -> AGENT_<KEY>_<FIELD> app setting names. Using
// an object parameter here (rather than one Bicep parameter per agent per
// field) keeps this maintainable as agent count/fields grow.
var agentOverrideFieldSuffixes = {
  name: 'NAME'
  role: 'ROLE'
  deployment: 'DEPLOYMENT'
  endpoint: 'ENDPOINT'
  temperature: 'TEMPERATURE'
  supportsTemperature: 'SUPPORTS_TEMPERATURE'
  apiVersion: 'API_VERSION'
  promptFile: 'PROMPT_FILE'
}

// Nested for-loops aren't supported directly for variable construction, so
// this uses the map() lambda (per-agent -> per-field) plus flatten() to
// produce one flat array of app settings.
var agentOverrideSettingsRaw = flatten(map(items(agentOverrides), agentItem => map(items(agentOverrideFieldSuffixes), fieldItem => {
  name: 'AGENT_${toUpper(agentItem.key)}_${fieldItem.value}'
  value: contains(agentItem.value, fieldItem.key) ? string(agentItem.value[fieldItem.key]) : ''
})))
// Skip fields the caller didn't set, so they fall back to the profile's
// own defaults instead of being overridden with an empty string.
var agentOverrideSettings = filter(agentOverrideSettingsRaw, setting => !empty(setting.value))

var appSettings = concat(baseAppSettings, namedEndpointSettings, agentOverrideSettings)

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: existingAppServicePlanId
    virtualNetworkSubnetId: webAppSubnetId
    publicNetworkAccess: publicNetworkAccess
    httpsOnly: true
    keyVaultReferenceIdentity: managedIdentityId
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      vnetRouteAllEnabled: true
      alwaysOn: true
      appSettings: appSettings
    }
  }
}

output webAppName string = webApp.name
output webAppHostName string = webApp.properties.defaultHostName
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
