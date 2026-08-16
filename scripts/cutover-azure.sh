#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
cd "$project_root"
environment_file="$project_root/.azure/job-notis.env"
local_database="$HOME/Library/Application Support/job-notis/state.sqlite3"
cutover_complete=false
local_scheduler_stopped=false

[[ -f "$environment_file" ]] || {
  print -u2 -- "Error: run scripts/deploy-azure.sh first."
  exit 1
}
source "$environment_file"

rollback_failed_cutover() {
  if [[ "$cutover_complete" == false && "$local_scheduler_stopped" == true ]]; then
    print -u2 -- "Cloud validation failed; disabling Azure and restoring the local scheduler."
    az functionapp config appsettings set \
      --resource-group "$JOB_NOTIS_RESOURCE_GROUP" \
      --name "$JOB_NOTIS_FUNCTION_APP" \
      --settings AzureWebJobs.job_notis_timer.Disabled=true \
      --output none >/dev/null 2>&1 || true
    "$project_root/.venv/bin/job-notis" install >/dev/null 2>&1 || true
  fi
}
trap rollback_failed_cutover EXIT

[[ -x "$project_root/.venv/bin/job-notis" ]] || {
  print -u2 -- "Error: run uv sync --locked --all-groups first."
  exit 1
}
[[ -f "$local_database" ]] || {
  print -u2 -- "Error: local initialized state is missing."
  exit 1
}

print -- "Keeping the Azure timer disabled while local state is frozen..."
az functionapp config appsettings set \
  --resource-group "$JOB_NOTIS_RESOURCE_GROUP" \
  --name "$JOB_NOTIS_FUNCTION_APP" \
  --settings AzureWebJobs.job_notis_timer.Disabled=true \
  --output none

"$project_root/.venv/bin/job-notis" uninstall
local_scheduler_stopped=true

print -- "Uploading a consistent backup of the final local SQLite state..."
state_before=$(
  uv run python "$project_root/scripts/manage_azure_state.py" \
    --account-url "$JOB_NOTIS_STORAGE_ACCOUNT_URL" \
    --container "$JOB_NOTIS_STATE_CONTAINER" \
    --blob "$JOB_NOTIS_STATE_BLOB" \
    upload --database "$local_database" --replace
)
initialized_before=$(
  STATE="$state_before" uv run python -c 'import json,os; print(json.loads(os.environ["STATE"])["initialized_at"])'
)
cloud_before=$(
  uv run python "$project_root/scripts/manage_azure_state.py" \
    --account-url "$JOB_NOTIS_STORAGE_ACCOUNT_URL" \
    --container "$JOB_NOTIS_STATE_CONTAINER" \
    --blob "$JOB_NOTIS_STATE_BLOB" \
    status
)
etag_before=$(
  STATE="$cloud_before" uv run python -c 'import json,os; print(json.loads(os.environ["STATE"])["blob_etag"])'
)

print -- "Enabling the Azure timer and starting one validation invocation..."
az functionapp config appsettings set \
  --resource-group "$JOB_NOTIS_RESOURCE_GROUP" \
  --name "$JOB_NOTIS_FUNCTION_APP" \
  --settings AzureWebJobs.job_notis_timer.Disabled=false \
  --output none

master_key=$(az functionapp keys list \
  --resource-group "$JOB_NOTIS_RESOURCE_GROUP" \
  --name "$JOB_NOTIS_FUNCTION_APP" \
  --query masterKey --output tsv)
FUNCTION_APP="$JOB_NOTIS_FUNCTION_APP" MASTER_KEY="$master_key" uv run python - <<'PY'
import os
import httpx

response = httpx.post(
    f"https://{os.environ['FUNCTION_APP']}.azurewebsites.net/admin/functions/job_notis_timer",
    headers={"x-functions-key": os.environ["MASTER_KEY"]},
    json={},
    timeout=30,
)
response.raise_for_status()
if response.status_code != 202:
    raise RuntimeError(f"Expected HTTP 202 from timer invocation, got {response.status_code}.")
PY
unset master_key

print -- "Waiting for the cloud invocation to persist state..."
validated=false
for attempt in {1..30}; do
  sleep 5
  if state_after=$(
    uv run python "$project_root/scripts/manage_azure_state.py" \
      --account-url "$JOB_NOTIS_STORAGE_ACCOUNT_URL" \
      --container "$JOB_NOTIS_STATE_CONTAINER" \
      --blob "$JOB_NOTIS_STATE_BLOB" \
      status 2>/dev/null
  ); then
    initialized_after=$(
      STATE="$state_after" uv run python -c 'import json,os; print(json.loads(os.environ["STATE"])["initialized_at"])'
    )
    etag_after=$(
      STATE="$state_after" uv run python -c 'import json,os; print(json.loads(os.environ["STATE"])["blob_etag"])'
    )
    last_check_at=$(
      STATE="$state_after" uv run python -c 'import json,os; print(json.loads(os.environ["STATE"]).get("last_check_at") or "")'
    )
    if [[ "$initialized_after" == "$initialized_before" &&
          "$etag_after" != "$etag_before" &&
          -n "$last_check_at" ]]; then
      validated=true
      break
    fi
  fi
done

[[ "$validated" == true ]] || {
  print -u2 -- "Error: the Azure invocation did not validate within 150 seconds."
  exit 1
}

cutover_complete=true
trap - EXIT
print -- "Cutover complete. Azure owns the schedule; local state remains available for rollback."
print -r -- "$state_after"
