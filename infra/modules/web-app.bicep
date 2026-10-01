// ──────────────────────────────────────────────
// Web App — Python, on existing App Service Plan
// VNet-integrated, Managed Identity, KV references
// ──────────────────────────────────────────────

param location string
param prefix string
param webAppSubnetId string
param existingAppServicePlanId string
param managedIdentityId string
param managedIdentityClientId string
param appInsightsConnectionString string
param keyVaultUri string
param openaiEndpoint string
param openaiDeploymentName string
param logAnalyticsWorkspaceId string

@description('Default Azure OpenAI API version for chat completions calls.')
param openaiApiVersion string = '2025-01-01-preview'

@description('Azure subscription the agents monitor. Empty is allowed (falls back to whatever the app is configured with at runtime).')
param subscriptionId string = ''

@description('Checked-in profile (profiles/<id>/) the app should load.')
param appProfile string = 'power'

@description('OpenTelemetry service.name. Empty (default) falls back to "ops-council-<appProfile>" at app startup — a short, profile-safe default that never leaks a customer brand string unless you set this explicitly. Only used when appInsightsConnectionString/APPLICATIONINSIGHTS_CONNECTION_STRING is non-empty; see docs/TELEMETRY.md.')
param otelServiceName string = ''

@description('A version fingerprint for the loaded profile\'s agent definitions (see docs/AGENT_INTELLIGENCE.md). Empty (default) derives one automatically at app startup.')
param agentDefinitionVersion string = ''

@description('Which model backend app/agents/analysis.py uses. "direct" (default) calls Azure OpenAI directly; "foundry" runs specialists as Azure AI Foundry agents (see docs/FOUNDRY_ARCHITECTURE.md) and fails loudly if FOUNDRY_PROJECT_ENDPOINT is empty.')
@allowed(['direct', 'foundry'])
param agentBackend string = 'direct'

@description('Azure AI Foundry project endpoint (FOUNDRY_PROJECT_ENDPOINT). Empty omits the setting.')
param foundryProjectEndpoint string = ''

@description('Foundry runtime settings (see main.bicep): agentPrefix, modelDeployment, enableTools, maxToolRounds, maxToolOutputChars, maxParallelSpecialists.')
param foundrySettings object = {}

@description('ZeroOps / Azure SRE Agent settings (see main.bicep): mcpApiKeySecretName, chaosEnabled, subscriptionId, demoResourceGroup, demoNsg, demoWebapp, demoPlan, demoStorage, demoKeyvault, demoAutomation, sreAgentAauPriceUsd, sreAgentModel.')
param zeroopsSettings object = {}

@description('Additional Azure OpenAI accounts for per-agent endpoint routing (see main.bicep). Only .endpoint is used here, surfaced as AZURE_OPENAI_ENDPOINT_<NAME>.')
param additionalOpenAiAccounts object = {}

@description('Per-agent configuration overrides. See main.bicep for the full field list.')
param agentOverrides object = {}

@description('Operations evidence layer settings (see main.bicep). Optional subset of: alertLookbackHours, changeLookbackHours, changeCorrelationWindowMinutes, capacityWarningPct, capacityCriticalPct, capacityLocations, openAiCapacityNameFilters, sloDefinitionsPath, sloDefinitionsJson, enableDefenderAlerts, enableDefenderAssessments, costBudgetWarningPct, costBudgetCriticalPct, costTrendLookbackDays, costTrendGrowthPctThreshold, enableCostManagementBudget, enableCostManagementTrend, backupLookbackHours, backupStaleRecoveryPointDays, enableBackup, patchAssessmentStaleDays, enableUpdateManager, keyVaultExpiryWarningDays, keyVaultMonitorUris, keyVaultMaxItemsPerType, enableKeyVaultExpiry, automationLookbackHours, automationAccountIds, enableAutomation, telemetryMonitoredResourceTypes, telemetryCriticalResourceIds, telemetryMaxResources, telemetryHeartbeatLookbackHours, enableTelemetryCoverage, retirementWarningDays, enableRetirementAdvisories, operationsSnapshotCacheTtlSeconds, operationsCollectionMaxWorkers, operationsStateDbPath (product API -- see docs/OPERATIONS_API.md). List-valued keys (capacityLocations, openAiCapacityNameFilters, keyVaultMonitorUris, automationAccountIds, telemetryMonitoredResourceTypes, telemetryCriticalResourceIds) take a comma-separated string.')
param operationsSettings object = {}

