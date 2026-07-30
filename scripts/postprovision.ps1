$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$venv = Join-Path $repoRoot ".venv-provision"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
  python -m venv $venv
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create provisioning virtual environment."
  }
}

& $python -m pip install --disable-pip-version-check --quiet --upgrade pip
& $python -m pip install --disable-pip-version-check --quiet -r "$PSScriptRoot\requirements-provision.txt"
& $python -m pip install --disable-pip-version-check --quiet -e "$repoRoot\packages\research_core"
Push-Location $repoRoot
& $python -m scripts.postprovision
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "Research corpus provisioning failed."
}
