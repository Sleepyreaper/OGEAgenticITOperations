// ──────────────────────────────────────────────
// RBAC for an EXISTING Azure AI Foundry account
// Used when main.bicep `foundryMode = 'existing'`. Grants the web app's
// managed identity Foundry User (formerly "Azure AI User") on the account
// so it can publish agent versions into the project and call the
// Responses API. Deployed within the RG that owns the account.
// ──────────────────────────────────────────────

param foundryAccountName string
param managedIdentityPrincipalId string

var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, managedIdentityPrincipalId, foundryUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}
