#!/usr/bin/env sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
. "$script_dir/ensure-azure-cli.sh"
research_ensure_azure_cli
sh "$script_dir/ensure-provision-env.sh"
python="$repo_root/.venv-provision/bin/python"
(cd "$repo_root" && "$python" -m scripts.postprovision)
