$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
. "$PSScriptRoot\ensure-azure-cli.ps1"
& "$PSScriptRoot\ensure-provision-env.ps1"
$python = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
Push-Location $repoRoot
& $python -m scripts.postprovision
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "Postprovision failed before required deployment inputs were ready."
}
