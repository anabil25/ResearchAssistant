targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param apiPrincipalId string
param workerPrincipalId string
param principalId string = ''
param principalType string = 'User'

var searchIndexDataReaderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '1407120a-92aa-4202-b7e9-c0e197c71c8f'
)
var searchIndexDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
)
var searchServiceContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
)

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'basic'
  }
  properties: {
    disableLocalAuth: true
    hostingMode: 'Default'
    networkRuleSet: {
      bypass: 'None'
      ipRules: []
    }
    partitionCount: 1
    publicNetworkAccess: 'enabled'
    replicaCount: 2
    semanticSearch: 'free'
  }
}

resource apiReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, apiPrincipalId, searchIndexDataReaderRoleId)
  scope: search
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: searchIndexDataReaderRoleId
  }
}

resource workerDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, workerPrincipalId, searchIndexDataContributorRoleId)
  scope: search
  properties: {
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: searchIndexDataContributorRoleId
  }
}

resource deployerServiceContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(search.id, principalId, searchServiceContributorRoleId)
  scope: search
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: searchServiceContributorRoleId
  }
}

resource deployerDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(search.id, principalId, searchIndexDataContributorRoleId)
  scope: search
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: searchIndexDataContributorRoleId
  }
}

output id string = search.id
output name string = search.name
output endpoint string = 'https://${search.name}.search.windows.net'
output indexName string = 'research-evidence'
output readerRoleDefinitionId string = searchIndexDataReaderRoleId
