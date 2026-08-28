#!/usr/bin/env sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
python="$repo_root/.venv-provision/bin/python"
if [ ! -x "$python" ]; then
  python="python3"
fi

digest="$(cd "$repo_root" && "$python" -m scripts.build_agent_source_tree)"
case "$digest" in
  *[!0-9a-f]*|'')
    echo "Hosted Agent source-tree digest is invalid." >&2
    exit 1
    ;;
esac
if [ "${#digest}" -ne 64 ]; then
  echo "Hosted Agent source-tree digest is invalid." >&2
  exit 1
fi
azd env set AGENT_SOURCE_TREE_DIGEST "$digest"
