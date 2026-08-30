#!/usr/bin/env sh
set -eu

# azd resolves infra/main.parameters.json before the preprovision hook runs, so the
# deployment identity has to exist by the end of this hook or azd prompts for the
# derived Foundry project name.
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"

# preup is the first hook `azd up` runs, so it is the only place that can install
# the Azure CLI before preprovision's ARM preflight shells out to it. Run it
# ahead of the environment check so a bare-azd machine is repaired either way.
. "$script_dir/ensure-azure-cli.sh"
research_ensure_azure_cli --verify

python="$repo_root/.venv-provision/bin/python"
if [ ! -x "$python" ]; then
  python="python3"
fi

environment_name="${AZURE_ENV_NAME:-}"
if [ -z "$environment_name" ]; then
  environment_name="$(azd env get-value AZURE_ENV_NAME 2>/dev/null || true)"
fi
if [ -z "$environment_name" ]; then
  echo "Skipping deployment identity initialization: azd has not selected an environment yet." >&2
  exit 0
fi

(cd "$repo_root" && "$python" -m scripts.deployment_incarnation ensure)
