targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param publisherName string
param publisherEmail string
param subnetResourceId string
param connectorBackendUrl string
param tenantId string
param apiPrincipalId string
param foundryProjectPrincipalId string
param logAnalyticsWorkspaceId string

var connectorOpenApi = loadTextContent('../../packages/contracts/connector-adapter-openapi.json')
var connectorApiId = 'research-connectors-v1'
var connectorMcpId = 'research-connectors-mcp-v1'
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
        </claim>
      </required-claims>
    </validate-azure-ad-token>
    <rate-limit-by-key calls="60" renewal-period="60" counter-key="@(context.Variables.GetValueOrDefault&lt;Jwt&gt;(&quot;validated-token&quot;).Claims.GetValueOrDefault(&quot;appid&quot;, &quot;unknown&quot;))" />
    <validate-content unspecified-content-type-action="ignore" max-size="32768" size-exceeded-action="prevent" errors-variable-name="connector-validation-errors">
      <content type="application/json" validate-as="json" action="prevent" allow-additional-properties="false" />
    </validate-content>
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
var mcpPolicyTemplate = '''
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
        </claim>
      </required-claims>
    </validate-azure-ad-token>
    <rate-limit-by-key calls="30" renewal-period="60" counter-key="@(context.Variables.GetValueOrDefault&lt;Jwt&gt;(&quot;validated-token&quot;).Claims.GetValueOrDefault(&quot;appid&quot;, &quot;unknown&quot;))" />
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
  '__ARM_AUDIENCE__',
  environment().resourceManager
)
var mcpPolicy = replace(
  replace(
    replace(
      replace(mcpPolicyTemplate, '__TENANT_ID__', tenantId),
      '__API_PRINCIPAL_ID__',
      apiPrincipalId
    ),
    '__FOUNDRY_PRINCIPAL_ID__',
    foundryProjectPrincipalId
  ),
  '__ARM_AUDIENCE__',
  environment().resourceManager
)

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
    virtualNetworkType: 'External'
    virtualNetworkConfiguration: {
      subnetResourceId: subnetResourceId
    }
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'false'
    }
  }
}

resource connectorApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apiManagement
  name: connectorApiId
  properties: {
    apiRevision: '1'
    description: 'Narrow, allowlisted public research metadata operations.'
    displayName: 'Research connector adapter'
    format: 'openapi+json'
    path: 'research-connectors/v1'
    protocols: [
      'https'
    ]
    serviceUrl: connectorBackendUrl
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
    value: connectorPolicy
  }
}

resource connectorMcp 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  parent: apiManagement
  name: connectorMcpId
  properties: {
    type: 'mcp'
    path: 'research-connectors-mcp'
    displayName: 'Research connector MCP server'
    description: 'Governed research metadata tools backed by the connector adapter API.'
    protocols: [
      'https'
    ]
    subscriptionRequired: false
  }
  dependsOn: [
    connectorApi
  ]
}

resource literatureTool 'Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview' = {
  parent: connectorMcp
  name: 'searchLiteratureMetadata'
  properties: {
    displayName: 'searchLiteratureMetadata'
    description: 'Search an allowlisted scholarly metadata source with a bounded public query.'
    operationId: resourceId(
      'Microsoft.ApiManagement/service/apis/operations',
      apiManagement.name,
      connectorApi.name,
      'searchLiteratureMetadata'
    )
  }
}

resource grantTool 'Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview' = {
  parent: connectorMcp
  name: 'searchGrantOpportunities'
  properties: {
    displayName: 'searchGrantOpportunities'
    description: 'Search an allowlisted public funding source for current grant opportunities or awards.'
    operationId: resourceId(
      'Microsoft.ApiManagement/service/apis/operations',
      apiManagement.name,
      connectorApi.name,
      'searchGrantOpportunities'
    )
  }
}

resource matchingTool 'Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview' = {
  parent: connectorMcp
  name: 'searchMatchingMetadata'
  properties: {
    displayName: 'searchMatchingMetadata'
    description: 'Search allowlisted public organization, researcher, facility, and award metadata for candidate discovery.'
    operationId: resourceId(
      'Microsoft.ApiManagement/service/apis/operations',
      apiManagement.name,
      connectorApi.name,
      'searchMatchingMetadata'
    )
  }
}

resource connectorMcpPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  parent: connectorMcp
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: mcpPolicy
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
output connectorApiUrl string = '${apiManagement.properties.gatewayUrl}/research-connectors/v1'
output connectorMcpUrl string = '${apiManagement.properties.gatewayUrl}/research-connectors-mcp/mcp'
output principalId string = apiManagement.identity.principalId
