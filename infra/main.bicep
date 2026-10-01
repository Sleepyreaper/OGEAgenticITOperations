// ═══════════════════════════════════════════════════
// Reusable multi-agent Azure operations platform — infrastructure
// ═══════════════════════════════════════════════════
// OpenAI       : Reuses one or more existing Azure OpenAI accounts
// App Service  : Reuses an existing App Service Plan
// Branding/agents are controlled by the `appProfile` + `agentOverrides`
// parameters below (see profiles/ and app/config.py). Defaults reproduce
// this template's recommended reference deployment: the checked-in
// "power" profile (a generic power-utility example) on a GPT-5.6
// Sol/Terra/Luna model mapping — see docs/MODEL_CONFIGURATION.md. Set
// `appProfile = 'oge'` to keep this template's original single-account,
// six-agent legacy/example branding instead.
// ═══════════════════════════════════════════════════

targetScope = 'resourceGroup'

@description('Prefix for new resource names.')
param prefix string = 'opscouncil'

@description('Azure region for new resources.')
param location string = 'westus2'

@description('Resource ID of the existing App Service Plan to share.')
param existingAppServicePlanId string

@description('Checked-in profile (profiles/<id>/) the app should load. Controls branding and default per-agent names/models.')
param appProfile string = 'power'

@description('Azure subscription the agents monitor. Surfaced to the app as AZURE_SUBSCRIPTION_ID. Defaults to the subscription being deployed into.')
param subscriptionId string = subscription().subscriptionId

@description('Name of the existing Azure OpenAI account.')
param openaiAccountName string

@description('Resource group containing the existing Azure OpenAI account.')
param openaiResourceGroup string

@description('Endpoint of the existing Azure OpenAI account.')
param openaiEndpoint string

@description('Name of the foundry-gpt model deployment.')
param openaiDeploymentName string = 'foundry-gpt'

@description('Default Azure OpenAI API version used for chat completions calls. Individual agents may override this via agentOverrides.')
param openaiApiVersion string = '2025-01-01-preview'

@description('''
Optional additional Azure OpenAI accounts for per-agent endpoint routing
beyond the primary account above. Object key = logical endpoint name (e.g.
"secondary"), surfaced to the app as AZURE_OPENAI_ENDPOINT_<NAME>. Each
value is { endpoint: string, accountName: string, resourceGroup: string }.
Leave accountName/resourceGroup empty on an entry to skip automatic RBAC
assignment for that account (e.g. if it was already granted).
Example: { secondary: { endpoint: 'https://acct2.openai.azure.com/', accountName: 'acct2', resourceGroup: 'rg-acct2' } }
''')
param additionalOpenAiAccounts object = {}

@description('''
Optional per-agent configuration overrides, keyed by agent key (orchestrator,
cost_sentinel, standards_architect, diagnostics_sre, scout,
compliance_inspector — matching the Python agent keys exactly). Each value
may set any subset of: deployment, endpoint, temperature, supportsTemperature,
apiVersion, name, role, promptFile, maxCompletionTokens, maxContextChars,
responseInstruction, inputCostPerMillion, outputCostPerMillion,
promptVersion, supportsStructuredOutput (see docs/MODEL_CONFIGURATION.md
for what the cost/token/instruction fields control, and
docs/AGENT_INTELLIGENCE.md for promptVersion/supportsStructuredOutput).
Omitted fields fall back to the loaded profile's defaults. `endpoint` may
be "primary", "secondary", any other key present in
additionalOpenAiAccounts, or a literal https:// URL.
Example: { cost_sentinel: { deployment: 'foundry-reasoning', endpoint: 'secondary' } }
''')
param agentOverrides object = {}

@description('OpenTelemetry service.name reported to Application Insights. Empty (default) falls back to a profile-safe "ops-council-<appProfile>" at app startup — see docs/TELEMETRY.md. Only takes effect when Application Insights is provisioned (always true for this template).')
param otelServiceName string = ''

