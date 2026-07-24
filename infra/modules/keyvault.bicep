// RBAC-authorization-enabled Key Vault used to deliver production secret
// material to the API container app via Container Apps `secretRef` --
// currently just the Agent Studio ReleaseAttestation HMAC signing key
// (`AGENT_STUDIO_ATTESTATION_SIGNING_KEY` / `_VERSION`).
//
// This module deliberately never mints the actual secret *value*: Bicep/ARM
// deployment-time randomness is not a suitable source of cryptographic key
// material, and generating a real signing secret through IaC would defeat
// the purpose of a managed, rotatable secret. Populating the secret version
// is an explicit out-of-band operational step (e.g. `az keyvault secret set
// --vault-name <vaultName> --name agent-studio-attestation-signing-key
// --value <key>`), the same disclosure pattern already used for the Entra
// App Registration this deployment also does not create automatically.

targetScope = 'resourceGroup'

@description('Name of the Key Vault to create.')
param name string

@description('Azure region for the Key Vault.')
param location string

@description('Tags applied to the Key Vault.')
param tags object = {}

@description('Principal id of the API managed identity. Granted Key Vault Secrets User so the running API container can read secret values at runtime.')
param apiPrincipalId string

@description('Object id of the developer/deployer principal. When set, grants Key Vault Secrets Officer so an operator can populate secret values out-of-band. Empty disables the role assignment so headless / CI runs do not fail.')
param principalId string = ''

@description('Principal type used in the developer role assignment.')
param principalType string = 'User'

// Built-in role definition ids. See:
// https://learn.microsoft.com/azure/role-based-access-control/built-in-roles
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)
var keyVaultSecretsOfficerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
)

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource apiSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, apiPrincipalId, keyVaultSecretsUserRoleId)
  scope: vault
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleId
  }
}

resource developerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(vault.id, principalId, keyVaultSecretsOfficerRoleId)
  scope: vault
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: keyVaultSecretsOfficerRoleId
  }
}

output vaultUri string = vault.properties.vaultUri
output vaultName string = vault.name
