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

var webAppName = '${prefix}-app-${take(uniqueString(resourceGroup().id), 6)}'

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
    publicNetworkAccess: 'Disabled'
    httpsOnly: true
    keyVaultReferenceIdentity: managedIdentityId
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      vnetRouteAllEnabled: true
      alwaysOn: true
      appSettings: [
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        { name: 'AZURE_CLIENT_ID', value: managedIdentityClientId }
        { name: 'KEY_VAULT_URI', value: keyVaultUri }
        { name: 'AZURE_OPENAI_ENDPOINT', value: openaiEndpoint }
        { name: 'AZURE_OPENAI_DEPLOYMENT', value: openaiDeploymentName }
        { name: 'LOG_ANALYTICS_WORKSPACE_ID', value: logAnalyticsWorkspaceId }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
      ]
    }
  }
}

output webAppName string = webApp.name
output webAppHostName string = webApp.properties.defaultHostName
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'