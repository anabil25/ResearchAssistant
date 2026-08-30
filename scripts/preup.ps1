$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

# azd resolves infra/main.parameters.json before the preprovision hook runs, so the
# deployment identity has to exist by the end of this hook or azd prompts for the
# derived Foundry project name.
$repoRoot = Resolve-Path "$PSScriptRoot\.."
$provisionPython = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
$python = if (Test-Path $provisionPython) { $provisionPython } else { "python" }

$environmentName = $env:AZURE_ENV_NAME
if (-not $environmentName) {
  $PSNativeCommandUseErrorActionPreference = $false
  $environmentName = (azd env get-value AZURE_ENV_NAME 2>$null | Out-String).Trim()
  $PSNativeCommandUseErrorActionPreference = $true
}
if (-not $environmentName) {
  Write-Host "Skipping deployment identity initialization: azd has not selected an environment yet."
  exit 0
}

Push-Location $repoRoot
& $python -m scripts.deployment_incarnation ensure
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "Deployment identity initialization failed."
}
