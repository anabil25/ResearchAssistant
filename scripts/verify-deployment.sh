#!/usr/bin/env sh
set -eu

service="${1:?service argument is required}"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
. "$script_dir/ensure-azure-cli.sh"
research_ensure_azure_cli
python="$repo_root/.venv-provision/bin/python"

if [ ! -x "$python" ]; then
  python="python3"
fi

(cd "$repo_root" && "$python" -m scripts.verify_deployment "$service")