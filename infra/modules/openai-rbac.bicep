// ──────────────────────────────────────────────
// RBAC for existing Azure OpenAI account
// Grants the managed identity Cognitive Services
// OpenAI User on the existing account.
// Deployed within the RG that owns the account.
// ──────────────────────────────────────────────

param openaiAccountName string
param managedIdentityPrincipalId string

resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: openaiAccountName
}

// Managed Identity → Cognitive Services OpenAI User
resource openaiUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openai.id, managedIdentityPrincipalId, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: openai
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}