@description('A version fingerprint for "this profile\'s agent definitions as currently loaded" (see docs/AGENT_INTELLIGENCE.md), surfaced on /api/health and every /api/operations/analyze|briefing response. Empty (default) derives one automatically at app startup; set this only for a human-chosen version tag.')
param agentDefinitionVersion string = ''

@description('Which model backend app/agents/analysis.py uses (see docs/FOUNDRY_ARCHITECTURE.md). "direct" (default) calls Azure OpenAI directly. "foundry" runs every specialist as a versioned Azure AI Foundry agent in the project configured by foundryMode below; it fails loudly at call time (never silently falls back to "direct") if no Foundry project endpoint is configured.')
@allowed(['direct', 'foundry'])
param agentBackend string = 'direct'

@description('''
Azure AI Foundry provisioning (see docs/FOUNDRY_ARCHITECTURE.md):
  "none"     (default) — no Foundry resources or RBAC; FOUNDRY_PROJECT_ENDPOINT is still set if foundryProjectEndpoint is given.
  "existing" — reuse an existing Foundry account/project: grants the app's managed identity Foundry User on foundryAccountName in foundryResourceGroup. Set foundryProjectEndpoint.
  "new"      — create an AIServices account (foundryAccountName), a project (foundryProjectName) and foundryModelDeployments in this resource group, plus the RBAC.
''')
@allowed(['none', 'existing', 'new'])
param foundryMode string = 'none'

@description('Foundry (AIServices) account name. Required for foundryMode existing/new; for "new" it must be globally unique (used as the custom subdomain).')
param foundryAccountName string = ''

@description('Resource group of the existing Foundry account (foundryMode "existing"). Defaults to this resource group.')
param foundryResourceGroup string = resourceGroup().name

@description('Foundry project name (foundryMode "new").')
param foundryProjectName string = 'oge-ops'

@description('Region for a new Foundry account (foundryMode "new"). Pick a region with quota for the requested models.')
param foundryLocation string = location

@description('Project endpoint for foundryMode "existing"/"none", e.g. https://<account>.services.ai.azure.com/api/projects/<project>. Ignored for "new" (derived).')
param foundryProjectEndpoint string = ''

@description('Model deployments created for foundryMode "new". Each: { name, model, version, sku, capacity }. Map agents onto them with agentOverrides.<key>.deployment.')
param foundryModelDeployments array = [
  { name: 'gpt-5.6-sol', model: 'gpt-5.6-sol', version: '2026-07-09', sku: 'GlobalStandard', capacity: 100 }
  { name: 'gpt-5.6-terra', model: 'gpt-5.6-terra', version: '2026-07-09', sku: 'GlobalStandard', capacity: 50 }
  { name: 'gpt-5.6-luna', model: 'gpt-5.6-luna', version: '2026-07-09', sku: 'GlobalStandard', capacity: 50 }
]

@description('''
Optional Foundry runtime settings (surfaced as FOUNDRY_* / ANALYSIS_* app settings; see .env.example).
Recognized keys: agentPrefix, modelDeployment, enableTools, maxToolRounds, maxToolOutputChars, maxParallelSpecialists.
Example: { agentPrefix: 'oge-ops', maxToolRounds: 4, maxParallelSpecialists: 4 }
''')
param foundrySettings object = {}

@description('''
Optional ZeroOps / Azure SRE Agent settings (see docs/ZEROOPS_SRE_AGENT.md). Recognized keys, all optional:
mcpApiKeySecretName (name of a Key Vault secret in this template's vault holding the /mcp API key --
the endpoint returns 503 until it is set), chaosEnabled (true only for demo environments),
subscriptionId, demoResourceGroup, demoNsg, demoWebapp, demoPlan, demoStorage, demoKeyvault,
demoAutomation (outputs of infra/zeroops-demo/), sreAgentAauPriceUsd (default 0.10 -- check the
Azure Retail Prices API for your region), sreAgentModel (default gpt-5.2), sreAgentEndpoint (the
agent's data-plane agentEndpoint; enables fast-detection hand-off threads -- grant the app identity
SRE Agent Standard User on the agent), sreAgentSubagent (default zeroops-triage).
Example: { mcpApiKeySecretName: 'mcp-api-key' }
''')
param zeroopsSettings object = {}

