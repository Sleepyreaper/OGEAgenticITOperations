// ZeroOps demo environment: small, cheap, real Azure resources that the
// ZeroOps scenarios (app/zeroops/scenarios.py) break and repair on purpose.
// Deploy into a dedicated resource group (recommended) or alongside the ops
// app: alerts and RBAC are scoped to the demo resources only, never the RG.
//
//   az group create -n <rg> -l <location>
//   az deployment group create -g <rg> -f infra/zeroops-demo/main.bicep \
//     -p opsAppPrincipalId=<principalId of the OGE app identity>
//
// Every resource is tagged zeroops-demo=true. Alert rule names start with
// "ZeroOps <scenario-id>" so the SRE Agent response plan (titleContains
// "ZeroOps") routes them to the zeroops-triage agent.
// Approximate run cost: ~US$15-20/month (B1 plan + pay-as-you-go telemetry).
targetScope = 'resourceGroup'

@description('Azure region for the demo resources.')
param location string = resourceGroup().location

@description('Short prefix for resource names (3-8 lowercase letters/digits).')
@minLength(3)
@maxLength(8)
param prefix string = 'zeroops'

@description('Principal (object) ID of the OGE app managed identity. Gets Contributor on each demo resource including the vault (not the resource group) and Key Vault Secrets Officer on the demo vault so it can inject/clean up scenarios. Leave empty to assign roles yourself.')
param opsAppPrincipalId string = ''

@description('Principal IDs (object IDs) of the Azure SRE Agent identities. Each gets Reader + Monitoring Reader on this resource group (add Contributor yourself if you want SRE-solo fixes to run after approval).')
param sreAgentPrincipalIds array = []

@description('Create an App Insights standard availability test against the demo web app (drives the bad-deploy alert).')
param enableAvailabilityTest bool = true

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = { 'zeroops-demo': 'true' }
var names = {
  law: '${prefix}-log-${suffix}'
  appi: '${prefix}-appi-${suffix}'
  plan: '${prefix}-plan-${suffix}'
  web: '${prefix}-web-${suffix}'
  nsg: '${prefix}-nsg-${suffix}'
  storage: take('${prefix}st${suffix}', 24)
  kv: take('${prefix}-kv-${suffix}', 24)
  automation: '${prefix}-aa-${suffix}'
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: names.law
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: names.appi
  location: location
  kind: 'web'
  tags: tags
  properties: { Application_Type: 'web', WorkspaceResourceId: law.id }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: names.plan
  location: location
  kind: 'linux'
  tags: tags
  sku: { name: 'B1', tier: 'Basic', capacity: 1 }
  properties: { reserved: true }
}

resource web 'Microsoft.Web/sites@2023-12-01' = {
  name: names.web
  location: location
  kind: 'app,linux'
  tags: tags
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: ''
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      alwaysOn: true
      appSettings: [
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
      ]
    }
  }
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: names.nsg
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-https-from-internet'
        properties: {
          priority: 200
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
    ]
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: names.storage
  location: location
  kind: 'StorageV2'
  tags: tags
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: names.kv
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource automation 'Microsoft.Automation/automationAccounts@2023-11-01' = {
  name: names.automation
  location: location
  tags: tags
  properties: { sku: { name: 'Basic' }, publicNetworkAccess: true }
}

// ── Role assignments ──
var roles = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  monitoringReader: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
  kvSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

resource opsContributorWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(web.id, opsAppPrincipalId, roles.contributor)
  scope: web
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource opsContributorPlan 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(plan.id, opsAppPrincipalId, roles.contributor)
  scope: plan
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource opsContributorNsg 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(nsg.id, opsAppPrincipalId, roles.contributor)
  scope: nsg
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource opsContributorStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(storage.id, opsAppPrincipalId, roles.contributor)
  scope: storage
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource opsContributorAutomation 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(automation.id, opsAppPrincipalId, roles.contributor)
  scope: automation
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

// Contributor on the vault lets the app manage the demo secret through ARM (control plane), which keeps
// working when Azure Policy disables the vault's public network access.
resource opsContributorKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(kv.id, opsAppPrincipalId, roles.contributor)
  scope: kv
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
  }
}

resource opsKvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(opsAppPrincipalId)) {
  name: guid(kv.id, opsAppPrincipalId, roles.kvSecretsOfficer)
  scope: kv
  properties: {
    principalId: opsAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsOfficer)
  }
}

resource sreReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in sreAgentPrincipalIds: {
  name: guid(resourceGroup().id, pid, roles.reader)
  properties: {
    principalId: pid
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.reader)
  }
}]

resource sreMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in sreAgentPrincipalIds: {
  name: guid(resourceGroup().id, pid, roles.monitoringReader)
  properties: {
    principalId: pid
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringReader)
  }
}]

