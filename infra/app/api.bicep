targetScope = 'resourceGroup'

param azdEnvironmentName string
param tags string = ''
param name string
param location string
param containerAppsEnvironmentName string
param containerRegistryName string
param imageName string
param apiIdentityResourceId string
param apiIdentityClientId string
param applicationResourceToken string
param foundryProjectEndpoint string
param openAIEndpoint string
param searchEndpoint string
param searchIndexName string
param embeddingDeploymentName string
param documentIntelligenceEndpoint string
param cosmosEndpoint string
param cosmosDatabaseName string
param storageAccountName string
param storageBlobEndpoint string
param sourceContainerName string
param artifactContainerName string
param agentStudioCosmosDatabaseName string
param agentStudioMetadataContainerName string
param agentStudioMemoryContainerName string
param agentStudioAuditContainerName string
param agentStudioCatalogContainerName string
param agentStudioBundleContainerName string
param appInsightsConnectionString string
param connectorGatewayUrl string
param connectorGatewayTokenScope string
param workspaceTenantId string
param workspaceProjectId string
param entraAuthEnforced string = 'false'
param entraTenantId string = ''
param entraApiClientId string = ''
param warmReplicaCount int = 1

var enableEntraAuth = toLower(entraAuthEnforced) == 'true'
var effectiveTags = union(empty(tags) ? {} : base64ToJson(tags), {
  'azd-env-name': azdEnvironmentName
  'azd-service-name': 'api'
})

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: containerAppsEnvironmentName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource api 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: effectiveTags
  identity: {
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${apiIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: apiIdentityResourceId
        }
      ]
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8000
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'auto'
      }
    }
    template: {
      containers: [
        {
          name: 'api'
          image: imageName
          env: [
            {
              name: 'RESEARCH_ENVIRONMENT'
              value: '${applicationResourceToken}-azure'
            }
            {
              name: 'RESEARCH_REQUIRE_APPROVAL_CONTEXT_RESOLVER'
              value: 'true'
            }
            {
              name: 'RESEARCH_COORDINATOR_AGENT_NAME'
              value: 'research-coordinator'
            }
            {
              name: 'RESEARCH_CONNECTOR_GATEWAY_URL'
              value: connectorGatewayUrl
            }
            {
              name: 'RESEARCH_CONNECTOR_GATEWAY_TOKEN_SCOPE'
              value: connectorGatewayTokenScope
            }
            {
              name: 'RESEARCH_WORKSPACE_TENANT_ID'
              value: workspaceTenantId
            }
            {
              name: 'RESEARCH_WORKSPACE_PROJECT_ID'
              value: workspaceProjectId
            }
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: foundryProjectEndpoint
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: openAIEndpoint
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: apiIdentityClientId
            }
            {
              name: 'AZURE_SEARCH_ENDPOINT'
              value: searchEndpoint
            }
            {
              name: 'AZURE_SEARCH_INDEX_NAME'
              value: searchIndexName
            }
            {
              name: 'AZURE_AI_EMBEDDING_DEPLOYMENT_NAME'
              value: embeddingDeploymentName
            }
            {
              name: 'AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT'
              value: documentIntelligenceEndpoint
            }
            {
              name: 'AZURE_COSMOS_ENDPOINT'
              value: cosmosEndpoint
            }
            {
              name: 'AZURE_COSMOS_DATABASE'
              value: cosmosDatabaseName
            }
            {
              name: 'AZURE_STORAGE_ACCOUNT_NAME'
              value: storageAccountName
            }
            {
              name: 'AZURE_STORAGE_BLOB_ENDPOINT'
              value: storageBlobEndpoint
            }
            {
              name: 'AZURE_STORAGE_SOURCE_CONTAINER'
              value: sourceContainerName
            }
            {
              name: 'AZURE_STORAGE_ARTIFACT_CONTAINER'
              value: artifactContainerName
            }
            {
              name: 'AZURE_COSMOS_AGENT_STUDIO_DATABASE'
              value: agentStudioCosmosDatabaseName
            }
            {
              name: 'AZURE_COSMOS_AGENT_STUDIO_METADATA_CONTAINER'
              value: agentStudioMetadataContainerName
            }
            {
              name: 'AZURE_COSMOS_AGENT_STUDIO_MEMORY_CONTAINER'
              value: agentStudioMemoryContainerName
            }
            {
              name: 'AZURE_COSMOS_AGENT_STUDIO_AUDIT_CONTAINER'
              value: agentStudioAuditContainerName
            }
            {
              name: 'AZURE_COSMOS_AGENT_STUDIO_CATALOG_CONTAINER'
              value: agentStudioCatalogContainerName
            }
            {
              name: 'AZURE_STORAGE_AGENT_STUDIO_BUNDLE_CONTAINER'
              value: agentStudioBundleContainerName
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'research-assistant-api'
            }
            {
              name: 'RESEARCH_ENTRA_AUTH_ENFORCED'
              value: entraAuthEnforced
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8000
              }
              periodSeconds: 5
              failureThreshold: 36
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: warmReplicaCount
        maxReplicas: 5
        rules: [
          {
            name: 'api-http'
            http: {
              metadata: {
                concurrentRequests: '30'
              }
            }
          }
        ]
      }
    }
  }
}

resource apiAuthConfig 'Microsoft.App/containerApps/authConfigs@2026-01-01' = if (enableEntraAuth) {
  parent: api
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${az.environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
          clientId: entraApiClientId
        }
        validation: {
          allowedAudiences: [
            'api://${entraApiClientId}'
            entraApiClientId
          ]
        }
      }
    }
  }
}

output SERVICE_API_NAME string = api.name
output SERVICE_API_URI string = 'https://${api.properties.configuration.ingress.fqdn}'
output SERVICE_API_IMAGE_NAME string = imageName
output SERVICE_API_ID string = api.id
output SERVICE_API_IDENTITY_PRINCIPAL_ID string = api.identity.principalId
