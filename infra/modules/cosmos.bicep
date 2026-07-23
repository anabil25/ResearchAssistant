targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param apiPrincipalId string
param workerPrincipalId string

var dataContributorRoleDefinitionId = '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'

resource account 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    locations: [
      {
        failoverPriority: 0
        isZoneRedundant: false
        locationName: location
      }
    ]
    networkAclBypass: 'None'
    publicNetworkAccess: 'Disabled'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' = {
  parent: account
  name: 'research'
  properties: {
    resource: {
      id: 'research'
    }
  }
}

resource projects 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: database
  name: 'projects'
  properties: {
    resource: {
      id: 'projects'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantId'
        ]
      }
    }
  }
}

resource sources 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: database
  name: 'sources'
  properties: {
    resource: {
      id: 'sources'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantProjectKey'
        ]
      }
    }
  }
}

resource runs 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: database
  name: 'runs'
  properties: {
    resource: {
      id: 'runs'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantRunKey'
        ]
      }
    }
  }
}

resource apiDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, apiPrincipalId, dataContributorRoleDefinitionId)
  properties: {
    principalId: apiPrincipalId
    roleDefinitionId: dataContributorRoleDefinitionId
    scope: account.id
  }
}

resource workerDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, workerPrincipalId, dataContributorRoleDefinitionId)
  properties: {
    principalId: workerPrincipalId
    roleDefinitionId: dataContributorRoleDefinitionId
    scope: account.id
  }
}

output accountId string = account.id
output accountName string = account.name
output endpoint string = account.properties.documentEndpoint
output databaseName string = database.name
