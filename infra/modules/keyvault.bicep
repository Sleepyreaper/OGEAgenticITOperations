// ──────────────────────────────────────────────
// Key Vault — RBAC, Private Endpoint, DNS
// All secrets live here. No public access.
// ──────────────────────────────────────────────

@description('Azure region for all regional resources.')
param location string

@description('Naming prefix for resources.')
param prefix string

@description('Resource ID of the virtual network for private DNS linking.')
param vnetId string

@description('Name of the virtual network for private DNS link naming.')
param vnetName string

@description('Resource ID of the subnet used for the Key Vault private endpoint.')
param peSubnetId string

@description('Resource ID of the subnet allowed in Key Vault network ACLs.')
param webAppSubnetId string

@description('Principal ID of the managed identity that needs Key Vault Secrets User access.')
param managedIdentityPrincipalId string

@description('Optional principal ID of a deployer user to grant Key Vault Administrator.')
param deployerPrincipalId string = ''

var kvName = '${prefix}-kv-${take(uniqueString(resourceGroup().id), 6)}'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      virtualNetworkRules: [
        { id: webAppSubnetId }
      ]
    }
  }
}

// Managed Identity → Key Vault Secrets User
resource kvSecretUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, managedIdentityPrincipalId, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Deployer → Key Vault Administrator (optional)
resource kvAdminRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(keyVault.id, deployerPrincipalId, '00482a5a-887f-4fb3-b363-3b7fe8e74483')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '00482a5a-887f-4fb3-b363-3b7fe8e74483')
    principalId: deployerPrincipalId
    principalType: 'User'
  }
}

// Private DNS Zone
resource dnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
}

resource dnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZone
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnetId }
    registrationEnabled: false
  }
}

// Private Endpoint
resource pe 'Microsoft.Network/privateEndpoints@2023-09-01' = {
  name: '${kvName}-pe'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: '${kvName}-plsc'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: [ 'vault' ]
        }
      }
    ]
  }
}

resource dnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-09-01' = {
  parent: pe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: { privateDnsZoneId: dnsZone.id }
      }
    ]
  }
}

output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri