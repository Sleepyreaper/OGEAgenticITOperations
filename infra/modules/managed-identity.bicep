// ──────────────────────────────────────────────
// User-Assigned Managed Identity
// ──────────────────────────────────────────────

param location string
param prefix string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
}

output identityId string = identity.id
output identityPrincipalId string = identity.properties.principalId
output identityClientId string = identity.properties.clientId
