$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$python = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
if (-not (Test-Path $python)) {
  & "$PSScriptRoot\ensure-provision-env.ps1"
}

Push-Location $repoRoot
& $python -m scripts.configure_agent_rbac
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "Hosted Agent reconciliation failed before API deployment."
}