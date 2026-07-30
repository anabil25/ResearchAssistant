targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param logAnalyticsWorkspaceName string
param appInsightsConnectionString string
param infrastructureSubnetId string
param foundryProjectEndpoint string
param openAIEndpoint string
param apiIdentityResourceId string
param apiIdentityClientId string
param apiIdentityPrincipalId string
param foundryProjectPrincipalId string
param workerIdentityResourceId string
param workerIdentityClientId string
param workerIdentityPrincipalId string
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
param workspaceTenantId string
param workspaceProjectId string
param connectorGatewayUrl string
param connectorGatewayTokenScope string
@minValue(65536)
@maxValue(333398872)
param connectorAdapterMaxRequestBodyBytes int = 5657944
param warmReplicaCount int = 1

@description('Entra ID tenant id used by Azure Container Apps built-in authentication (EasyAuth) to validate incoming bearer tokens. Required when enableEntraAuth is true.')
param entraTenantId string = ''

@description('Client (application) id of the Entra App Registration representing this API, used as the allowed token audience for Container Apps built-in authentication. Required when enableEntraAuth is true. This registration is not created by this template -- see the module header comment.')
param entraApiClientId string = ''

@description('Enable Azure Container Apps built-in authentication (EasyAuth), enforcing a valid Entra ID bearer token on every ingress request to the api container app before it is invoked. Defaults to false so existing deployments are unaffected until an operator has created the Entra App Registration referenced by entraApiClientId. The api container always reports the true value of this flag to itself via RESEARCH_ENTRA_AUTH_ENFORCED, rather than assuming enforcement is active on faith.')
param enableEntraAuth bool = false

@description('Key Vault URI (e.g. https://<vault>.vault.azure.net/) from which the api container sources the attestation signing key/version as Container Apps Key-Vault-backed secrets. Required when attestationSigningSecretsProvisioned is true.')
param attestationKeyVaultUri string = ''

@description('Whether an operator has already populated the attestation signing key secret versions in the Key Vault referenced by attestationKeyVaultUri (an explicit out-of-band step -- see modules/keyvault.bicep). When false (default) the api container runs without a signing key, and research_assistant_api.config\'s fail-closed startup validator refuses to boot with the unsigned sha256-digest fallback outside its known-safe local/dev/test environments.')
param attestationSigningSecretsProvisioned bool = false

// Container Apps Key-Vault-backed secret references for the attestation
// signing key/version, only emitted once an operator has confirmed (via
// attestationSigningSecretsProvisioned) that real secret values exist at
// these Key Vault secret names -- otherwise the api container app would be
// pinned to a non-existent secret version and fail to start.
var attestationSecretRefs = attestationSigningSecretsProvisioned
  ? [
      {
        name: 'agent-studio-attestation-signing-key'
        keyVaultUrl: '${attestationKeyVaultUri}secrets/agent-studio-attestation-signing-key'
        identity: apiIdentityResourceId
      }
      {
        name: 'agent-studio-attestation-signing-key-version'
        keyVaultUrl: '${attestationKeyVaultUri}secrets/agent-studio-attestation-signing-key-version'
        identity: apiIdentityResourceId
      }
    ]
  : []

var attestationEnvVars = attestationSigningSecretsProvisioned
  ? [
      {
        name: 'AGENT_STUDIO_ATTESTATION_SIGNING_KEY'
        secretRef: 'agent-studio-attestation-signing-key'
      }
      {
        name: 'AGENT_STUDIO_ATTESTATION_SIGNING_KEY_VERSION'
        secretRef: 'agent-studio-attestation-signing-key-version'
      }
    ]
  : []

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
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

resource connectorAdapter 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-connectors-${name}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'connector-adapter'
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
      registries: [
        {
          server: acr.properties.loginServer
          identity: apiIdentityResourceId
        }
      ]
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
              name: 'AZURE_CLIENT_ID'
              value: apiIdentityClientId
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
              name: 'RESEARCH_PROVIDER_CALLER_PRINCIPAL_IDS'
              value: '${apiIdentityPrincipalId},${foundryProjectPrincipalId}'
            }
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: foundryProjectEndpoint
            }
            {
              name: 'AZURE_SEARCH_ENDPOINT'
              value: searchEndpoint
            }
            {
              name: 'AZURE_STORAGE_BLOB_ENDPOINT'
              value: storageBlobEndpoint
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'research-assistant-connector-adapter'
            }
            {
              name: 'CONNECTOR_ADAPTER_MAX_REQUEST_BODY_BYTES'
              value: string(connectorAdapterMaxRequestBodyBytes)
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
  dependsOn: [
    apiIdentityAcrPull
  ]
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
      registries: [
        {
          server: acr.properties.loginServer
          identity: apiIdentityResourceId
        }
      ]
      secrets: attestationSecretRefs
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
          env: concat(
            [
              {
                name: 'RESEARCH_ENVIRONMENT'
                value: '${name}-azure'
            }
            {
              name: 'RESEARCH_EXECUTION_MODE'
              value: 'hosted'
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
            {
              name: 'RESEARCH_ENTRA_AUTH_ENFORCED'
              value: string(enableEntraAuth)
            }
            {
              // The API only trusts the platform-injected `x-ms-client-principal`
              // header when Container Apps built-in authentication (EasyAuth) is
              // actually enforcing on ingress. Deriving this flag from the same
              // `enableEntraAuth` toggle that gates the `authConfigs` resource
              // keeps the two in lockstep: the header is trusted exactly when,
              // and only when, the platform is validating tokens and injecting
              // it. This also satisfies the app's fail-closed startup guard
              // (config._forbid_unenforced_platform_identity_trust_outside_safe_environments),
              // which refuses trust_platform_identity_headers=True unless
              // entra_auth_enforced (RESEARCH_ENTRA_AUTH_ENFORCED) is also True.
              name: 'RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS'
              value: string(enableEntraAuth)
            }
            ],
            attestationEnvVars
          )
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
  dependsOn: [
    apiIdentityAcrPull
  ]
}

// Azure Container Apps built-in authentication (EasyAuth). When enabled,
// this validates the Entra ID bearer token (audience/issuer/signature) on
// every ingress request to the api container app *before* the request
// reaches the container, and injects the `x-ms-client-principal` header
// that `research_assistant_api.identity.resolve_identity` trusts -- see
// that module's docstring and `config.Settings.entra_auth_enforced` for the
// corresponding app-level trust boundary and fail-closed startup guard.
// Gated behind enableEntraAuth (default false) because it requires an
// operator to have first created the Entra App Registration referenced by
// entraApiClientId -- an out-of-band step this template does not perform.
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

resource web 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-web-${name}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'web'
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
      registries: [
        {
          server: acr.properties.loginServer
          identity: apiIdentityResourceId
        }
      ]
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
  dependsOn: [
    apiIdentityAcrPull
  ]
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
      registries: [
        {
          server: acr.properties.loginServer
          identity: workerIdentityResourceId
        }
      ]
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
  dependsOn: [
    workerIdentityAcrPull
  ]
}

resource apiIdentityAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, apiIdentityPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: apiIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource workerIdentityAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, workerIdentityPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: workerIdentityPrincipalId
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
