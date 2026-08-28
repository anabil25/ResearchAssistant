$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$python = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
if (-not (Test-Path $python)) {
  & "$PSScriptRoot\ensure-provision-env.ps1"
}

Push-Location $repoRoot
& $python -m scripts.deploy_sequential
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
  & $python -m scripts.verify_release
  $exitCode = $LASTEXITCODE
}
Pop-Location
if ($exitCode -ne 0) {
  throw "Sequential application deployment or release verification failed."
}