// ── Scenario alerts (names start with "ZeroOps <scenario-id>") ──
// Activity Log alerts fire on the successful write that injects each scenario,
// filtered to the single demo resource (so other resources in the RG never trigger them).
// Cleanup writes may fire them again; the response plan merges repeats into the
// open incident thread (mergeEnabled), where the agent confirms the revert.
var activityAlerts = [
  {
    name: 'ZeroOps open-door - NSG inbound rule changed'
    operation: 'Microsoft.Network/networkSecurityGroups/securityRules/write'
    scope: nsg.id
    description: 'A security rule was written on the demo NSG. Check for Internet-exposed management ports.'
  }
  {
    name: 'ZeroOps bad-deploy - web app config changed'
    operation: 'Microsoft.Web/sites/config/write'
    scope: web.id
    description: 'Demo web app configuration changed. Correlate with availability/5xx.'
  }
  {
    name: 'ZeroOps storm-surge - App Service plan scaled'
    operation: 'Microsoft.Web/serverfarms/write'
    scope: plan.id
    description: 'Demo App Service plan SKU/instance count changed. Cost vs. performance decision.'
  }
  {
    name: 'ZeroOps rogue-hotfix - storage security config changed'
    operation: 'Microsoft.Storage/storageAccounts/write'
    scope: storage.id
    description: 'Demo storage account configuration changed. Check public access and TLS.'
  }
]

resource activityAlert 'Microsoft.Insights/activityLogAlerts@2020-10-01' = [for a in activityAlerts: {
  name: a.name
  location: 'global'
  tags: tags
  properties: {
    enabled: true
    description: a.description
    scopes: [a.scope]
    condition: {
      allOf: [
        { field: 'category', equals: 'Administrative' }
        { field: 'operationName', equals: a.operation }
        { field: 'status', equals: 'Succeeded' }
      ]
    }
    actions: { actionGroups: [] }
  }
}]

resource certJobFailedAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'ZeroOps 2am-cert - certificate renewal runbook failed'
  location: 'global'
  tags: tags
  properties: {
    description: 'An Automation job failed in the demo account. The TLS bundle in the demo vault expires soon.'
    severity: 1
    enabled: true
    scopes: [automation.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'failed-jobs'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.Automation/automationAccounts'
          metricName: 'TotalJob'
          dimensions: [{ name: 'Status', operator: 'Include', values: ['Failed'] }]
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
        }
      ]
    }
    actions: []
  }
}

resource availabilityTest 'Microsoft.Insights/webtests@2022-06-15' = if (enableAvailabilityTest) {
  name: '${prefix}-avail-${suffix}'
  location: location
  kind: 'standard'
  tags: union(tags, { 'hidden-link:${appi.id}': 'Resource' })
  properties: {
    SyntheticMonitorId: '${prefix}-avail-${suffix}'
    Name: 'ZeroOps demo web availability'
    Enabled: true
    Frequency: 300
    Timeout: 30
    Kind: 'standard'
    RetryEnabled: true
    Locations: [
      { Id: 'us-va-ash-azr' }
      { Id: 'us-ca-sjc-azr' }
      { Id: 'emea-nl-ams-azr' }
    ]
    Request: { RequestUrl: 'https://${web.properties.defaultHostName}/', HttpVerb: 'GET' }
    ValidationRules: { ExpectedHttpStatusCode: 200, SSLCheck: true, SSLCertRemainingLifetimeCheck: 7 }
  }
}

resource availabilityAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = if (enableAvailabilityTest) {
  name: 'ZeroOps bad-deploy - web app availability dropped'
  location: 'global'
  tags: tags
  properties: {
    description: 'Demo web app is failing its availability test.'
    severity: 1
    enabled: true
    scopes: [availabilityTest.id, appi.id]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.WebtestLocationAvailabilityCriteria'
      webTestId: availabilityTest.id
      componentId: appi.id
      failedLocationCount: 2
    }
    actions: []
  }
}

output resourceGroup string = resourceGroup().name
output webAppName string = web.name
output webAppUrl string = 'https://${web.properties.defaultHostName}'
output planName string = plan.name
output nsgName string = nsg.name
output storageName string = storage.name
output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
output automationName string = automation.name
output automationId string = automation.id
output appSettings object = {
  ZEROOPS_CHAOS_ENABLED: 'true'
  ZEROOPS_SUBSCRIPTION_ID: subscription().subscriptionId
  ZEROOPS_DEMO_RESOURCE_GROUP: resourceGroup().name
  ZEROOPS_DEMO_NSG: nsg.name
  ZEROOPS_DEMO_WEBAPP: web.name
  ZEROOPS_DEMO_PLAN: plan.name
  ZEROOPS_DEMO_STORAGE: storage.name
  ZEROOPS_DEMO_KEYVAULT: kv.name
  ZEROOPS_DEMO_AUTOMATION: automation.name
  KEY_VAULT_MONITOR_URIS: kv.properties.vaultUri
  AUTOMATION_ACCOUNT_IDS: automation.id
}
