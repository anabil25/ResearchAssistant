targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param publisherName string
param publisherEmail string
param tenantId string
param apiPrincipalId string
param foundryProjectPrincipalId string
param logAnalyticsWorkspaceId string

var connectorOpenApi = loadTextContent('../../infra/provider-specs/authored/research_connectors.json')
var connectorMcpDefinitions = loadJsonContent('../../infra/connector-mcp-catalog.json')
var connectorOperationPolicies = loadJsonContent('../../infra/connector-operation-policies.json')
var connectorApiId = 'research-connectors-v1'
var connectorMcpProductId = 'research-agent-tools'
var connectorMcpSubscriptionId = 'foundry-agent-tools'
var connectorPolicyTemplate = '''
<policies>
  <inbound>
    <base />
    <validate-azure-ad-token tenant-id="__TENANT_ID__" output-token-variable-name="validated-token">
      <audiences>
        <audience>__ARM_AUDIENCE__</audience>
      </audiences>
      <required-claims>
        <claim name="oid" match="any">
          <value>__API_PRINCIPAL_ID__</value>
          <value>__FOUNDRY_PRINCIPAL_ID__</value>
          <value>__APIM_PRINCIPAL_ID__</value>
        </claim>
      </required-claims>
    </validate-azure-ad-token>
    <validate-parameters specified-parameter-action="prevent" unspecified-parameter-action="prevent" errors-variable-name="connector-validation-errors">
      <headers specified-parameter-action="ignore" unspecified-parameter-action="ignore" />
    </validate-parameters>
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
'''
var mcpPolicyTemplate = '''
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="__ARM_AUDIENCE__" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
'''
var connectorPolicy = replace(
  replace(
    replace(
      replace(connectorPolicyTemplate, '__TENANT_ID__', tenantId),
      '__API_PRINCIPAL_ID__',
      apiPrincipalId
    ),
    '__FOUNDRY_PRINCIPAL_ID__',
    foundryProjectPrincipalId
  ),
  '__APIM_PRINCIPAL_ID__',
  apiManagement.identity.principalId
)
var connectorPolicyWithAudience = replace(
  connectorPolicy,
  '__ARM_AUDIENCE__',
  environment().resourceManager
)
var mcpPolicy = replace(mcpPolicyTemplate, '__ARM_AUDIENCE__', environment().resourceManager)

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

resource connectorMcpProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apiManagement
  name: connectorMcpProductId
  properties: {
    displayName: 'Research agent tools'
    description: 'Governed connector MCP servers consumed by Microsoft Foundry.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

resource connectorMcpSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apiManagement
  name: connectorMcpSubscriptionId
  properties: {
    displayName: 'Microsoft Foundry connector MCP access'
    scope: '/products/${connectorMcpProduct.name}'
    state: 'active'
    allowTracing: false
  }
}

resource connectorContact 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apiManagement
  name: 'research-connector-contact'
  properties: {
    displayName: 'research-connector-contact'
    value: publisherEmail
    secret: false
    tags: [
      'connector'
    ]
  }
}

resource connectorApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apiManagement
  name: connectorApiId
  properties: {
    apiRevision: '1'
    description: 'Narrow normalized public research metadata operations implemented by APIM policies.'
    displayName: 'Research connector facade'
    format: 'openapi+json'
    path: 'research-connectors'
    protocols: [
      'https'
    ]
    serviceUrl: 'https://normalized-connectors.invalid'
    subscriptionRequired: false
    type: 'http'
    value: connectorOpenApi
  }
}

resource connectorApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: connectorApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: connectorPolicyWithAudience
  }
}

resource connectorPolicies 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [
  for policy in connectorOperationPolicies: {
    name: '${apiManagement.name}/${connectorApi.name}/${policy.operationId}/policy'
    properties: {
      format: 'rawxml'
      value: policy.value
    }
    dependsOn: [
      connectorContact
    ]
  }
]

resource sourceConnectorMcps 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = [
  for connector in connectorMcpDefinitions: {
    parent: apiManagement
    name: connector.apiId
  properties: {
      type: 'mcp'
      path: connector.path
      displayName: connector.displayName
      description: connector.description
      protocols: [
        'https'
      ]
      subscriptionRequired: false
    }
    dependsOn: [
      connectorApi
    ]
  }
]

resource sourceConnectorMcpPolicies 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = [
  for (connector, index) in connectorMcpDefinitions: {
    parent: sourceConnectorMcps[index]
    name: 'policy'
    properties: {
      format: 'rawxml'
      value: mcpPolicy
    }
  }
]

resource sourceConnectorMcpProductApis 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = [
  for (connector, index) in connectorMcpDefinitions: {
    parent: connectorMcpProduct
    name: sourceConnectorMcps[index].name
  }
]

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
output connectorApiUrl string = '${apiManagement.properties.gatewayUrl}/research-connectors'
output connectorMcpUrls array = [
  for connector in connectorMcpDefinitions: {
    id: connector.id
    endpoint: '${apiManagement.properties.gatewayUrl}/${connector.path}/mcp'
  }
]
output connectorMcpSubscriptionId string = connectorMcpSubscription.name
output principalId string = apiManagement.identity.principalId
