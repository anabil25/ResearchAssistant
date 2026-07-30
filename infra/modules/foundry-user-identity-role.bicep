targetScope = 'subscription'

@description('Unique display name for the custom Foundry delegated-user role.')
param roleName string

@description('Narrow scope under which the role can be assigned.')
param assignableScope string

resource roleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, roleName)
  properties: {
    roleName: roleName
    description: 'Lets the trusted Research Assistant API delegate its authenticated end-user identity to Foundry Hosted Agents.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      assignableScope
    ]
  }
}

output roleDefinitionId string = roleDefinition.id
