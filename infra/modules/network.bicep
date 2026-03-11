// ──────────────────────────────────────────────
// Network — VNet, Subnets, NSGs for Web App +
// Private Endpoints (KV, OpenAI cross-region)
// ──────────────────────────────────────────────

param location string
param prefix string

resource nsgWebApp 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-nsg-webapp'
  location: location
  properties: { securityRules: [] }
}

resource nsgPe 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-nsg-pe'
  location: location
  properties: { securityRules: [] }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.0.0.0/16' ] }
    subnets: [
      {
        name: 'snet-webapp'
        properties: {
          addressPrefix: '10.0.1.0/24'
          networkSecurityGroup: { id: nsgWebApp.id }
          delegations: [
            {
              name: 'delegation-web'
              properties: { serviceName: 'Microsoft.Web/serverFarms' }
            }
          ]
          serviceEndpoints: [
            { service: 'Microsoft.Storage' }
            { service: 'Microsoft.KeyVault' }
          ]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: '10.0.2.0/24'
          networkSecurityGroup: { id: nsgPe.id }
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output webAppSubnetId string = vnet.properties.subnets[0].id
output peSubnetId string = vnet.properties.subnets[1].id
