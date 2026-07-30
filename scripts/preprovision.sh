#!/usr/bin/env sh
set -eu

subscription="$(azd env get-value AZURE_SUBSCRIPTION_ID)"
location="$(azd env get-value AZURE_LOCATION)"

if [ -z "$subscription" ] || [ -z "$location" ]; then
  echo "AZURE_SUBSCRIPTION_ID and AZURE_LOCATION must be set." >&2
  exit 1
fi

account_type="$(az account show --query user.type --output tsv)"
if [ "$account_type" = "user" ]; then
  principal_type="User"
else
  principal_type="ServicePrincipal"
fi
access_token="$(az account get-access-token --resource https://management.azure.com/ --query accessToken --output tsv)"
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
tenant_id="$(az account show --query tenantId --output tsv)"
if [ -z "$tenant_id" ]; then
  echo "The active Azure tenant id could not be resolved." >&2
  exit 1
fi
azd env set AZURE_TENANT_ID "$tenant_id"

display_location="$(az account list-locations \
  --query "[?name=='$location'].displayName | [0]" \
  --output tsv)"
if [ -z "$display_location" ]; then
  echo "Azure location '$location' is not recognized." >&2
  exit 1
fi

for provider in Microsoft.ApiManagement Microsoft.Web; do
  state="$(az provider show --namespace "$provider" --query registrationState --output tsv)"
  if [ "$state" != "Registered" ]; then
    echo "Registering $provider..."
    az provider register --namespace "$provider" --wait
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
  supported="$(az provider show --namespace "$provider" \
    --query "resourceTypes[?resourceType=='$resource_type'].locations | [0] | contains(@, '$display_location')" \
    --output tsv)"
  if [ "$supported" != "true" ]; then
    echo "$provider/$resource_type is not available in $display_location." >&2
    exit 1
  fi
done

document_intelligence="$(az cognitiveservices account list-skus \
  --kind FormRecognizer \
  --location "$location" \
  --query "[?name=='S0'] | length(@)" \
  --output tsv)"
if [ "$document_intelligence" = "0" ]; then
  echo "Document Intelligence S0 is not available in $display_location." >&2
  exit 1
fi

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
(cd "$repo_root" && python3 -c '
import json
from pathlib import Path

parameters = json.loads(Path("infra/main.parameters.json").read_text(encoding="utf-8"))
for deployment in parameters["parameters"]["deployments"]["value"]:
    model = deployment["model"]
    print(f"{model['name']}|{model['version']}|{deployment['sku']['name']}|{deployment['sku']['capacity']}")
') | while IFS='|' read -r name version sku capacity; do
  available="$(az cognitiveservices model list --location "$location" \
    --query "[?model.name=='$name' && model.version=='$version' && contains(model.skus[].name, '$sku')] | length(@)" \
    --output tsv)"
  if [ "$available" = "0" ]; then
    echo "Model $name version $version is not available as $sku in $location." >&2
    exit 1
  fi
  quota_name="OpenAI.$sku.$name"
  quota="$(az cognitiveservices usage list --location "$location" \
    --query "[?name.value=='$quota_name'] | [0].[currentValue, limit]" \
    --output tsv)"
  if [ -z "$quota" ]; then
    echo "Quota '$quota_name' is not exposed in $display_location." >&2
    exit 1
  fi
  current="$(printf '%s' "$quota" | cut -f1)"
  limit="$(printf '%s' "$quota" | cut -f2)"
  if ! awk -v current="$current" -v limit="$limit" -v needed="$capacity" \
    'BEGIN { exit ((limit - current) >= needed) ? 0 : 1 }'; then
    echo "Model $name needs $capacity capacity units in $display_location; only $(awk -v c="$current" -v l="$limit" 'BEGIN { print l-c }') remain." >&2
    exit 1
  fi
done

echo "Azure provider and model preflight passed."
