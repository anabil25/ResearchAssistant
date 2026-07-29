$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$subscription = azd env get-value AZURE_SUBSCRIPTION_ID
$location = azd env get-value AZURE_LOCATION
if (-not $subscription -or -not $location) {
  throw "AZURE_SUBSCRIPTION_ID and AZURE_LOCATION must be set in the azd environment."
}

$accountUser = az account show --query user --output json | ConvertFrom-Json
$principalType = if ($accountUser.type -eq "user") { "User" } else { "ServicePrincipal" }
$accessToken = az account get-access-token `
  --resource https://management.azure.com/ `
  --query accessToken `
  --output tsv
$payload = $accessToken.Split('.')[1].Replace('-', '+').Replace('_', '/')
$payload = $payload.PadRight($payload.Length + ((4 - ($payload.Length % 4)) % 4), '=')
$principalId = (
  [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($payload)
  ) | ConvertFrom-Json
).oid
if (-not $principalId) {
  throw "The active Azure principal object id could not be resolved."
}
azd env set AZURE_PRINCIPAL_ID $principalId
azd env set AZURE_PRINCIPAL_TYPE $principalType
$tenantId = az account show --query tenantId --output tsv
if (-not $tenantId) {
  throw "The active Azure tenant id could not be resolved."
}
azd env set AZURE_TENANT_ID $tenantId

$displayLocation = az account list-locations `
  --query "[?name=='$location'].displayName | [0]" `
  --output tsv
if (-not $displayLocation) {
  throw "Azure location '$location' is not recognized."
}

$requiredResourceTypes = @(
  @{ Provider = "Microsoft.ApiManagement"; Type = "service" },
  @{ Provider = "Microsoft.CognitiveServices"; Type = "accounts" },
  @{ Provider = "Microsoft.Search"; Type = "searchServices" },
  @{ Provider = "Microsoft.App"; Type = "managedEnvironments" },
  @{ Provider = "Microsoft.App"; Type = "containerApps" },
  @{ Provider = "Microsoft.DocumentDB"; Type = "databaseAccounts" },
  @{ Provider = "Microsoft.Storage"; Type = "storageAccounts" },
  @{ Provider = "Microsoft.DurableTask"; Type = "schedulers" },
  @{ Provider = "Microsoft.OperationalInsights"; Type = "workspaces" },
  @{ Provider = "Microsoft.Insights"; Type = "components" },
  @{ Provider = "Microsoft.ContainerRegistry"; Type = "registries" }
)

foreach ($provider in @("Microsoft.ApiManagement", "Microsoft.Web", "Microsoft.DurableTask")) {
  $state = az provider show --namespace $provider --query registrationState --output tsv
  if ($state -ne "Registered") {
    Write-Host "Registering $provider..."
    az provider register --namespace $provider --wait
    if ($LASTEXITCODE -ne 0) {
      throw "$provider registration failed."
    }
  }
}

foreach ($required in $requiredResourceTypes) {
  $resourceTypes = az provider show `
    --namespace $required.Provider `
    --query "resourceTypes" `
    --output json | ConvertFrom-Json
  $resourceType = $resourceTypes | Where-Object resourceType -eq $required.Type
  if (-not $resourceType -or $resourceType.locations -notcontains $displayLocation) {
    throw "$($required.Provider)/$($required.Type) is not available in $displayLocation."
  }
}

$documentIntelligence = az cognitiveservices account list-skus `
  --kind FormRecognizer `
  --location $location `
  --query "[?name=='S0'] | length(@)" `
  --output tsv
if ($documentIntelligence -eq "0") {
  throw "Document Intelligence S0 is not available in $displayLocation."
}

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$parameters = Get-Content (Join-Path $repoRoot "infra\main.parameters.json") -Raw | ConvertFrom-Json
$usage = az cognitiveservices usage list --location $location --output json | ConvertFrom-Json
foreach ($deployment in $parameters.parameters.deployments.value) {
  $model = $deployment.model
  $available = az cognitiveservices model list --location $location `
    --query "[?model.name=='$($model.name)' && model.version=='$($model.version)' && contains(model.skus[].name, '$($deployment.sku.name)')] | length(@)" `
    --output tsv
  if ($available -eq "0") {
    throw "Model $($model.name) version $($model.version) is not available as $($deployment.sku.name) in $location."
  }
  $quotaName = "OpenAI.$($deployment.sku.name).$($model.name)"
  $quota = $usage | Where-Object { $_.name.value -eq $quotaName }
  if (-not $quota) {
    throw "Quota '$quotaName' is not exposed in $displayLocation."
  }
  $remaining = [double]$quota.limit - [double]$quota.currentValue
  if ($remaining -lt [double]$deployment.sku.capacity) {
    throw "Model $($model.name) needs $($deployment.sku.capacity) capacity units in $displayLocation; only $remaining remain."
  }
}

Write-Host "Azure provider and model preflight passed."
