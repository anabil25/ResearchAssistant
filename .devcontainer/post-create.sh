#!/usr/bin/env sh
# Installs the toolchain `azd up` needs that the base image and features do not carry.
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"

# azure.yaml pins these; azd refuses to deploy hosted agents without them.
azd extension install azure.ai.agents --version 1.0.0-beta.12
azd extension install azure.ai.projects --version 1.0.0-beta.7
azd extension install microsoft.foundry --version 1.0.0-beta.2

# The release gate downloads Chromium itself but cannot install its OS libraries.
playwright_version="$(node -p "require('$repo_root/apps/web/package.json').devDependencies['@playwright/test']" 2>/dev/null || echo "")"
if [ -n "$playwright_version" ]; then
    sudo npx --yes "playwright@${playwright_version}" install-deps chromium
fi

printf '\nToolchain:\n'
printf '  azd     %s\n' "$(azd version | head -n 1)"
printf '  az      %s\n' "$(az version --query '\"azure-cli\"' --output tsv 2>/dev/null || echo unknown)"
printf '  python  %s\n' "$(python3 --version)"
printf '  node    %s\n' "$(node --version)"
printf '\nSign in with "azd auth login" and "az login", then run "azd up".\n'
