targetScope = 'subscription'

@description('Unique display name for the custom APIM connector credential role.')
param roleName string

@description('Resource group under which this role can be assigned.')
param assignableScope string

resource roleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, roleName)
  properties: {
    roleName: roleName
    description: 'Lets the Research Assistant API set or clear governed connector credentials in API Management.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/service/read'
          'Microsoft.ApiManagement/service/namedValues/read'
          'Microsoft.ApiManagement/service/namedValues/write'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      assignableScope
    ]
  }
}

output roleDefinitionId string = roleDefinition.id
