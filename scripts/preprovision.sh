#!/usr/bin/env sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
. "$script_dir/ensure-azure-cli.sh"
research_ensure_azure_cli --verify
(cd "$repo_root" && python3 -m scripts.build_agent_source_tree >/dev/null)
python3 "$script_dir/deployment_incarnation.py" ensure

subscription="$(azd env get-value AZURE_SUBSCRIPTION_ID)"
location="$(azd env get-value AZURE_LOCATION)"
environment_name="$(azd env get-value AZURE_ENV_NAME)"
resource_group="$environment_name"
foundry_account="$(azd env get-value FOUNDRY_ACCOUNT_NAME)"

if [ -z "$subscription" ] || [ -z "$location" ] || [ -z "$environment_name" ] || [ -z "$resource_group" ] || [ -z "$foundry_account" ]; then
  echo "The azd subscription, location, environment, resource group, and Foundry account must be set." >&2
  exit 1
fi
account_type="$(az account show --subscription "$subscription" --query user.type --output tsv)"
if [ "$account_type" = "user" ]; then
  principal_type="User"
else
  principal_type="ServicePrincipal"
fi
access_token="$(az account get-access-token --subscription "$subscription" --resource https://management.azure.com/ --query accessToken --output tsv)"
principal_id="$(python3 - "$access_token" <<'PY'
import base64
import json
import sys

payload = sys.argv[1].split('.')[1]
payload += '=' * (-len(payload) % 4)
print(json.loads(base64.urlsafe_b64decode(payload)).get('oid', ''))
PY
)"
if [ -z "$principal_id" ]; then
  echo "The active Azure principal object id could not be resolved." >&2
  exit 1
fi
azd env set AZURE_PRINCIPAL_ID "$principal_id"
azd env set AZURE_PRINCIPAL_TYPE "$principal_type"
tenant_id="$(az account show --subscription "$subscription" --query tenantId --output tsv)"
if [ -z "$tenant_id" ]; then
  echo "The active Azure tenant id could not be resolved." >&2
  exit 1
fi
azd env set AZURE_TENANT_ID "$tenant_id"

display_location="$(az rest \
  --method get \
  --url "https://management.azure.com/subscriptions/$subscription/locations?api-version=2022-12-01" \
  --query "value[?name=='$location'].displayName | [0]" \
  --output tsv)"
if [ -z "$display_location" ]; then
  echo "Azure location '$location' is not recognized." >&2
  exit 1
fi

for provider in Microsoft.ApiManagement Microsoft.Web; do
  state="$(az provider show --subscription "$subscription" --namespace "$provider" --query registrationState --output tsv)"
  if [ "$state" != "Registered" ]; then
    echo "Registering $provider..."
    az provider register --subscription "$subscription" --namespace "$provider" --wait
  fi
done

for resource in \
  "Microsoft.ApiManagement|service" \
  "Microsoft.CognitiveServices|accounts" \
  "Microsoft.Search|searchServices" \
  "Microsoft.App|managedEnvironments" \
  "Microsoft.App|containerApps" \
  "Microsoft.DocumentDB|databaseAccounts" \
  "Microsoft.Storage|storageAccounts" \
  "Microsoft.OperationalInsights|workspaces" \
  "Microsoft.Insights|components" \
  "Microsoft.ContainerRegistry|registries"; do
  provider="${resource%%|*}"
  resource_type="${resource##*|}"
  supported="$(az provider show --subscription "$subscription" --namespace "$provider" \
    --query "resourceTypes[?resourceType=='$resource_type'].locations | [0] | contains(@, '$display_location')" \
    --output tsv)"
  if [ "$supported" != "true" ]; then
    echo "$provider/$resource_type is not available in $display_location." >&2
    exit 1
  fi
done

document_intelligence="$(az cognitiveservices account list-skus \
  --subscription "$subscription" \
  --kind FormRecognizer \
  --location "$location" \
  --query "[?name=='S0'] | length(@)" \
  --output tsv)"
if [ "$document_intelligence" = "0" ]; then
  echo "Document Intelligence S0 is not available in $display_location." >&2
  exit 1
fi

