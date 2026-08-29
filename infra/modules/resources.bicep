// Resource-group-scoped resources for a microsoft.foundry service: the
// Foundry (AIServices) account, its project, model deployments, the optional
// container registry, and the developer role assignment.
//
// Deployed by main.bicep into a resource group it creates at subscription
// scope. Kept as a separate module so main.bicep can target the subscription
// (and thus create the resource group) while these resources stay RG-scoped.

targetScope = 'resourceGroup'

// User-defined types

@description('Shape of one model deployment entry in azure.yaml.')
type deploymentsType = deploymentType[]

@description('Shape of a single model deployment.')
type deploymentType = {
  name: string
  model: {
    name: string
    format: string
    version: string
  }
  sku: {
    name: string
    capacity: int
  }
}

// Parameters

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Azure region for Azure AI Search.')
param searchLocation string = location

@description('Azure region for the application VNet and Container Apps.')
param applicationLocation string = location

@description('Tags applied to all resources.')
param tags object = {}

@description('Optional salt to vary resource names across re-provisions.')
param resourceTokenSalt string = ''

@description('Optional Foundry project name override. Empty uses the deterministic shared resource token.')
@maxLength(32)
param foundryProjectName string = ''

@description('Optional Foundry account name override. Empty uses the deterministic shared resource token.')
@maxLength(64)
param foundryAccountName string = ''

@description('Model deployments to provision on the Foundry account.')
param deployments deploymentsType = []

@description('Name of the agentic guardrail (RAI policy) applied to agents and the shared Toolbox.')
param agenticGuardrailName string = 'research-agentic-guardrail'

@description('Include an Azure Container Registry. Set true when any agent uses docker:.')
param includeAcr bool = false

@description('Object id of the developer running azd. When set, grants project data-plane roles. Empty disables the role assignments so headless / CI runs do not fail.')
param principalId string = ''

@description('Principal type used in the developer role assignment.')
param principalType string = 'User'

@description('Publisher display name for API Management.')
param apimPublisherName string

@description('Publisher contact email for API Management.')
param apimPublisherEmail string

@description('Custom role that permits only governed APIM named-value updates.')
param apimNamedValueWriterRoleId string

@description('Entra ID tenant id used by Azure Container Apps built-in authentication (EasyAuth) to validate incoming bearer tokens. Required when enableEntraAuth is true.')
param entraTenantId string = ''

@description('Client (application) id of the Entra App Registration representing this API, used as the allowed token audience for Container Apps built-in authentication. Required when enableEntraAuth is true. Not created by this template -- see app/api.bicep.')
param entraApiClientId string = ''

@description('Enable Azure Container Apps built-in authentication (EasyAuth) on the API container app. Defaults to false; see app/api.bicep for the full trust-boundary rationale.')
param enableEntraAuth bool = false

// Variables

var resourceToken = empty(resourceTokenSalt)
  ? uniqueString(subscription().id, resourceGroup().id, location)
  : uniqueString(subscription().id, resourceGroup().id, location, resourceTokenSalt)
var applicationResourceToken = empty(resourceTokenSalt)
  ? uniqueString(subscription().id, resourceGroup().id, applicationLocation)
  : uniqueString(subscription().id, resourceGroup().id, applicationLocation, resourceTokenSalt)

var abbrs = loadJsonContent('../abbreviations.json')

var effectiveFoundryAccountName = empty(foundryAccountName)
  ? '${abbrs.cognitiveServicesAccounts}${resourceToken}'
  : foundryAccountName
var effectiveFoundryProjectName = empty(foundryProjectName)
  ? 'proj-${resourceToken}'
  : foundryProjectName
var apiManagementName = 'apim-${resourceToken}'
var embeddingDeployments = filter(
  deployments,
  deployment => deployment.model.name == 'text-embedding-3-large'
)
var embeddingDeploymentName = length(embeddingDeployments) == 1
  ? embeddingDeployments[0].name
  : ''
// The memory store needs a chat model alongside the embedding model.
var chatDeployments = filter(deployments, deployment => !startsWith(deployment.model.name, 'text-embedding'))
var chatDeploymentName = length(chatDeployments) > 0 ? chatDeployments[0].name : ''

// Built-in role definition ids. See: https://learn.microsoft.com/azure/role-based-access-control/built-in-roles
var cognitiveServicesUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a97b65f3-24c7-4388-baec-2e87135dc908'
)
var cognitiveServicesOpenAIUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)
var foundryUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)
var foundryProjectManagerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'eadc314b-1a2d-4efa-be10-5d325db5065e'
)
var monitoringMetricsPublisherRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '3913510d-42f4-4e42-8a64-420c390055eb'
)

