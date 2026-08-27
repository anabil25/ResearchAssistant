targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param logAnalyticsWorkspaceName string
param infrastructureSubnetId string
param apiIdentityPrincipalId string
param acrResourceId string

var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var acrName = last(split(acrResourceId, '/'))
var apiName = 'ca-api-${name}'
var webName = 'ca-web-${name}'

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

resource apiIdentityAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, apiIdentityPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: apiIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

output environmentId string = environment.id
output environmentName string = environment.name
output defaultDomain string = environment.properties.defaultDomain
output webName string = webName
output webUrl string = 'https://${webName}.${environment.properties.defaultDomain}'
output apiName string = apiName
output apiUrl string = 'https://${apiName}.internal.${environment.properties.defaultDomain}'