@description('''
Optional operations evidence layer settings (app/operations/, see
docs/EVIDENCE_MODEL.md, docs/AZURE_DATA_SOURCES.md) — an object param (rather than dozens of flat
params) so it stays maintainable as the evidence layer grows. Recognized
keys, all optional: alertLookbackHours, changeLookbackHours,
changeCorrelationWindowMinutes, capacityWarningPct, capacityCriticalPct,
capacityLocations, openAiCapacityNameFilters, sloDefinitionsPath, sloDefinitionsJson (Phase 1); enableDefenderAlerts,
enableDefenderAssessments, costBudgetWarningPct, costBudgetCriticalPct,
costTrendLookbackDays, costTrendGrowthPctThreshold,
enableCostManagementBudget, enableCostManagementTrend,
backupLookbackHours, backupStaleRecoveryPointDays, enableBackup,
patchAssessmentStaleDays, enableUpdateManager, keyVaultExpiryWarningDays,
keyVaultMonitorUris, keyVaultMaxItemsPerType, enableKeyVaultExpiry,
automationLookbackHours, automationAccountIds, enableAutomation,
telemetryMonitoredResourceTypes, telemetryCriticalResourceIds,
telemetryMaxResources, telemetryHeartbeatLookbackHours,
enableTelemetryCoverage, retirementWarningDays,
enableRetirementAdvisories (Phase 2); operationsSnapshotCacheTtlSeconds,
operationsCollectionMaxWorkers, operationsStateDbPath (product API -- snapshot/brief/queue/workflow-state,
see docs/OPERATIONS_API.md). List-valued keys
(capacityLocations, openAiCapacityNameFilters, keyVaultMonitorUris, automationAccountIds,
telemetryMonitoredResourceTypes, telemetryCriticalResourceIds) take a
single comma-separated string, not a Bicep array. Omitted keys fall back to the
safe defaults documented in .env.example. Leaving this empty entirely
(the default) is a valid, safe production state — in particular, the
SLO collector reports an explicit not_configured state rather than a
fabricated uptime number until sloDefinitionsPath/sloDefinitionsJson is
set, and every Phase 2 source with an empty required list (e.g. no
keyVaultMonitorUris) reports not_configured rather than silently
skipping itself -- the same applies to capacityLocations (capacity
reports not_configured with no auto-discovered regions); an unset/empty
openAiCapacityNameFilters means no filtering at all -- every Cognitive
Services/Azure OpenAI quota is kept, never just Compute quotas, which
this setting never touches. Set at most
one of sloDefinitionsPath/sloDefinitionsJson.
operationsStateDbPath should point under /home (e.g.
/home/data/operations.db) so finding workflow state survives app
restarts/scale events on Azure App Service Linux.
operationsCollectionMaxWorkers bounds how many independent evidence
sources run_collection()/run_full_collection() collects concurrently
per request (app/operations/service.py) -- default 6, hard-capped at
12 regardless of this value (an out-of-range value raises at app
startup, same as every other numeric setting here).
Example: { capacityWarningPct: 80, capacityCriticalPct: 95 }
''')
param operationsSettings object = {}

@description('Whether the web app is reachable directly over the public internet. "Disabled" (default) requires access via the VNet/private networking this template provisions. Set to "Enabled" for a simpler standalone public demo deployment (weaker isolation — see DEPLOYMENT.md).')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

@description('Object ID of the deploying user (for Key Vault admin). Leave empty to skip.')
param deployerPrincipalId string = ''

// ── Network ──
module network 'modules/network.bicep' = {
  name: 'network'
  params: { location: location, prefix: prefix }
}

// ── Managed Identity ──
module identity 'modules/managed-identity.bicep' = {
  name: 'managed-identity'
  params: { location: location, prefix: prefix }
}