@description('Whether the web app is reachable directly over the public internet.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

var webAppName = '${prefix}-app-${take(uniqueString(resourceGroup().id), 6)}'

// Base application settings — always present, unchanged from prior releases
// (plus the AZURE_SUBSCRIPTION_ID/APP_PROFILE/AZURE_OPENAI_API_VERSION
// settings this template previously omitted).
var baseAppSettings = [
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'OTEL_SERVICE_NAME', value: otelServiceName }
  { name: 'AZURE_CLIENT_ID', value: managedIdentityClientId }
  { name: 'KEY_VAULT_URI', value: keyVaultUri }
  { name: 'AZURE_OPENAI_ENDPOINT', value: openaiEndpoint }
  { name: 'AZURE_OPENAI_DEPLOYMENT', value: openaiDeploymentName }
  { name: 'AZURE_OPENAI_API_VERSION', value: openaiApiVersion }
  { name: 'LOG_ANALYTICS_WORKSPACE_ID', value: logAnalyticsWorkspaceId }
  { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
  { name: 'APP_PROFILE', value: appProfile }
  { name: 'AGENT_DEFINITION_VERSION', value: agentDefinitionVersion }
  { name: 'AGENT_BACKEND', value: agentBackend }
  { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
]

// Named endpoint overrides, e.g. key "secondary" -> AZURE_OPENAI_ENDPOINT_SECONDARY.
var namedEndpointSettings = [for item in items(additionalOpenAiAccounts): {
  name: 'AZURE_OPENAI_ENDPOINT_${toUpper(item.key)}'
  value: item.value.endpoint
}]

// Per-agent override fields -> AGENT_<KEY>_<FIELD> app setting names. Using
// an object parameter here (rather than one Bicep parameter per agent per
// field) keeps this maintainable as agent count/fields grow.
var agentOverrideFieldSuffixes = {
  name: 'NAME'
  role: 'ROLE'
  deployment: 'DEPLOYMENT'
  endpoint: 'ENDPOINT'
  temperature: 'TEMPERATURE'
  supportsTemperature: 'SUPPORTS_TEMPERATURE'
  apiVersion: 'API_VERSION'
  promptFile: 'PROMPT_FILE'
  maxCompletionTokens: 'MAX_COMPLETION_TOKENS'
  maxContextChars: 'MAX_CONTEXT_CHARS'
  responseInstruction: 'RESPONSE_INSTRUCTION'
  inputCostPerMillion: 'INPUT_COST_PER_MILLION'
  outputCostPerMillion: 'OUTPUT_COST_PER_MILLION'
  promptVersion: 'PROMPT_VERSION'
  supportsStructuredOutput: 'SUPPORTS_STRUCTURED_OUTPUT'
}

// Nested for-loops aren't supported directly for variable construction, so
// this uses the map() lambda (per-agent -> per-field) plus flatten() to
// produce one flat array of app settings.
var agentOverrideSettingsRaw = flatten(map(items(agentOverrides), agentItem => map(items(agentOverrideFieldSuffixes), fieldItem => {
  name: 'AGENT_${toUpper(agentItem.key)}_${fieldItem.value}'
  value: contains(agentItem.value, fieldItem.key) ? string(agentItem.value[fieldItem.key]) : ''
})))
// Skip fields the caller didn't set, so they fall back to the profile's
// own defaults instead of being overridden with an empty string.
var agentOverrideSettings = filter(agentOverrideSettingsRaw, setting => !empty(setting.value))

// Operations evidence layer settings -> env var names (see
// app/operations/config.py). One object param instead of dozens of flat
// params, same convention as agentOverrides above. List-valued settings
// (capacityLocations, openAiCapacityNameFilters, keyVaultMonitorUris, automationAccountIds,
// telemetryMonitoredResourceTypes, telemetryCriticalResourceIds) take a
// single comma-separated STRING value here, matching the env var format
// app/operations/config.py's _parse_csv_list/_parse_capacity_locations
// expect -- not a Bicep array.
var operationsSettingFieldNames = {
  alertLookbackHours: 'ALERT_LOOKBACK_HOURS'
  changeLookbackHours: 'CHANGE_LOOKBACK_HOURS'
  changeCorrelationWindowMinutes: 'CHANGE_CORRELATION_WINDOW_MINUTES'
  capacityWarningPct: 'CAPACITY_WARNING_PCT'
  capacityCriticalPct: 'CAPACITY_CRITICAL_PCT'
  capacityLocations: 'CAPACITY_LOCATIONS'
  openAiCapacityNameFilters: 'OPENAI_CAPACITY_NAME_FILTERS'
  sloDefinitionsPath: 'SLO_DEFINITIONS_PATH'
  sloDefinitionsJson: 'SLO_DEFINITIONS_JSON'
  enableDefenderAlerts: 'ENABLE_DEFENDER_ALERTS'
  enableDefenderAssessments: 'ENABLE_DEFENDER_ASSESSMENTS'
  costBudgetWarningPct: 'COST_BUDGET_WARNING_PCT'
  costBudgetCriticalPct: 'COST_BUDGET_CRITICAL_PCT'
  costTrendLookbackDays: 'COST_TREND_LOOKBACK_DAYS'
  costTrendGrowthPctThreshold: 'COST_TREND_GROWTH_PCT_THRESHOLD'
  enableCostManagementBudget: 'ENABLE_COST_MANAGEMENT_BUDGET'
  enableCostManagementTrend: 'ENABLE_COST_MANAGEMENT_TREND'
  backupLookbackHours: 'BACKUP_LOOKBACK_HOURS'
  backupStaleRecoveryPointDays: 'BACKUP_STALE_RECOVERY_POINT_DAYS'
  enableBackup: 'ENABLE_BACKUP'
  patchAssessmentStaleDays: 'PATCH_ASSESSMENT_STALE_DAYS'
  enableUpdateManager: 'ENABLE_UPDATE_MANAGER'
  keyVaultExpiryWarningDays: 'KEY_VAULT_EXPIRY_WARNING_DAYS'
  keyVaultMonitorUris: 'KEY_VAULT_MONITOR_URIS'
  keyVaultMaxItemsPerType: 'KEY_VAULT_MAX_ITEMS_PER_TYPE'
  enableKeyVaultExpiry: 'ENABLE_KEY_VAULT_EXPIRY'
  automationLookbackHours: 'AUTOMATION_LOOKBACK_HOURS'
  automationAccountIds: 'AUTOMATION_ACCOUNT_IDS'
  enableAutomation: 'ENABLE_AUTOMATION'
  telemetryMonitoredResourceTypes: 'TELEMETRY_MONITORED_RESOURCE_TYPES'
  telemetryCriticalResourceIds: 'TELEMETRY_CRITICAL_RESOURCE_IDS'
  telemetryMaxResources: 'TELEMETRY_MAX_RESOURCES'
  telemetryHeartbeatLookbackHours: 'TELEMETRY_HEARTBEAT_LOOKBACK_HOURS'
  enableTelemetryCoverage: 'ENABLE_TELEMETRY_COVERAGE'
  retirementWarningDays: 'RETIREMENT_WARNING_DAYS'
  enableRetirementAdvisories: 'ENABLE_RETIREMENT_ADVISORIES'
  operationsSnapshotCacheTtlSeconds: 'OPERATIONS_SNAPSHOT_CACHE_TTL_SECONDS'
  operationsCollectionMaxWorkers: 'OPERATIONS_COLLECTION_MAX_WORKERS'
  operationsStateDbPath: 'OPERATIONS_STATE_DB'
}

var operationsSettingsRaw = [for item in items(operationsSettingFieldNames): {
  name: item.value
  value: contains(operationsSettings, item.key) ? string(operationsSettings[item.key]) : ''
}]
// Skip fields the caller didn't set, so they fall back to the safe
// defaults in app/operations/config.py instead of an empty override.
var operationsAppSettings = filter(operationsSettingsRaw, setting => !empty(setting.value))

// Foundry runtime settings -> env var names (see app/agents/backend.py
// FoundryConfig.from_env and app/agents/analysis.py). Unset keys are
// skipped so the app's defaults apply.
var foundrySettingFieldNames = {
  agentPrefix: 'FOUNDRY_AGENT_PREFIX'
  modelDeployment: 'FOUNDRY_MODEL_DEPLOYMENT'
  enableTools: 'FOUNDRY_ENABLE_TOOLS'
  maxToolRounds: 'FOUNDRY_MAX_TOOL_ROUNDS'
  maxToolOutputChars: 'FOUNDRY_MAX_TOOL_OUTPUT_CHARS'
  maxParallelSpecialists: 'ANALYSIS_MAX_PARALLEL_SPECIALISTS'
}
var foundryTuningSettings = [for item in items(foundrySettingFieldNames): {
  name: item.value
  value: contains(foundrySettings, item.key) ? string(foundrySettings[item.key]) : ''
}]
var foundrySettingsRaw = concat([{ name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }], foundryTuningSettings)
var foundryAppSettings = filter(foundrySettingsRaw, setting => !empty(setting.value))

// ZeroOps settings -> env var names (see app/zeroops/). MCP_API_KEY is
// only ever a Key Vault reference to a secret the operator creates.
var zeroopsSettingFieldNames = {
  chaosEnabled: 'ZEROOPS_CHAOS_ENABLED'
  subscriptionId: 'ZEROOPS_SUBSCRIPTION_ID'
  demoResourceGroup: 'ZEROOPS_DEMO_RESOURCE_GROUP'
  demoNsg: 'ZEROOPS_DEMO_NSG'
  demoWebapp: 'ZEROOPS_DEMO_WEBAPP'
  demoPlan: 'ZEROOPS_DEMO_PLAN'
  demoStorage: 'ZEROOPS_DEMO_STORAGE'
  demoKeyvault: 'ZEROOPS_DEMO_KEYVAULT'
  demoAutomation: 'ZEROOPS_DEMO_AUTOMATION'
  sreAgentAauPriceUsd: 'SRE_AGENT_AAU_PRICE_USD'
  sreAgentModel: 'SRE_AGENT_MODEL'
}
var zeroopsTuningSettings = [for item in items(zeroopsSettingFieldNames): {
  name: item.value
  value: contains(zeroopsSettings, item.key) ? string(zeroopsSettings[item.key]) : ''
}]
var mcpApiKeySecretName = string(zeroopsSettings.?mcpApiKeySecretName ?? '')
var zeroopsSettingsRaw = concat(zeroopsTuningSettings, [{
  name: 'MCP_API_KEY'
  value: empty(mcpApiKeySecretName) ? '' : '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/${mcpApiKeySecretName})'
}])
var zeroopsAppSettings = filter(zeroopsSettingsRaw, setting => !empty(setting.value))

var appSettings = concat(baseAppSettings, namedEndpointSettings, agentOverrideSettings, operationsAppSettings, foundryAppSettings, zeroopsAppSettings)

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: existingAppServicePlanId
    virtualNetworkSubnetId: webAppSubnetId
    publicNetworkAccess: publicNetworkAccess
    httpsOnly: true
    keyVaultReferenceIdentity: managedIdentityId
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      vnetRouteAllEnabled: true
      alwaysOn: true
      appSettings: appSettings
    }
  }
}

output webAppName string = webApp.name
output webAppHostName string = webApp.properties.defaultHostName
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