model_rows="$(mktemp)"
existing_deployments="$(mktemp)"
trap 'rm -f "$model_rows" "$existing_deployments"' EXIT HUP INT TERM
printf '[]\n' > "$existing_deployments"
resource_group_exists="$(az group exists --subscription "$subscription" --name "$resource_group" --output tsv)"
if [ "$resource_group_exists" = "true" ]; then
  account_exists="$(az cognitiveservices account list \
    --subscription "$subscription" \
    --resource-group "$resource_group" \
    --query "[?name=='$foundry_account'] | length(@)" \
    --output tsv)"
  if [ "$account_exists" = "1" ]; then
    az cognitiveservices account deployment list \
      --subscription "$subscription" \
      --resource-group "$resource_group" \
      --name "$foundry_account" \
      --output json > "$existing_deployments"
  fi
fi
if ! (cd "$repo_root" && python3 - "$existing_deployments" <<'PY'
import json
import sys
from pathlib import Path

parameters = json.loads(Path("infra/main.parameters.json").read_text(encoding="utf-8"))
existing = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for deployment in parameters["parameters"]["deployments"]["value"]:
    model = deployment["model"]
    existing_capacity = sum(
        item.get("sku", {}).get("capacity", 0)
        for item in existing
        if item.get("name") == deployment["name"]
        and item.get("properties", {}).get("model", {}).get("name") == model["name"]
        and item.get("properties", {}).get("model", {}).get("version") == model["version"]
        and item.get("sku", {}).get("name") == deployment["sku"]["name"]
    )
    required = max(0, deployment["sku"]["capacity"] - existing_capacity)
    print(
        f"{deployment['name']}|{model['name']}|{model['version']}|"
        f"{deployment['sku']['name']}|{deployment['sku']['capacity']}|{required}"
    )
PY
) > "$model_rows"; then
  echo "Model deployment parameters could not be parsed." >&2
  exit 1
fi
if [ ! -s "$model_rows" ]; then
  echo "At least one model deployment is required." >&2
  exit 1
fi

while IFS='|' read -r _deployment_name name version sku _capacity _required; do
  available="$(az cognitiveservices model list --location "$location" \
    --subscription "$subscription" \
    --query "[?model.name=='$name' && model.version=='$version' && contains(model.skus[].name, '$sku')] | length(@)" \
    --output tsv)"
  if [ "$available" = "0" ]; then
    echo "Model $name version $version is not available as $sku in $location." >&2
    exit 1
  fi
done < "$model_rows"

quota_attempt=1
quota_attempts=20
quota_delay_seconds=30
while [ "$quota_attempt" -le "$quota_attempts" ]; do
  quota_ready=true
  quota_shortages=""
  while IFS='|' read -r _deployment_name name _version sku _capacity required; do
    quota_name="OpenAI.$sku.$name"
    quota="$(az cognitiveservices usage list --location "$location" \
      --subscription "$subscription" \
      --query "[?name.value=='$quota_name'] | [0].[currentValue, limit]" \
      --output tsv)"
    if [ -z "$quota" ]; then
      echo "Quota '$quota_name' is not exposed in $display_location." >&2
      exit 1
    fi
    current="$(printf '%s' "$quota" | cut -f1)"
    limit="$(printf '%s' "$quota" | cut -f2)"
    if ! awk -v current="$current" -v limit="$limit" -v needed="$required" \
      'BEGIN { exit ((limit - current) >= needed) ? 0 : 1 }'; then
      remaining="$(awk -v c="$current" -v l="$limit" 'BEGIN { print l-c }')"
      quota_ready=false
      quota_shortages="${quota_shortages}${quota_shortages:+; }$name needs $required additional capacity units; only $remaining remain"
    fi
  done < "$model_rows"
  if [ "$quota_ready" = true ]; then
    break
  fi
  if [ "$quota_attempt" -eq "$quota_attempts" ]; then
    echo "Model quota did not recover in $display_location after $quota_attempts attempts: $quota_shortages" >&2
    exit 1
  fi
  echo "Waiting ${quota_delay_seconds}s for deleted model quota to be released ($quota_attempt/$quota_attempts): $quota_shortages"
  sleep "$quota_delay_seconds"
  quota_attempt=$((quota_attempt + 1))
done
rm -f "$model_rows" "$existing_deployments"
trap - EXIT HUP INT TERM

echo "Azure provider and model preflight passed."
