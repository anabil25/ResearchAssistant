$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$provisionPython = Join-Path $repoRoot ".venv-provision\Scripts\python.exe"
$python = if (Test-Path $provisionPython) { $provisionPython } else { "python" }

Push-Location $repoRoot
$digest = (& $python -m scripts.build_agent_source_tree | Select-Object -Last 1).Trim()
$exitCode = $LASTEXITCODE
Pop-Location
if ($exitCode -ne 0) {
  throw "Hosted Agent committed-source identity generation failed."
}
if ($digest -notmatch '^[0-9a-f]{64}$') {
  throw "Hosted Agent source-tree digest is invalid."
}
azd env set AGENT_SOURCE_TREE_DIGEST $digest
if ($LASTEXITCODE -ne 0) {
  throw "Hosted Agent source-tree digest could not be persisted."
}
