#!/usr/bin/env sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
python="$repo_root/.venv-provision/bin/python"

if [ ! -x "$python" ]; then
  sh "$script_dir/ensure-provision-env.sh"
fi

(cd "$repo_root" && "$python" -m scripts.configure_agent_rbac)