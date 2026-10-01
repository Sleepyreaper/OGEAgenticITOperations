// ──────────────────────────────────────────────
// Azure AI Foundry — NEW account + project + model deployments
// Used when main.bicep `foundryMode = 'new'`. Creates:
//   * an AIServices account with project management enabled
//     (Entra ID only — local/key auth disabled),
//   * one Foundry project (the runtime squad's home: the app publishes
//     one versioned agent per specialist into it at runtime),
//   * the model deployments the agents reference,
//   * Foundry User (formerly "Azure AI User") for the web app's managed
//     identity on the account, so it can publish agent versions and call
//     the Responses API. See docs/FOUNDRY_ARCHITECTURE.md.
// ──────────────────────────────────────────────

param location string

@description('Globally unique AIServices account name; also used as the custom subdomain (<name>.services.ai.azure.com).')
param accountName string

@description('Foundry project name.')
param projectName string

@description('''
Model deployments to create. Each item:
{ name: string, model: string, version: string, sku: string (e.g. GlobalStandard), capacity: int }
''')
param modelDeployments array

@description('Principal ID of the web app\'s user-assigned managed identity.')
param managedIdentityPrincipalId string

@description('Public network access for the Foundry account.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

@description('Optional Log Analytics workspace for Foundry diagnostic logs (token/request audit). Empty skips diagnostics.')
param logAnalyticsWorkspaceResourceId string = ''

// Foundry User (formerly Azure AI User): Microsoft.CognitiveServices/* data actions.
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: publicNetworkAccess
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: projectName
    description: 'OGE agentic operations runtime squad (orchestrator + specialists).'
  }
}

// Deployments on one account must be created serially.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for d in modelDeployments: {
  parent: account
  name: d.name
  sku: {
    name: d.sku
    capacity: d.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: d.model
      version: d.version
    }
  }
}]

resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, managedIdentityPrincipalId, foundryUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceResourceId)) {
  name: 'foundry-to-law'
  scope: account
  properties: {
    workspaceId: logAnalyticsWorkspaceResourceId
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output accountId string = account.id
output projectName string = project.name
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
