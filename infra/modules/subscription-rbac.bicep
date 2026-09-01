// ──────────────────────────────────────────────
// Subscription-Level RBAC for Managed Identity
// ──────────────────────────────────────────────
// Grants the Cloud Weather Ops Managed Identity the
// read-only roles it needs on each monitored
// subscription. Deploy once PER subscription.
//
// Usage:
//   az deployment sub create \
//     --location westus2 \
//     --template-file modules/subscription-rbac.bicep \
//     --parameters managedIdentityPrincipalId=<PRINCIPAL_ID>
// ──────────────────────────────────────────────

targetScope = 'subscription'

@description('Principal ID of the Cloud Weather Ops Managed Identity.')
param managedIdentityPrincipalId string

// ── Role Definition IDs (built-in) ──
var roles = [
  {
    name: 'Reader'
    id: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  }
  {
    name: 'Log-Analytics-Reader'
    id: '73c42c96-874c-492b-b04d-ab87d138a893'
  }
  {
    name: 'Monitoring-Reader'
    id: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
  }
]

// ── Assign each role at subscription scope ──
resource roleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in roles: {
    name: guid(subscription().id, managedIdentityPrincipalId, role.id)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role.id)
      principalId: managedIdentityPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]
