// ═══════════════════════════════════════════════════
// OGE Envisioning — Parameters
// ═══════════════════════════════════════════════════
// Customer: Update these values for your environment.
// ═══════════════════════════════════════════════════

using './main.bicep'

param prefix = 'ogeops'
param location = 'westus2'

// Existing App Service Plan (P0v3 in West US 2)
param existingAppServicePlanId = '/subscriptions/b1672fa6-8e52-45d0-bf79-ceccc352177d/resourceGroups/NextGen_Agents/providers/Microsoft.Web/serverfarms/nextgen-webapp-plan'

// Existing Azure OpenAI account (nextgenagentfoundry in West US 3)
param openaiAccountName = 'nextgenagentfoundry'
param openaiResourceGroup = 'NextGen_Agents'

// Endpoint for the existing OpenAI account
param openaiEndpoint = 'https://nextgenagentfoundry.cognitiveservices.azure.com/'

// o4-mini deployment name
param openaiDeploymentName = 'o4MiniAgent'

// Deployer's Azure AD object ID — retrieve with: az ad signed-in-user show --query id -o tsv
param deployerPrincipalId = ''
