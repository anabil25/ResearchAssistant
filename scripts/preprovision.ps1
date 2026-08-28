$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
Push-Location $repoRoot
python -m scripts.build_agent_source_tree | Out-Null
$sourceIdentityExitCode = $LASTEXITCODE
Pop-Location
if ($sourceIdentityExitCode -ne 0) {
  throw "The complete release must be committed before provisioning."
}

python "$PSScriptRoot\deployment_incarnation.py" ensure
if ($LASTEXITCODE -ne 0) {
  throw "Deployment identity initialization failed."
}

$subscription = azd env get-value AZURE_SUBSCRIPTION_ID
$location = azd env get-value AZURE_LOCATION
$environmentName = azd env get-value AZURE_ENV_NAME
$resourceGroup = $environmentName
$foundryAccount = azd env get-value FOUNDRY_ACCOUNT_NAME
if (-not $subscription -or -not $location -or -not $environmentName -or -not $resourceGroup -or -not $foundryAccount) {
  throw "The azd subscription, location, environment, resource group, and Foundry account must be set."
}
$accountUser = az account show --subscription $subscription --query user --output json | ConvertFrom-Json
$principalType = if ($accountUser.type -eq "user") { "User" } else { "ServicePrincipal" }
$accessToken = az account get-access-token `
  --subscription $subscription `
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
$tenantId = az account show --subscription $subscription --query tenantId --output tsv
if (-not $tenantId) {
  throw "The active Azure tenant id could not be resolved."
}
azd env set AZURE_TENANT_ID $tenantId

$displayLocation = az account list-locations `
  --subscription $subscription `
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
  @{ Provider = "Microsoft.OperationalInsights"; Type = "workspaces" },
  @{ Provider = "Microsoft.Insights"; Type = "components" },
  @{ Provider = "Microsoft.ContainerRegistry"; Type = "registries" }
)

foreach ($provider in @("Microsoft.ApiManagement", "Microsoft.Web")) {
  $state = az provider show --subscription $subscription --namespace $provider --query registrationState --output tsv
  if ($state -ne "Registered") {
    Write-Host "Registering $provider..."
    az provider register --subscription $subscription --namespace $provider --wait
    if ($LASTEXITCODE -ne 0) {
      throw "$provider registration failed."
    }
  }
}

foreach ($required in $requiredResourceTypes) {
  $resourceTypes = az provider show `
    --subscription $subscription `
    --namespace $required.Provider `
    --query "resourceTypes" `
    --output json | ConvertFrom-Json
  $resourceType = $resourceTypes | Where-Object resourceType -eq $required.Type
  if (-not $resourceType -or $resourceType.locations -notcontains $displayLocation) {
    throw "$($required.Provider)/$($required.Type) is not available in $displayLocation."
  }
}

$documentIntelligence = az cognitiveservices account list-skus `
  --subscription $subscription `
  --kind FormRecognizer `
  --location $location `
  --query "[?name=='S0'] | length(@)" `
  --output tsv
if ($documentIntelligence -eq "0") {
  throw "Document Intelligence S0 is not available in $displayLocation."
}

$parameters = Get-Content (Join-Path $repoRoot "infra\main.parameters.json") -Raw | ConvertFrom-Json
foreach ($deployment in $parameters.parameters.deployments.value) {
  $model = $deployment.model
  $available = az cognitiveservices model list --location $location `
    --subscription $subscription `
    --query "[?model.name=='$($model.name)' && model.version=='$($model.version)' && contains(model.skus[].name, '$($deployment.sku.name)')] | length(@)" `
    --output tsv
  if ($available -eq "0") {
    throw "Model $($model.name) version $($model.version) is not available as $($deployment.sku.name) in $location."
  }
}

$existingDeployments = @()
$resourceGroupExists = az group exists `
  --subscription $subscription `
  --name $resourceGroup `
  --output tsv
if ($resourceGroupExists -eq "true") {
  $accountExists = az cognitiveservices account list `
    --subscription $subscription `
    --resource-group $resourceGroup `
    --query "[?name=='$foundryAccount'] | length(@)" `
    --output tsv
  if ($accountExists -eq "1") {
    $existingDeployments = @(
      az cognitiveservices account deployment list `
        --subscription $subscription `
        --resource-group $resourceGroup `
        --name $foundryAccount `
        --output json | ConvertFrom-Json
    )
  }
}

$quotaAttempts = 20
$quotaDelaySeconds = 30
$quotaReady = $false
$lastShortages = @()
for ($attempt = 1; $attempt -le $quotaAttempts; $attempt++) {
  $usage = az cognitiveservices usage list `
    --subscription $subscription `
    --location $location `
    --output json | ConvertFrom-Json
  $shortages = @()
  foreach ($deployment in $parameters.parameters.deployments.value) {
    $model = $deployment.model
    $quotaName = "OpenAI.$($deployment.sku.name).$($model.name)"
    $quota = $usage | Where-Object { $_.name.value -eq $quotaName }
    if (-not $quota) {
      throw "Quota '$quotaName' is not exposed in $displayLocation."
    }
    $existingCapacity = 0.0
    foreach ($existing in $existingDeployments) {
      if (
        $existing.name -eq $deployment.name -and
        $existing.properties.model.name -eq $model.name -and
        $existing.properties.model.version -eq $model.version -and
        $existing.sku.name -eq $deployment.sku.name
      ) {
        $existingCapacity += [double]$existing.sku.capacity
      }
    }
    $requiredCapacity = [Math]::Max(
      0.0,
      [double]$deployment.sku.capacity - $existingCapacity
    )
    $remaining = [double]$quota.limit - [double]$quota.currentValue
    if ($remaining -lt $requiredCapacity) {
      $shortages += (
        "$($model.name) needs $requiredCapacity additional capacity units; " +
        "only $remaining remain"
      )
    }
  }
  if ($shortages.Count -eq 0) {
    $quotaReady = $true
    break
  }
  $lastShortages = $shortages
  if ($attempt -lt $quotaAttempts) {
    Write-Host (
      "Waiting ${quotaDelaySeconds}s for deleted model quota to be released " +
      "($attempt/$quotaAttempts): $($shortages -join '; ')"
    )
    [System.Threading.Thread]::Sleep($quotaDelaySeconds * 1000)
  }
}
if (-not $quotaReady) {
  throw (
    "Model quota did not recover in $displayLocation after $quotaAttempts attempts: " +
    ($lastShortages -join "; ")
  )
}

Write-Host "Azure provider and model preflight passed."
