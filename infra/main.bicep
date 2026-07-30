// Provisioning template for a Foundry project service.
//
// Inputs are derived from the host: azure.ai.project service body in
// azure.yaml by internal/synthesis. Greenfield only (no endpoint:); a
// brownfield path is handled by the provider before synthesis.
//
// Subscription-scoped so the resource group is part of the deployment. This
// keeps `azd provision --preview` side-effect free: the resource group shows
// up as a previewed Create instead of being created up front to satisfy a
// resource-group-scoped what-if.

targetScope = 'subscription'

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
param location string

@description('Name of the resource group to create and deploy resources into.')
@minLength(1)
@maxLength(90)
param resourceGroupName string

@description('Azure region for Azure AI Search. Defaults to the primary location.')
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

@description('Include Azure Container Registry and Container Apps for Docker-backed application services.')
param includeAcr bool = true

@description('Object id of the developer running azd. When set, grants Cognitive Services User on the project. Empty disables the role assignment so headless / CI runs do not fail.')
param principalId string = ''

@description('Principal type used in the developer role assignment.')
param principalType string = 'User'

@description('Publisher display name for API Management.')
param apimPublisherName string = 'Research Assistant Accelerator'

@description('Publisher contact email for API Management.')
param apimPublisherEmail string = 'noreply@example.invalid'

@description('Entra ID tenant id used by Azure Container Apps built-in authentication (EasyAuth) to validate incoming bearer tokens. Required when enableEntraAuth is true.')
param entraTenantId string = ''

@description('Client (application) id of the Entra App Registration representing this API, used as the allowed token audience for Container Apps built-in authentication. Required when enableEntraAuth is true. Not created by this template.')
param entraApiClientId string = ''

@description('Enable Azure Container Apps built-in authentication (EasyAuth) on the api container app. Defaults to false so existing deployments are unaffected until an operator has created the Entra App Registration referenced by entraApiClientId.')
param enableEntraAuth bool = false

@description('Maximum connector-adapter HTTP request body in bytes. Default covers a 4 MiB base64 upload plus bounded JSON overhead; use streaming/upload sessions for larger files.')
@minValue(65536)
@maxValue(333398872)
param connectorAdapterMaxRequestBodyBytes int = 5657944

// Resources

resource resourceGroup 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'modules/resources.bicep' = {
  name: 'foundry-resources'
  scope: resourceGroup
  params: {
    location: location
    searchLocation: searchLocation
    tags: tags
    resourceTokenSalt: resourceTokenSalt
    foundryProjectName: foundryProjectName
    deployments: deployments
    includeAcr: includeAcr
    principalId: principalId
    principalType: principalType
    connectorAdapterMaxRequestBodyBytes: connectorAdapterMaxRequestBodyBytes
    apimPublisherName: apimPublisherName
    apimPublisherEmail: apimPublisherEmail
    entraTenantId: entraTenantId
    entraApiClientId: entraApiClientId
    enableEntraAuth: enableEntraAuth
  }
}

// Outputs

