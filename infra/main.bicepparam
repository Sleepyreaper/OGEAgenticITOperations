// ═══════════════════════════════════════════════════
// OGE Ops Council — Parameters
// ═══════════════════════════════════════════════════
// Update these values for your environment before deploying.
// ═══════════════════════════════════════════════════

using './main.bicep'

param prefix = 'ogeops'
param location = 'westus2'

// Existing App Service Plan — provide the full resource ID
// Example: /subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.Web/serverfarms/<PLAN_NAME>
param existingAppServicePlanId = '<YOUR_APP_SERVICE_PLAN_RESOURCE_ID>'

// Existing Azure OpenAI account
param openaiAccountName = '<YOUR_OPENAI_ACCOUNT_NAME>'
param openaiResourceGroup = '<YOUR_OPENAI_RESOURCE_GROUP>'

// Endpoint for the existing OpenAI account
param openaiEndpoint = 'https://<YOUR_OPENAI_ACCOUNT_NAME>.openai.azure.com/'

// Model deployment name for the reasoning model (foundry-gpt recommended)
param openaiDeploymentName = '<YOUR_O4_MINI_DEPLOYMENT_NAME>'

// Deployer's Azure AD object ID — retrieve with: az ad signed-in-user show --query id -o tsv
param deployerPrincipalId = ''
