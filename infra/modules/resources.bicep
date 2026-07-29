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

@description('Tags applied to all resources.')
param tags object = {}

@description('Optional salt to vary resource names across re-provisions.')
param resourceTokenSalt string = ''

@description('Foundry project name. 3-32 alphanumeric/hyphen chars.')
@minLength(3)
@maxLength(32)
param foundryProjectName string

@description('Model deployments to provision on the Foundry account.')
param deployments deploymentsType = []

@description('Include an Azure Container Registry. Set true when any agent uses docker:.')
param includeAcr bool = false

@description('Maximum connector-adapter HTTP request body in bytes.')
@minValue(65536)
@maxValue(333398872)
param connectorAdapterMaxRequestBodyBytes int = 5657944

@description('Enable APIM MCP tool resources. Disabled by default because preview APIs can return transient 502 during provisioning.')
param enableApimMcpTools bool = false

@description('Object id of the developer running azd. When set, grants project data-plane roles. Empty disables the role assignments so headless / CI runs do not fail.')
param principalId string = ''

@description('Principal type used in the developer role assignment.')
param principalType string = 'User'

@description('Publisher display name for API Management.')
param apimPublisherName string

@description('Publisher contact email for API Management.')
param apimPublisherEmail string

@description('Entra ID tenant id used by Azure Container Apps built-in authentication (EasyAuth) to validate incoming bearer tokens. Required when enableEntraAuth is true.')
param entraTenantId string = ''

@description('Client (application) id of the Entra App Registration representing this API, used as the allowed token audience for Container Apps built-in authentication. Required when enableEntraAuth is true. Not created by this template -- see modules/container-apps.bicep.')
param entraApiClientId string = ''

@description('Enable Azure Container Apps built-in authentication (EasyAuth) on the api container app. Defaults to false; see modules/container-apps.bicep for the full trust-boundary rationale.')
param enableEntraAuth bool = false

@description('Provision a Key Vault (see modules/keyvault.bicep) so an operator can deliver the Agent Studio ReleaseAttestation signing key to the api container app via Container Apps secrets, instead of a plaintext env var.')
param includeAttestationKeyVault bool = false

@description('Whether an operator has already populated the attestation signing key secret versions in the provisioned Key Vault (an explicit out-of-band step). Only takes effect when includeAttestationKeyVault is true.')
param attestationSigningSecretsProvisioned bool = false

// Variables

var resourceToken = empty(resourceTokenSalt)
  ? uniqueString(subscription().id, resourceGroup().id, location)
  : uniqueString(subscription().id, resourceGroup().id, location, resourceTokenSalt)

var abbrs = loadJsonContent('../abbreviations.json')

var foundryAccountName = '${abbrs.cognitiveServicesAccounts}${resourceToken}'
var apiManagementName = 'apim-${resourceToken}'
var embeddingDeployments = filter(
  deployments,
  deployment => deployment.model.name == 'text-embedding-3-large'
)
var embeddingDeploymentName = length(embeddingDeployments) == 1
  ? embeddingDeployments[0].name
  : ''

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
  name: foundryAccountName
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
    customSubDomainName: foundryAccountName
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
    name: foundryProjectName
    location: location
    identity: {
      type: 'SystemAssigned'
    }
    properties: {
      description: '${foundryProjectName} Project'
      displayName: foundryProjectName
    }
    // Explicit dependsOn ensures all model deployments complete before
    // the project is created; the project does not reference them so
    // there is no implicit dependency Bicep can infer.
    dependsOn: [
      modelDeployments
    ]
  }
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
    workerPrincipalId: identities.outputs.workerPrincipalId
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
    workerPrincipalId: identities.outputs.workerPrincipalId
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
    workerPrincipalId: identities.outputs.workerPrincipalId
  }
}

module keyVault 'keyvault.bicep' = if (includeAttestationKeyVault) {
  name: 'research-attestation-keyvault'
  params: {
    name: 'kv-${take(resourceToken, 17)}'
    location: location
    tags: tags
    apiPrincipalId: identities.outputs.apiPrincipalId
    principalId: principalId
    principalType: principalType
  }
}

module documentIntelligence 'document-intelligence.bicep' = {
  name: 'document-intelligence'
  params: {
    name: 'di-${resourceToken}'
    location: location
    tags: tags
    workerPrincipalId: identities.outputs.workerPrincipalId
  }
}

module durableTask 'durable-task.bicep' = {
  name: 'durable-task'
  params: {
    name: 'dts-${resourceToken}'
    location: location
    tags: tags
    apiPrincipalId: identities.outputs.apiPrincipalId
    workerPrincipalId: identities.outputs.workerPrincipalId
  }
}

resource apiFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount::project.id, 'id-api-${resourceToken}', foundryUserRoleId)
  scope: foundryAccount::project
  properties: {
    principalId: identities.outputs.apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryUserRoleId
  }
}

resource workerModelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, 'id-worker-${resourceToken}', cognitiveServicesOpenAIUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: identities.outputs.workerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cognitiveServicesOpenAIUserRoleId
  }
}

