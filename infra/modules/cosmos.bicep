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

// Agent Studio uses a separate Cosmos database on the same account so its
// lifecycle (retention, throughput, future migration) can evolve
// independently from the main "research" database. Every container here is
// partitioned by the synthetic `/scope_key` field
// (`ScopeContext.scope_key` / `compute_scope_key(tenant_id, project_id)`),
// matching the single shared partitioning convention used across
// `cosmos_store.py`, `memory_service.py`, and `audit_service.py`. Names must
// stay in lockstep with the `agent_studio_*` defaults in
// `services/api/src/research_assistant_api/config.py`.
resource agentStudioDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' = {
  parent: account
  name: 'agent-studio'
  properties: {
    resource: {
      id: 'agent-studio'
    }
  }
}

resource agentStudioMetadata 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: agentStudioDatabase
  name: 'agentStudioMetadataV1'
  properties: {
    resource: {
      id: 'agentStudioMetadataV1'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/scope_key'
        ]
      }
    }
  }
}

resource agentStudioMemory 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: agentStudioDatabase
  name: 'agentStudioMemoryV1'
  properties: {
    resource: {
      id: 'agentStudioMemoryV1'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/scope_key'
        ]
      }
      // Retention/expiry for memory entries is enforced in application code
      // via `expires_at` filtering (see `memory_service.py`), not native
      // Cosmos TTL, so no `defaultTtl` is configured here. Revisit if a
      // future change starts writing a per-item `ttl` field.
    }
  }
}

resource agentStudioAudit 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: agentStudioDatabase
  name: 'agentStudioAuditV1'
  properties: {
    resource: {
      id: 'agentStudioAuditV1'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/scope_key'
        ]
      }
    }
  }
}

resource agentStudioCatalog 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: agentStudioDatabase
  name: 'agentStudioCatalogV1'
  properties: {
    resource: {
      id: 'agentStudioCatalogV1'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/scope_key'
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
output agentStudioDatabaseName string = agentStudioDatabase.name
output agentStudioMetadataContainerName string = agentStudioMetadata.name
output agentStudioMemoryContainerName string = agentStudioMemory.name
output agentStudioAuditContainerName string = agentStudioAudit.name
output agentStudioCatalogContainerName string = agentStudioCatalog.name
