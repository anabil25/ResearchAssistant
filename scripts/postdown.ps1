$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$provisionPython = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
$python = if (Test-Path $provisionPython) { $provisionPython } else { "python" }

Push-Location $repoRoot
& $python -m scripts.deployment_incarnation rotate
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "Deployment identity rotation failed."
}