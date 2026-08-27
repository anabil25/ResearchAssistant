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

if ($LASTEXITCODE -ne 0) {
  throw "Failed to prepare the provisioning Python environment."
}