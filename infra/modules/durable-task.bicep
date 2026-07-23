targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param apiPrincipalId string
param workerPrincipalId string

var dataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0ad04412-c4d5-4796-b79c-f76d14c8d402'
)

resource scheduler 'Microsoft.DurableTask/schedulers@2026-02-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    ipAllowlist: [
      '0.0.0.0/0'
    ]
    sku: {
      name: 'Consumption'
    }
  }
}

resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2026-02-01' = {
  parent: scheduler
  name: 'research'
}

resource apiDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(taskHub.id, apiPrincipalId, dataContributorRoleId)
  scope: taskHub
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: dataContributorRoleId
  }
}

resource workerDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(taskHub.id, workerPrincipalId, dataContributorRoleId)
  scope: taskHub
  properties: {
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: dataContributorRoleId
  }
}

output id string = scheduler.id
output endpoint string = scheduler.properties.endpoint
output taskHubName string = taskHub.name