module containerApps 'container-apps.bicep' = if (includeAcr) {
  name: 'research-container-apps'
  params: {
    name: take(resourceToken, 8)
    location: location
    tags: tags
    logAnalyticsWorkspaceName: monitoring.outputs.workspaceName
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    foundryProjectEndpoint: 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/${foundryAccount::project.name}'
    openAIEndpoint: 'https://${foundryAccount.name}.openai.azure.com/'
    apiIdentityResourceId: identities.outputs.apiResourceId
    apiIdentityClientId: identities.outputs.apiClientId
    apiIdentityPrincipalId: identities.outputs.apiPrincipalId
    foundryProjectPrincipalId: foundryAccount::project.identity.principalId
    workerIdentityResourceId: identities.outputs.workerResourceId
    workerIdentityClientId: identities.outputs.workerClientId
    acrResourceId: acr!.outputs.resourceId
    searchEndpoint: search.outputs.endpoint
    searchIndexName: search.outputs.indexName
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabaseName: cosmos.outputs.databaseName
    agentStudioCosmosDatabaseName: cosmos.outputs.agentStudioDatabaseName
    agentStudioMetadataContainerName: cosmos.outputs.agentStudioMetadataContainerName
    agentStudioMemoryContainerName: cosmos.outputs.agentStudioMemoryContainerName
    agentStudioAuditContainerName: cosmos.outputs.agentStudioAuditContainerName
    agentStudioCatalogContainerName: cosmos.outputs.agentStudioCatalogContainerName
    storageAccountName: storage.outputs.accountName
    storageBlobEndpoint: storage.outputs.blobEndpoint
    sourceContainerName: storage.outputs.sourcesContainer
    artifactContainerName: storage.outputs.artifactsContainer
    agentStudioBundleContainerName: storage.outputs.agentStudioBundlesContainer
    documentIntelligenceEndpoint: documentIntelligence.outputs.endpoint
    embeddingDeploymentName: embeddingDeploymentName
    durableTaskEndpoint: durableTask.outputs.endpoint
    durableTaskHubName: durableTask.outputs.taskHubName
    workspaceTenantId: subscription().tenantId
    workspaceProjectId: foundryAccount::project.name
    connectorGatewayUrl: 'https://${apiManagementName}.azure-api.net/research-connectors'
    connectorGatewayTokenScope: '${environment().resourceManager}.default'
    entraTenantId: entraTenantId
    entraApiClientId: entraApiClientId
    enableEntraAuth: enableEntraAuth
    attestationKeyVaultUri: includeAttestationKeyVault ? keyVault!.outputs.vaultUri : ''
    attestationSigningSecretsProvisioned: includeAttestationKeyVault && attestationSigningSecretsProvisioned
    connectorAdapterMaxRequestBodyBytes: connectorAdapterMaxRequestBodyBytes
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
    connectorBackendUrl: containerApps!.outputs.connectorAdapterUrl
    tenantId: subscription().tenantId
    apiPrincipalId: identities.outputs.apiPrincipalId
    foundryProjectPrincipalId: foundryAccount::project.identity.principalId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    enableMcpTools: enableApimMcpTools
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

resource workerTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsightsForRbac.id, 'id-worker-${resourceToken}', monitoringMetricsPublisherRoleId)
  scope: appInsightsForRbac
  properties: {
    principalId: identities.outputs.workerPrincipalId
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

// Outputs

output AZURE_AI_PROJECT_ID string = foundryAccount::project.id
output AZURE_AI_ACCOUNT_NAME string = foundryAccount.name
output AZURE_AI_PROJECT_NAME string = foundryAccount::project.name
output AZURE_OPENAI_ENDPOINT string = 'https://${foundryAccount.name}.openai.azure.com/'
output AZURE_AI_EMBEDDING_DEPLOYMENT_NAME string = embeddingDeploymentName
output FOUNDRY_PROJECT_ENDPOINT string = 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/${foundryAccount::project.name}'
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = includeAcr ? acr!.outputs.loginServer : ''
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = includeAcr ? acr!.outputs.resourceId : ''
output AZURE_AI_PROJECT_ACR_CONNECTION_NAME string = includeAcr ? acr!.outputs.connectionName : ''
output AZURE_FOUNDRY_NETWORK_MODE string = 'none'
output AZURE_FOUNDRY_MANAGED_ISOLATION_MODE string = ''
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceId
output AZURE_MANAGED_IDENTITY_CLIENT_ID string = identities.outputs.apiClientId
output AZURE_WORKER_MANAGED_IDENTITY_CLIENT_ID string = identities.outputs.workerClientId
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
output AZURE_DURABLE_TASK_ENDPOINT string = durableTask.outputs.endpoint
output AZURE_DURABLE_TASK_HUB string = durableTask.outputs.taskHubName
output AZURE_AGENT_STUDIO_ATTESTATION_KEY_VAULT_URI string = includeAttestationKeyVault ? keyVault!.outputs.vaultUri : ''
output AZURE_AGENT_STUDIO_ATTESTATION_KEY_VAULT_NAME string = includeAttestationKeyVault ? keyVault!.outputs.vaultName : ''
output WEB_URL string = includeAcr ? containerApps!.outputs.webUrl : ''
output API_URL string = includeAcr ? containerApps!.outputs.apiUrl : ''
output API_NAME string = includeAcr ? containerApps!.outputs.apiName : ''
output WEB_NAME string = includeAcr ? containerApps!.outputs.webName : ''
output WORKER_NAME string = includeAcr ? containerApps!.outputs.workerName : ''
output CONNECTOR_ADAPTER_NAME string = includeAcr ? containerApps!.outputs.connectorAdapterName : ''
output CONNECTOR_ADAPTER_URL string = includeAcr ? containerApps!.outputs.connectorAdapterUrl : ''
output AZURE_API_MANAGEMENT_NAME string = includeAcr ? apiManagement!.outputs.serviceName : ''
output AZURE_API_MANAGEMENT_GATEWAY_URL string = includeAcr ? apiManagement!.outputs.gatewayUrl : ''
output AZURE_CONNECTOR_MCP_URL string = includeAcr ? apiManagement!.outputs.connectorMcpUrl : ''
output AZURE_API_MANAGEMENT_PRINCIPAL_ID string = includeAcr ? apiManagement!.outputs.principalId : ''