output AZURE_RESOURCE_GROUP string = resourceGroup.name
output AZURE_AI_PROJECT_ID string = resources.outputs.AZURE_AI_PROJECT_ID
output AZURE_AI_ACCOUNT_NAME string = resources.outputs.AZURE_AI_ACCOUNT_NAME
output AZURE_AI_PROJECT_NAME string = resources.outputs.AZURE_AI_PROJECT_NAME
output AZURE_OPENAI_ENDPOINT string = resources.outputs.AZURE_OPENAI_ENDPOINT
output AZURE_AI_EMBEDDING_DEPLOYMENT_NAME string = resources.outputs.AZURE_AI_EMBEDDING_DEPLOYMENT_NAME
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.FOUNDRY_PROJECT_ENDPOINT
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = resources.outputs.AZURE_CONTAINER_REGISTRY_RESOURCE_ID
output AZURE_AI_PROJECT_ACR_CONNECTION_NAME string = resources.outputs.AZURE_AI_PROJECT_ACR_CONNECTION_NAME
output AZURE_FOUNDRY_NETWORK_MODE string = resources.outputs.AZURE_FOUNDRY_NETWORK_MODE
output AZURE_FOUNDRY_MANAGED_ISOLATION_MODE string = resources.outputs.AZURE_FOUNDRY_MANAGED_ISOLATION_MODE
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = resources.outputs.AZURE_LOG_ANALYTICS_WORKSPACE_ID
output AZURE_MANAGED_IDENTITY_CLIENT_ID string = resources.outputs.AZURE_MANAGED_IDENTITY_CLIENT_ID
output AZURE_WORKER_MANAGED_IDENTITY_CLIENT_ID string = resources.outputs.AZURE_WORKER_MANAGED_IDENTITY_CLIENT_ID
output AZURE_STORAGE_ACCOUNT_NAME string = resources.outputs.AZURE_STORAGE_ACCOUNT_NAME
output AZURE_STORAGE_BLOB_ENDPOINT string = resources.outputs.AZURE_STORAGE_BLOB_ENDPOINT
output AZURE_STORAGE_SOURCE_CONTAINER string = resources.outputs.AZURE_STORAGE_SOURCE_CONTAINER
output AZURE_STORAGE_ARTIFACT_CONTAINER string = resources.outputs.AZURE_STORAGE_ARTIFACT_CONTAINER
output AZURE_STORAGE_AGENT_STUDIO_BUNDLE_CONTAINER string = resources.outputs.AZURE_STORAGE_AGENT_STUDIO_BUNDLE_CONTAINER
output AZURE_SEARCH_ENDPOINT string = resources.outputs.AZURE_SEARCH_ENDPOINT
output AZURE_SEARCH_SERVICE_NAME string = resources.outputs.AZURE_SEARCH_SERVICE_NAME
output AZURE_SEARCH_INDEX_NAME string = resources.outputs.AZURE_SEARCH_INDEX_NAME
output AZURE_SEARCH_INDEX_DATA_READER_ROLE_ID string = resources.outputs.AZURE_SEARCH_INDEX_DATA_READER_ROLE_ID
output AZURE_COSMOS_ENDPOINT string = resources.outputs.AZURE_COSMOS_ENDPOINT
output AZURE_COSMOS_DATABASE string = resources.outputs.AZURE_COSMOS_DATABASE
output AZURE_COSMOS_AGENT_STUDIO_DATABASE string = resources.outputs.AZURE_COSMOS_AGENT_STUDIO_DATABASE
output AZURE_COSMOS_AGENT_STUDIO_METADATA_CONTAINER string = resources.outputs.AZURE_COSMOS_AGENT_STUDIO_METADATA_CONTAINER
output AZURE_COSMOS_AGENT_STUDIO_MEMORY_CONTAINER string = resources.outputs.AZURE_COSMOS_AGENT_STUDIO_MEMORY_CONTAINER
output AZURE_COSMOS_AGENT_STUDIO_AUDIT_CONTAINER string = resources.outputs.AZURE_COSMOS_AGENT_STUDIO_AUDIT_CONTAINER
output AZURE_COSMOS_AGENT_STUDIO_CATALOG_CONTAINER string = resources.outputs.AZURE_COSMOS_AGENT_STUDIO_CATALOG_CONTAINER
output AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT string = resources.outputs.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
output AZURE_DURABLE_TASK_ENDPOINT string = resources.outputs.AZURE_DURABLE_TASK_ENDPOINT
output AZURE_DURABLE_TASK_HUB string = resources.outputs.AZURE_DURABLE_TASK_HUB
output WEB_URL string = resources.outputs.WEB_URL
output API_URL string = resources.outputs.API_URL
output SERVICE_API_NAME string = resources.outputs.API_NAME
output SERVICE_API_URI string = resources.outputs.API_URL
output SERVICE_WEB_NAME string = resources.outputs.WEB_NAME
output SERVICE_WEB_URI string = resources.outputs.WEB_URL
output SERVICE_WORKER_NAME string = resources.outputs.WORKER_NAME
output SERVICE_CONNECTOR_ADAPTER_NAME string = resources.outputs.CONNECTOR_ADAPTER_NAME
output SERVICE_CONNECTOR_ADAPTER_URI string = resources.outputs.CONNECTOR_ADAPTER_URL
output AZURE_API_MANAGEMENT_NAME string = resources.outputs.AZURE_API_MANAGEMENT_NAME
output AZURE_API_MANAGEMENT_GATEWAY_URL string = resources.outputs.AZURE_API_MANAGEMENT_GATEWAY_URL
output AZURE_CONNECTOR_MCP_URL string = resources.outputs.AZURE_CONNECTOR_MCP_URL
output AZURE_CONNECTOR_MCP_URLS string = resources.outputs.AZURE_CONNECTOR_MCP_URLS
output AZURE_API_MANAGEMENT_PRINCIPAL_ID string = resources.outputs.AZURE_API_MANAGEMENT_PRINCIPAL_ID
