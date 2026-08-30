param(
  # Also enforce the Azure CLI minimum this repository supports.
  [switch]$Verify
)

# Dot-source this file from a hook so the PATH change survives in the caller:
#   . "$PSScriptRoot\ensure-azure-cli.ps1"
#   . "$PSScriptRoot\ensure-azure-cli.ps1" -Verify
#
# Preprovision preflight, postprovision, agent RBAC, sequential deploy, both
# deployment verifiers, and postdown rotation all shell out to `az` or
# authenticate through AzureCliCredential, so a machine that only has azd must
# acquire the CLI inside `azd up` itself. azd 1.32 owns the per-platform recipe
# for the `az-cli` tool, so bootstrap through it rather than hand-rolling
# winget/brew/apt commands here.

# README "Prerequisites" declares this same floor.
$researchAzureCliMinimumVersion = [version]"2.84.0"

function Test-ResearchAzureCli {
  return [bool](Get-Command az -ErrorAction SilentlyContinue)
}

function Update-ResearchPathFromMachine {
  # winget records the new CLI directory in the machine and user PATH, which a
  # process azd already started never sees. Re-read both scopes so the rest of
  # this azd run resolves the CLI that was just installed.
  if (-not $IsWindows) {
    return
  }
  $scopes = @(
    [Environment]::GetEnvironmentVariable("Path", "Machine"),
    [Environment]::GetEnvironmentVariable("Path", "User")
  ) | Where-Object { $_ }
  if (-not $scopes) {
    return
  }
  $env:PATH = ((@($scopes) + @($env:PATH)) -join [IO.Path]::PathSeparator)
}

if (-not (Test-ResearchAzureCli)) {
  Update-ResearchPathFromMachine
}

if (-not (Test-ResearchAzureCli)) {
  Write-Host "The azd lifecycle requires the Azure CLI; installing it with 'azd tool install az-cli'."
  $researchNativePreference = $PSNativeCommandUseErrorActionPreference
  $PSNativeCommandUseErrorActionPreference = $false
  azd tool install az-cli
  $researchInstallExitCode = $LASTEXITCODE
  $PSNativeCommandUseErrorActionPreference = $researchNativePreference
  if ($researchInstallExitCode -ne 0) {
    throw "'azd tool install az-cli' failed. Install the Azure CLI from https://aka.ms/azure-cli and re-run azd."
  }
  Update-ResearchPathFromMachine
  if (-not (Test-ResearchAzureCli)) {
    throw "azd installed the Azure CLI but 'az' is still not on PATH. Start a new shell, or install it from https://aka.ms/azure-cli, and re-run azd."
  }
  $Verify = $true
}

if ($Verify) {
  $researchAzureCliVersion = (az version --output json | ConvertFrom-Json).'azure-cli'
  if (-not $researchAzureCliVersion) {
    throw "The installed Azure CLI version could not be read from 'az version'."
  }
  if ([version]$researchAzureCliVersion -lt $researchAzureCliMinimumVersion) {
    throw "Azure CLI $researchAzureCliVersion is older than the supported minimum $researchAzureCliMinimumVersion; run 'azd tool update az-cli' and re-run azd."
  }
}