// Resources

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: effectiveFoundryAccountName
  location: location
  tags: tags
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: effectiveFoundryAccountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: null
      virtualNetworkRules: []
      ipRules: []
    }
  }

  // Sequential model deployment creation; ARM throttles concurrent
  // deployments on the same account.
  @batchSize(1)
  resource modelDeployments 'deployments' = [
    for d in deployments: {
      name: d.name
      properties: {
        model: d.model
      }
      sku: d.sku
    }
  ]

  resource project 'projects' = {
    name: effectiveFoundryProjectName
    location: location
    identity: {
      type: 'SystemAssigned'
    }
    properties: {
      description: '${effectiveFoundryProjectName} Project'
      displayName: effectiveFoundryProjectName
    }
    // Explicit dependsOn ensures all model deployments complete before
    // the project is created; the project does not reference them so
    // there is no implicit dependency Bicep can infer.
    dependsOn: [
      modelDeployments
    ]
  }
}

// Agentic guardrail. PostToolCall indirect-attack screening is the enforcement
// point for the "retrieved content is untrusted" rule, because tool output is
// where cross-prompt injection reaches the model.
resource agenticGuardrail 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-05-01' = {
  parent: foundryAccount
  name: agenticGuardrailName
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Blocking'
    contentFilters: [
      { name: 'Hate', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Hate', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Sexual', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Sexual', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Violence', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Violence', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Selfharm', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Selfharm', enabled: true, blocking: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Jailbreak', enabled: true, blocking: true, source: 'Prompt' }
      { name: 'Indirect Attack', enabled: true, blocking: true, source: 'PostToolCall' }
      { name: 'Protected Material Text', enabled: true, blocking: true, source: 'Completion' }
      { name: 'Protected Material Code', enabled: true, blocking: true, source: 'Completion' }
    ]
  }
  dependsOn: [
    foundryAccount::project
  ]
}

module acr 'acr.bicep' = if (includeAcr) {
  name: 'acr'
  params: {
    location: location
    tags: tags
    name: '${abbrs.containerRegistryRegistries}${resourceToken}'
    foundryAccountName: foundryAccount.name
    foundryProjectName: foundryAccount::project.name
    foundryProjectPrincipalId: foundryAccount::project.identity.principalId
  }
}

module monitoring 'monitoring.bicep' = {
  name: 'monitoring'
  params: {
    name: 'log-${resourceToken}'
    location: location
    tags: tags
  }
}

module identities 'identity.bicep' = {
  name: 'workload-identities'
  params: {
    name: resourceToken
    location: location
    tags: tags
  }
}

module storage 'storage.bicep' = {
  name: 'research-storage'
  params: {
    name: 'st${resourceToken}'
    location: location
    tags: tags
    apiPrincipalId: identities.outputs.apiPrincipalId
    principalId: principalId
    principalType: principalType
  }
}

module search 'search.bicep' = {
  name: 'research-search'
  params: {
    name: 'srch-${resourceToken}'
    location: searchLocation
    tags: tags
    apiPrincipalId: identities.outputs.apiPrincipalId
    principalId: principalId
    principalType: principalType
  }
}

module cosmos 'cosmos.bicep' = {
  name: 'research-cosmos'
  params: {
    name: 'cosmos-${resourceToken}'
    location: location
    tags: tags
    apiPrincipalId: identities.outputs.apiPrincipalId
    principalId: principalId
  }
}

module privateNetwork 'app-private-network.bicep' = if (includeAcr) {
  name: 'app-private-network'
  params: {
    name: applicationResourceToken
    location: applicationLocation
    tags: tags
    storageAccountId: storage.outputs.accountId
    cosmosAccountId: cosmos.outputs.accountId
  }
}

module documentIntelligence 'document-intelligence.bicep' = {
  name: 'document-intelligence'
  params: {
    name: 'di-${resourceToken}'
    location: location
    tags: tags
    apiPrincipalId: identities.outputs.apiPrincipalId
  }
}

resource apiFoundryProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount::project.id, 'id-api-${resourceToken}', foundryProjectManagerRoleId)
  scope: foundryAccount::project
  properties: {
    principalId: identities.outputs.apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryProjectManagerRoleId
  }
}

module apiUserIdentityImpersonationRole 'foundry-user-identity-role.bicep' = {
  name: 'foundry-user-identity-${resourceToken}'
  scope: subscription()
  params: {
    roleName: 'Foundry Agent User Identity Impersonation - ${foundryAccount.name}'
    assignableScope: foundryAccount.id
  }
}

resource apiUserIdentityImpersonation 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    foundryAccount.id,
    'id-api-${resourceToken}',
    'foundry-agent-user-identity-impersonation'
  )
  scope: foundryAccount
  properties: {
    principalId: identities.outputs.apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: apiUserIdentityImpersonationRole.outputs.roleDefinitionId
  }
}

