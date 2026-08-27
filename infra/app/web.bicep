targetScope = 'resourceGroup'

param azdEnvironmentName string
param tags string = ''
param name string
param location string
param containerAppsEnvironmentName string
param containerRegistryName string
param imageName string
param apiIdentityResourceId string
param internalApiUrl string
param warmReplicaCount int = 1

var effectiveTags = union(empty(tags) ? {} : base64ToJson(tags), {
  'azd-env-name': azdEnvironmentName
  'azd-service-name': 'web'
})

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: containerAppsEnvironmentName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource web 'Microsoft.App/containerApps@2026-01-01' = {
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
          image: imageName
          env: [
            {
              name: 'INTERNAL_API_URL'
              value: internalApiUrl
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

output SERVICE_WEB_NAME string = web.name
output SERVICE_WEB_URI string = 'https://${web.properties.configuration.ingress.fqdn}'
output SERVICE_WEB_IMAGE_NAME string = imageName
output SERVICE_WEB_ID string = web.id
output SERVICE_WEB_IDENTITY_PRINCIPAL_ID string = web.identity.principalId
