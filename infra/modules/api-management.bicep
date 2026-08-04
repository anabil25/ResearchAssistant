targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param publisherName string
param publisherEmail string
param apiPrincipalId string
param namedValueWriterRoleDefinitionId string
param logAnalyticsWorkspaceId string

resource apiManagement 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'StandardV2'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherName: publisherName
    publisherEmail: publisherEmail
    publicNetworkAccess: 'Enabled'
    virtualNetworkType: 'None'
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'false'
    }
  }
}

resource connectorCredentialWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apiManagement.id, apiPrincipalId, namedValueWriterRoleDefinitionId)
  scope: apiManagement
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: namedValueWriterRoleDefinitionId
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'apim-diagnostics'
  scope: apiManagement
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output serviceName string = apiManagement.name
output gatewayUrl string = apiManagement.properties.gatewayUrl
output principalId string = apiManagement.identity.principalId