// The memory store calls the chat and embedding deployments as the project
// identity, so without this the store accepts writes but fails every search.
resource projectFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, foundryAccount::project.id, foundryUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: foundryAccount::project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryUserRoleId
  }
}

resource apiModelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, 'id-api-${resourceToken}', cognitiveServicesOpenAIUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: identities.outputs.apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cognitiveServicesOpenAIUserRoleId
  }
}

module containerAppsEnvironment 'container-apps-environment.bicep' = if (includeAcr) {
  name: 'research-container-apps-environment'
  params: {
    name: take(applicationResourceToken, 8)
    location: applicationLocation
    tags: tags
    logAnalyticsWorkspaceName: monitoring.outputs.workspaceName
    infrastructureSubnetId: privateNetwork!.outputs.containerAppsSubnetId
    apiIdentityPrincipalId: identities.outputs.apiPrincipalId
    webIdentityPrincipalId: identities.outputs.webPrincipalId
    acrResourceId: acr!.outputs.resourceId
  }
}

module apiManagement 'api-management.bicep' = if (includeAcr) {
  name: 'research-api-management'
  params: {
    name: apiManagementName
    location: location
    tags: tags
    publisherName: apimPublisherName
    publisherEmail: apimPublisherEmail
    apiPrincipalId: identities.outputs.apiPrincipalId
    namedValueWriterRoleDefinitionId: apimNamedValueWriterRoleId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
  }
}

resource appInsightsForRbac 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-log-${resourceToken}'
}

resource apiTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsightsForRbac.id, 'id-api-${resourceToken}', monitoringMetricsPublisherRoleId)
  scope: appInsightsForRbac
  properties: {
    principalId: identities.outputs.apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: monitoringMetricsPublisherRoleId
  }
  dependsOn: [
    monitoring
  ]
}

resource developerTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(appInsightsForRbac.id, principalId, monitoringMetricsPublisherRoleId)
  scope: appInsightsForRbac
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: monitoringMetricsPublisherRoleId
  }
  dependsOn: [
    monitoring
  ]
}

// Grant the developer Cognitive Services User on the project so they can call
// the Foundry data-plane (chat/completions, agents API) from their machine.
resource developerCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(foundryAccount::project.id, principalId, cognitiveServicesUserRoleId)
  scope: foundryAccount::project
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: cognitiveServicesUserRoleId
  }
}

resource developerFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(foundryAccount::project.id, principalId, foundryUserRoleId)
  scope: foundryAccount::project
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: foundryUserRoleId
  }
}

resource developerFoundryProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(foundryAccount::project.id, principalId, foundryProjectManagerRoleId)
  scope: foundryAccount::project
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: foundryProjectManagerRoleId
  }
}

resource developerUserIdentityImpersonation 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(
    foundryAccount.id,
    principalId,
    'foundry-agent-user-identity-impersonation'
  )
  scope: foundryAccount
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: apiUserIdentityImpersonationRole.outputs.roleDefinitionId
  }
}

// Outputs

