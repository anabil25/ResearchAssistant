param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("api", "web")]
  [string]$Service
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
. "$PSScriptRoot\ensure-azure-cli.ps1"
$provisionPython = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
$python = if (Test-Path $provisionPython) { $provisionPython } else { "python" }

Push-Location $repoRoot
& $python -m scripts.verify_deployment $Service
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "$Service deployment verification failed."
}