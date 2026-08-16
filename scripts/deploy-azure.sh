#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
cd "$project_root"
resource_group=${JOB_NOTIS_RESOURCE_GROUP:-rg-job-notis}
location=${JOB_NOTIS_AZURE_LOCATION:-centralus}
budget_email=${JOB_NOTIS_BUDGET_EMAIL:-}
azure_dir="$project_root/.azure"
environment_file="$azure_dir/job-notis.env"

fail() {
  print -u2 -- "Error: $1"
  exit 1
}

command -v az >/dev/null || fail "Azure CLI is required. Install it with: brew install azure-cli"
command -v uv >/dev/null || fail "uv is required."
command -v zip >/dev/null || fail "zip is required."

az account show --output none >/dev/null 2>&1 || fail "Sign in first with: az login"

cli_version=$(az version --query '"azure-cli"' --output tsv)
autoload -Uz is-at-least
is-at-least 2.60.0 "$cli_version" || fail "Azure CLI 2.60.0 or newer is required."

supported_locations=$(az functionapp list-flexconsumption-locations --query '[].name' --output tsv)
print -r -- "$supported_locations" | grep -Fxq "$location" ||
  fail "Flex Consumption is unavailable in $location. Set JOB_NOTIS_AZURE_LOCATION."

deployer_id=$(az ad signed-in-user show --query id --output tsv)
[[ -n "$deployer_id" ]] || fail "Could not resolve the signed-in user's object ID."

if [[ -z "$budget_email" ]]; then
  budget_email=$(az account show --query user.name --output tsv)
fi
[[ "$budget_email" == *@* ]] ||
  fail "Set JOB_NOTIS_BUDGET_EMAIL to the email that should receive cost alerts."

webhook=$(
  PROJECT_ROOT="$project_root" uv run python - <<'PY'
import os
from pathlib import Path
from job_notis.config import load_config
from job_notis.paths import default_paths

print(load_config(default_paths()).discord_webhook_url, end="")
PY
)
[[ -n "$webhook" ]] || fail "The local Discord webhook configuration is missing."
hardware_webhook=$(
  PROJECT_ROOT="$project_root" uv run python - <<'PY'
from job_notis.config import load_config
from job_notis.paths import default_paths

print(load_config(default_paths()).hardware_discord_webhook_url or "", end="")
PY
)

print -- "Validating every configured employer board before deployment..."
PROJECT_ROOT="$project_root" uv run python - <<'PY' || fail "One or more configured employer boards failed preflight."
from job_notis.sources import DEFAULT_ADAPTERS, fetch_all

results = fetch_all(retries=0)
failures = [f"{name}: {result.error}" for name, result in results.items() if not result.ok]
if failures:
    raise SystemExit("\n".join(failures))
print(f"Validated {len(DEFAULT_ADAPTERS)} independent sources.")
PY

temporary=$(mktemp -d "${TMPDIR:-/tmp}/job-notis-deploy.XXXXXX")
cleanup() {
  find "$temporary" -type f -delete 2>/dev/null || true
  find "$temporary" -depth -type d -exec rmdir {} \; 2>/dev/null || true
}
trap cleanup EXIT

chmod 700 "$temporary"
parameter_file="$temporary/infra-parameters.json"
PARAMETER_FILE="$parameter_file" WEBHOOK="$webhook" HARDWARE_WEBHOOK="$hardware_webhook" DEPLOYER_ID="$deployer_id" \
  uv run python - <<'PY'
import json
import os
from pathlib import Path

document = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "discordWebhookUrl": {"value": os.environ["WEBHOOK"]},
        "hardwareDiscordWebhookUrl": {"value": os.environ.get("HARDWARE_WEBHOOK", "")},
        "deployerPrincipalId": {"value": os.environ["DEPLOYER_ID"]},
        "timerDisabled": {"value": True},
    },
}
path = Path(os.environ["PARAMETER_FILE"])
path.write_text(json.dumps(document), encoding="utf-8")
path.chmod(0o600)
PY
unset webhook hardware_webhook

print -- "Provisioning Azure resources in $resource_group ($location)..."
az group create --name "$resource_group" --location "$location" --output none
deployment=$(
  az deployment group create \
    --resource-group "$resource_group" \
    --name "job-notis-$(date -u +%Y%m%d%H%M%S)" \
    --template-file "$project_root/infra/main.bicep" \
    --parameters "@$parameter_file" "location=$location" \
    --query properties.outputs \
    --output json
)

function_app=$(
  DEPLOYMENT="$deployment" uv run python -c 'import json,os; print(json.loads(os.environ["DEPLOYMENT"])["functionAppName"]["value"])'
)
storage_url=$(
  DEPLOYMENT="$deployment" uv run python -c 'import json,os; print(json.loads(os.environ["DEPLOYMENT"])["storageAccountUrl"]["value"])'
)
state_container=$(
  DEPLOYMENT="$deployment" uv run python -c 'import json,os; print(json.loads(os.environ["DEPLOYMENT"])["stateContainerName"]["value"])'
)
state_blob=$(
  DEPLOYMENT="$deployment" uv run python -c 'import json,os; print(json.loads(os.environ["DEPLOYMENT"])["stateBlobName"]["value"])'
)

print -- "Creating the monthly Azure budget..."
az deployment sub create \
  --location "$location" \
  --name "job-notis-budget-$(date -u +%Y%m%d%H%M%S)" \
  --template-file "$project_root/infra/budget.bicep" \
  --parameters "contactEmail=$budget_email" \
  --output none

package_root="$temporary/package"
mkdir -p "$package_root"
cp "$project_root/function_app.py" "$project_root/host.json" "$project_root/requirements.txt" \
  "$package_root/"
cp -R "$project_root/src" "$package_root/src"
(
  cd "$package_root"
  zip -qr "$temporary/job-notis-function.zip" .
)

print -- "Deploying the Python Function package with the timer disabled..."
az functionapp deployment source config-zip \
  --resource-group "$resource_group" \
  --name "$function_app" \
  --src "$temporary/job-notis-function.zip" \
  --build-remote true \
  --output none

mkdir -p "$azure_dir"
chmod 700 "$azure_dir"
{
  print -r -- "JOB_NOTIS_RESOURCE_GROUP=${(q)resource_group}"
  print -r -- "JOB_NOTIS_FUNCTION_APP=${(q)function_app}"
  print -r -- "JOB_NOTIS_STORAGE_ACCOUNT_URL=${(q)storage_url}"
  print -r -- "JOB_NOTIS_STATE_CONTAINER=${(q)state_container}"
  print -r -- "JOB_NOTIS_STATE_BLOB=${(q)state_blob}"
  print -r -- "JOB_NOTIS_AZURE_LOCATION=${(q)location}"
} > "$environment_file"
chmod 600 "$environment_file"

print -- "Azure deployment is ready with its timer disabled."
print -- "Run scripts/cutover-azure.sh to migrate state and switch scheduling to Azure."
