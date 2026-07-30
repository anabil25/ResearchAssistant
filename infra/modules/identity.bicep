targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-api-${name}'
  location: location
  tags: tags
}

output apiClientId string = apiIdentity.properties.clientId
output apiPrincipalId string = apiIdentity.properties.principalId
output apiResourceId string = apiIdentity.id
