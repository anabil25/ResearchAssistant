targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param logAnalyticsWorkspaceName string
param appInsightsConnectionString string
param foundryProjectEndpoint string
param openAIEndpoint string
param apiIdentityResourceId string
param apiIdentityClientId string
param workerIdentityResourceId string
param workerIdentityClientId string
param acrResourceId string
param searchEndpoint string
param searchIndexName string
param cosmosEndpoint string
param cosmosDatabaseName string
param agentStudioCosmosDatabaseName string
param agentStudioMetadataContainerName string
param agentStudioMemoryContainerName string
param agentStudioAuditContainerName string
param agentStudioCatalogContainerName string
param storageAccountName string
param storageBlobEndpoint string
param sourceContainerName string
param artifactContainerName string
param agentStudioBundleContainerName string
param documentIntelligenceEndpoint string
param embeddingDeploymentName string
param durableTaskEndpoint string
param durableTaskHubName string
param infrastructureSubnetId string
param workspaceTenantId string
param workspaceProjectId string
param connectorGatewayUrl string
param connectorGatewayTokenScope string
param warmReplicaCount int = 1

var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var acrName = last(split(acrResourceId, '/'))

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: 'cae-${name}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
  }
}

resource connectorAdapter 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-connectors-${name}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'connector-adapter'
  })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 8200
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
          name: 'connector-adapter'
          image: placeholderImage
          env: [
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'RESEARCH_WORKSPACE_TENANT_ID'
              value: workspaceTenantId
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'research-assistant-connector-adapter'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8200
              }
              periodSeconds: 5
              failureThreshold: 36
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8200
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: 8200
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
            name: 'connector-http'
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

resource api 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-api-${name}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'api'
  })
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
          image: placeholderImage
          env: [
            {
              name: 'RESEARCH_ENVIRONMENT'
              value: '${name}-azure'
            }
            {
              name: 'RESEARCH_EXECUTION_MODE'
              value: 'hosted'
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
              name: 'DURABLE_TASK_SCHEDULER_CONNECTION_STRING'
              value: 'Endpoint=${durableTaskEndpoint};TaskHub=${durableTaskHubName};Authentication=ManagedIdentity;ClientID=${apiIdentityClientId}'
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'research-assistant-api'
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

resource web 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-web-${name}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'web'
  })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 3000
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
          name: 'web'
          image: placeholderImage
          env: [
            {
              name: 'INTERNAL_API_URL'
              value: 'https://${api.properties.configuration.ingress.fqdn}'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 3000
              }
              periodSeconds: 5
              failureThreshold: 36
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 3000
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 3000
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
            name: 'web-http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

resource worker 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-worker-${name}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'worker'
  })
  identity: {
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${workerIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: placeholderImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: workerIdentityClientId
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
              name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME'
              value: 'gpt-5.4-mini'
            }
            {
              name: 'AZURE_AI_EMBEDDING_DEPLOYMENT_NAME'
              value: embeddingDeploymentName
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
              name: 'AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT'
              value: documentIntelligenceEndpoint
            }
            {
              name: 'DURABLE_TASK_SCHEDULER_CONNECTION_STRING'
              value: 'Endpoint=${durableTaskEndpoint};TaskHub=${durableTaskHubName};Authentication=ManagedIdentity;ClientID=${workerIdentityClientId}'
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'research-assistant-worker'
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

resource webAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, web.name, acrPullRoleId)
  scope: acr
  properties: {
    principalId: web.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource apiAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, api.name, acrPullRoleId)
  scope: acr
  properties: {
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource workerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, worker.name, acrPullRoleId)
  scope: acr
  properties: {
    principalId: worker.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource connectorAdapterAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, connectorAdapter.name, acrPullRoleId)
  scope: acr
  properties: {
    principalId: connectorAdapter.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}


output environmentId string = environment.id
output environmentName string = environment.name
output webName string = web.name
output webUrl string = 'https://${web.properties.configuration.ingress.fqdn}'
output webPrincipalId string = web.identity.principalId
output apiName string = api.name
output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output workerName string = worker.name
output connectorAdapterName string = connectorAdapter.name
output connectorAdapterUrl string = 'https://${connectorAdapter.properties.configuration.ingress.fqdn}'