output AZURE_AI_PROJECT_ID string = foundryAccount::project.id
output AZURE_AI_ACCOUNT_NAME string = foundryAccount.name
output AZURE_AI_PROJECT_NAME string = foundryAccount::project.name
output AZURE_OPENAI_ENDPOINT string = 'https://${foundryAccount.name}.openai.azure.com/'
output AZURE_AI_EMBEDDING_DEPLOYMENT_NAME string = embeddingDeploymentName
output AZURE_AI_CHAT_DEPLOYMENT_NAME string = chatDeploymentName
output FOUNDRY_PROJECT_ENDPOINT string = 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/${foundryAccount::project.name}'
output AZURE_TAGS string = base64(string(tags))
output AZURE_APPLICATION_LOCATION string = applicationLocation
output AZURE_APPLICATION_RESOURCE_TOKEN string = take(applicationResourceToken, 8)
output AZURE_CONTAINER_ENVIRONMENT_ID string = includeAcr ? containerAppsEnvironment!.outputs.environmentId : ''
output AZURE_CONTAINER_ENVIRONMENT_NAME string = includeAcr ? containerAppsEnvironment!.outputs.environmentName : ''
output AZURE_CONTAINER_ENVIRONMENT_DEFAULT_DOMAIN string = includeAcr ? containerAppsEnvironment!.outputs.defaultDomain : ''
output AZURE_CONTAINER_REGISTRY_NAME string = includeAcr ? acr!.outputs.name : ''
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = includeAcr ? acr!.outputs.loginServer : ''
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = includeAcr ? acr!.outputs.resourceId : ''
output AZURE_AI_PROJECT_ACR_CONNECTION_NAME string = includeAcr ? acr!.outputs.connectionName : ''
output AZURE_FOUNDRY_NETWORK_MODE string = 'none'
output AZURE_FOUNDRY_MANAGED_ISOLATION_MODE string = ''
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceId
output AZURE_MANAGED_IDENTITY_CLIENT_ID string = identities.outputs.apiClientId
output AZURE_MANAGED_IDENTITY_PRINCIPAL_ID string = identities.outputs.apiPrincipalId
output AZURE_MANAGED_IDENTITY_RESOURCE_ID string = identities.outputs.apiResourceId
output AZURE_WEB_MANAGED_IDENTITY_CLIENT_ID string = identities.outputs.webClientId
output AZURE_WEB_MANAGED_IDENTITY_PRINCIPAL_ID string = identities.outputs.webPrincipalId
output AZURE_WEB_MANAGED_IDENTITY_RESOURCE_ID string = identities.outputs.webResourceId
output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.accountName
output AZURE_STORAGE_BLOB_ENDPOINT string = storage.outputs.blobEndpoint
output AZURE_STORAGE_SOURCE_CONTAINER string = storage.outputs.sourcesContainer
output AZURE_STORAGE_ARTIFACT_CONTAINER string = storage.outputs.artifactsContainer
output AZURE_STORAGE_AGENT_STUDIO_BUNDLE_CONTAINER string = storage.outputs.agentStudioBundlesContainer
output AZURE_SEARCH_ENDPOINT string = search.outputs.endpoint
output AZURE_SEARCH_SERVICE_NAME string = search.outputs.name
output AZURE_SEARCH_INDEX_NAME string = search.outputs.indexName
output AZURE_SEARCH_INDEX_DATA_READER_ROLE_ID string = search.outputs.readerRoleDefinitionId
output AZURE_COSMOS_ENDPOINT string = cosmos.outputs.endpoint
output AZURE_COSMOS_DATABASE string = cosmos.outputs.databaseName
output AZURE_COSMOS_AGENT_STUDIO_DATABASE string = cosmos.outputs.agentStudioDatabaseName
output AZURE_COSMOS_AGENT_STUDIO_METADATA_CONTAINER string = cosmos.outputs.agentStudioMetadataContainerName
output AZURE_COSMOS_AGENT_STUDIO_MEMORY_CONTAINER string = cosmos.outputs.agentStudioMemoryContainerName
output AZURE_COSMOS_AGENT_STUDIO_AUDIT_CONTAINER string = cosmos.outputs.agentStudioAuditContainerName
output AZURE_COSMOS_AGENT_STUDIO_CATALOG_CONTAINER string = cosmos.outputs.agentStudioCatalogContainerName
output AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT string = documentIntelligence.outputs.endpoint
output RESEARCH_CONNECTOR_GATEWAY_URL string = includeAcr ? 'https://${apiManagementName}.azure-api.net/research-connectors' : ''
output RESEARCH_CONNECTOR_GATEWAY_TOKEN_SCOPE string = '${environment().resourceManager}.default'
output RESEARCH_WORKSPACE_TENANT_ID string = subscription().tenantId
output RESEARCH_WORKSPACE_PROJECT_ID string = foundryAccount::project.name
output RESEARCH_ENTRA_AUTH_ENFORCED string = string(enableEntraAuth)
output RESEARCH_ENTRA_TENANT_ID string = entraTenantId
output RESEARCH_ENTRA_API_CLIENT_ID string = entraApiClientId
output WEB_URL string = includeAcr ? containerAppsEnvironment!.outputs.webUrl : ''
output API_URL string = includeAcr ? containerAppsEnvironment!.outputs.apiUrl : ''
output API_NAME string = includeAcr ? containerAppsEnvironment!.outputs.apiName : ''
output WEB_NAME string = includeAcr ? containerAppsEnvironment!.outputs.webName : ''
output AZURE_API_MANAGEMENT_NAME string = includeAcr ? apiManagement!.outputs.serviceName : ''
output AZURE_API_MANAGEMENT_GATEWAY_URL string = includeAcr ? apiManagement!.outputs.gatewayUrl : ''
output AZURE_API_MANAGEMENT_PRINCIPAL_ID string = includeAcr ? apiManagement!.outputs.principalId : ''
output AZURE_API_MANAGED_IDENTITY_PRINCIPAL_ID string = identities.outputs.apiPrincipalId
output AZURE_FOUNDRY_PROJECT_PRINCIPAL_ID string = foundryAccount::project.identity.principalId
output AZURE_AGENTIC_GUARDRAIL_ID string = agenticGuardrail.id
