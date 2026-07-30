#!/usr/bin/env sh
# set -eu removed so a postprovision failure warns rather than aborting azd up.
set -u

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
venv="$repo_root/.venv-provision"
python="$venv/bin/python"

if [ ! -x "$python" ]; then
  python3 -m venv "$venv"
fi

"$python" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$python" -m pip install --disable-pip-version-check --quiet -r "$script_dir/requirements-provision.txt"
"$python" -m pip install --disable-pip-version-check --quiet -e "$repo_root/packages/research_core"
(cd "$repo_root" && "$python" -m scripts.postprovision) || echo "WARNING: postprovision exited non-zero — re-run 'python -m scripts.postprovision' after deploy completes."
