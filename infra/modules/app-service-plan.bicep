// ──────────────────────────────────────────────
// App Service Plan — Linux P0v3
// ──────────────────────────────────────────────

param location string
param prefix string

var planName = '${prefix}-plan'

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  kind: 'linux'
  sku: {
    name: 'P0v3'
    tier: 'PremiumV3'
  }
  properties: {
    reserved: true // Required for Linux
  }
}

output planId string = appServicePlan.id
output planName string = appServicePlan.name
