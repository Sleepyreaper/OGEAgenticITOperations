// ──────────────────────────────────────────────
// Network — VNet, Subnets, NSGs for Web App +
// Private Endpoints (KV, OpenAI cross-region)
// ──────────────────────────────────────────────

@description('Azure region for all network resources.')
param location string

@description('Naming prefix for all resources.')
param prefix string

resource nsgWebApp 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-nsg-webapp'
  location: location
  properties: {
    securityRules: [
      {
        name: 'Allow-HTTPS-Inbound-From-AzureLoadBalancer'
        properties: {
          description: 'Allow HTTPS health probes/traffic from Azure Load Balancer.'
          priority: 100
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'Deny-All-Inbound'
        properties: {
          description: 'Explicitly deny all inbound traffic.'
          priority: 4096
          protocol: '*'
          access: 'Deny'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      {
        name: 'Allow-VNet-Outbound'
        properties: {
          description: 'Allow outbound traffic within virtual network.'
          priority: 100
          protocol: '*'
          access: 'Allow'
          direction: 'Outbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      {
        name: 'Allow-AzureCloud-HTTPS-Outbound'
        properties: {
          description: 'Allow outbound HTTPS to Azure platform services.'
          priority: 200
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Outbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureCloud'
          destinationPortRange: '443'
        }
      }
      {
        name: 'Deny-Internet-Outbound'
        properties: {
          description: 'Explicitly deny outbound traffic to Internet.'
          priority: 300
          protocol: '*'
          access: 'Deny'
          direction: 'Outbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nsgPe 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-nsg-pe'
  location: location
  properties: {
    securityRules: [
      {
        name: 'Allow-VNet-Inbound'
        properties: {
          description: 'Allow inbound traffic from within virtual network for private endpoint flows.'
          priority: 100
          protocol: '*'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      {
        name: 'Deny-All-Inbound'
        properties: {
          description: 'Explicitly deny all other inbound traffic.'
          priority: 4096
          protocol: '*'
          access: 'Deny'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      {
        name: 'Allow-VNet-Outbound'
        properties: {
          description: 'Allow outbound traffic to virtual network only.'
          priority: 100
          protocol: '*'
          access: 'Allow'
          direction: 'Outbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      {
        name: 'Deny-Internet-Outbound'
        properties: {
          description: 'Explicitly deny outbound traffic to Internet.'
          priority: 300
          protocol: '*'
          access: 'Deny'
          direction: 'Outbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '*'
        }
      }
    ]
  }
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