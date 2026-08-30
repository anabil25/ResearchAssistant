#!/usr/bin/env sh
# Dot-source this file from a hook, then call the guard:
#   . "$script_dir/ensure-azure-cli.sh"
#   research_ensure_azure_cli            # make `az` usable
#   research_ensure_azure_cli --verify   # also enforce the supported minimum
#
# Preprovision preflight, postprovision, agent RBAC, sequential deploy, both
# deployment verifiers, and postdown rotation all shell out to `az` or
# authenticate through AzureCliCredential, so a machine that only has azd must
# acquire the CLI inside `azd up` itself. azd 1.32 owns the per-platform recipe
# for the `az-cli` tool, so bootstrap through it rather than hand-rolling
# winget/brew/apt commands here.

# README "Prerequisites" declares this same floor.
RESEARCH_AZURE_CLI_MINIMUM_VERSION='2.84.0'

research_azure_cli_on_path() {
  command -v az >/dev/null 2>&1
}

research_azure_cli_version() {
  az version --output json 2>/dev/null |
    sed -n 's/.*"azure-cli"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' |
    head -n 1
}

research_require_azure_cli_version() {
  research_az_version="$(research_azure_cli_version)"
  if [ -z "$research_az_version" ]; then
    echo "The installed Azure CLI version could not be read from 'az version'." >&2
    return 1
  fi
  research_az_oldest="$(printf '%s\n%s\n' "$research_az_version" \
    "$RESEARCH_AZURE_CLI_MINIMUM_VERSION" | sort -t. -k1,1n -k2,2n -k3,3n | head -n 1)"
  if [ "$research_az_version" != "$RESEARCH_AZURE_CLI_MINIMUM_VERSION" ] &&
    [ "$research_az_oldest" != "$RESEARCH_AZURE_CLI_MINIMUM_VERSION" ]; then
    echo "Azure CLI $research_az_version is older than the supported minimum" \
      "$RESEARCH_AZURE_CLI_MINIMUM_VERSION; run 'azd tool update az-cli' and re-run azd." >&2
    unset research_az_version research_az_oldest
    return 1
  fi
  unset research_az_version research_az_oldest
  return 0
}

research_ensure_azure_cli() {
  research_az_verify=0
  if [ "${1:-}" = "--verify" ]; then
    research_az_verify=1
  fi

  if ! research_azure_cli_on_path; then
    echo "The azd lifecycle requires the Azure CLI; installing it with 'azd tool install az-cli'." >&2
    if ! azd tool install az-cli; then
      echo "'azd tool install az-cli' failed. Install the Azure CLI from" \
        "https://aka.ms/azure-cli and re-run azd." >&2
      unset research_az_verify
      return 1
    fi
    # apt and Homebrew publish `az` into a prefix this process already has on
    # PATH, so a successful install is usable by this hook and by every later
    # one. Anything else is a broken bootstrap, not a fallback path.
    if ! research_azure_cli_on_path; then
      echo "azd installed the Azure CLI but 'az' is still not on PATH. Start a new shell," \
        "or install it from https://aka.ms/azure-cli, and re-run azd." >&2
      unset research_az_verify
      return 1
    fi
    research_az_verify=1
  fi

  if [ "$research_az_verify" -eq 1 ]; then
    unset research_az_verify
    research_require_azure_cli_version || return 1
    return 0
  fi
  unset research_az_verify
  return 0
}