// ── Monitoring ──
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: { location: location, prefix: prefix }
}

// ── Key Vault ──
module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    prefix: prefix
    vnetId: network.outputs.vnetId
    vnetName: network.outputs.vnetName
    peSubnetId: network.outputs.peSubnetId
    webAppSubnetId: network.outputs.webAppSubnetId
    managedIdentityPrincipalId: identity.outputs.identityPrincipalId
    deployerPrincipalId: deployerPrincipalId
  }
}

// ── OpenAI RBAC (cross-RG: grant MI access to existing account) ──
module openaiRbac 'modules/openai-rbac.bicep' = {
  name: 'openai-rbac'
  scope: resourceGroup(openaiResourceGroup)
  params: {
    openaiAccountName: openaiAccountName
    managedIdentityPrincipalId: identity.outputs.identityPrincipalId
  }
}

// ── Additional OpenAI RBAC (optional secondary/tertiary accounts) ──
module additionalOpenaiRbac 'modules/openai-rbac.bicep' = [
  for item in items(additionalOpenAiAccounts): if (contains(item.value, 'accountName') && !empty(item.value.accountName)) {
    name: 'openai-rbac-${item.key}'
    scope: resourceGroup(item.value.resourceGroup)
    params: {
      openaiAccountName: item.value.accountName
      managedIdentityPrincipalId: identity.outputs.identityPrincipalId
    }
  }
]

// ── Azure AI Foundry (optional runtime squad backend) ──
module foundryNew 'modules/foundry.bicep' = if (foundryMode == 'new') {
  name: 'foundry'
  params: {
    location: foundryLocation
    accountName: foundryAccountName
    projectName: foundryProjectName
    modelDeployments: foundryModelDeployments
    managedIdentityPrincipalId: identity.outputs.identityPrincipalId
    logAnalyticsWorkspaceResourceId: monitoring.outputs.logAnalyticsId
  }
}

module foundryExistingRbac 'modules/foundry-rbac.bicep' = if (foundryMode == 'existing') {
  name: 'foundry-rbac'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    foundryAccountName: foundryAccountName
    managedIdentityPrincipalId: identity.outputs.identityPrincipalId
  }
}

var resolvedFoundryProjectEndpoint = foundryMode == 'new'
  ? 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
  : foundryProjectEndpoint

// ── Web App ──
module webApp 'modules/web-app.bicep' = {
  name: 'web-app'
  params: {
    location: location
    prefix: prefix
    webAppSubnetId: network.outputs.webAppSubnetId
    existingAppServicePlanId: existingAppServicePlanId
    managedIdentityId: identity.outputs.identityId
    managedIdentityClientId: identity.outputs.identityClientId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    keyVaultUri: keyVault.outputs.keyVaultUri
    openaiEndpoint: openaiEndpoint
    openaiDeploymentName: openaiDeploymentName
    openaiApiVersion: openaiApiVersion
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    subscriptionId: subscriptionId
    appProfile: appProfile
    additionalOpenAiAccounts: additionalOpenAiAccounts
    agentOverrides: agentOverrides
    publicNetworkAccess: publicNetworkAccess
    otelServiceName: otelServiceName
    operationsSettings: operationsSettings
    agentDefinitionVersion: agentDefinitionVersion
    agentBackend: agentBackend
    foundryProjectEndpoint: resolvedFoundryProjectEndpoint
    foundrySettings: foundrySettings
    zeroopsSettings: zeroopsSettings
  }
  dependsOn: [
    foundryNew
    foundryExistingRbac
  ]
}

// ── Outputs ──
output foundryProjectEndpoint string = resolvedFoundryProjectEndpoint
output webAppUrl string = webApp.outputs.webAppUrl
output webAppName string = webApp.outputs.webAppName
output keyVaultName string = keyVault.outputs.keyVaultName
output managedIdentityPrincipalId string = identity.outputs.identityPrincipalId
