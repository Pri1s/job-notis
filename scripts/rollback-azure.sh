#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
cd "$project_root"
environment_file="$project_root/.azure/job-notis.env"
local_root="$HOME/Library/Application Support/job-notis"
local_database="$local_root/state.sqlite3"

[[ -f "$environment_file" ]] || {
  print -u2 -- "Error: Azure deployment metadata is missing."
  exit 1
}
source "$environment_file"

print -- "Disabling the Azure timer before retrieving its latest durable state..."
az functionapp config appsettings set \
  --resource-group "$JOB_NOTIS_RESOURCE_GROUP" \
  --name "$JOB_NOTIS_FUNCTION_APP" \
  --settings AzureWebJobs.job_notis_timer.Disabled=true \
  --output none

temporary=$(mktemp -d "${TMPDIR:-/tmp}/job-notis-rollback.XXXXXX")
cleanup() {
  find "$temporary" -type f -delete 2>/dev/null || true
  rmdir "$temporary" 2>/dev/null || true
}
trap cleanup EXIT

cloud_database="$temporary/state.sqlite3"
uv run python "$project_root/scripts/manage_azure_state.py" \
  --account-url "$JOB_NOTIS_STORAGE_ACCOUNT_URL" \
  --container "$JOB_NOTIS_STATE_CONTAINER" \
  --blob "$JOB_NOTIS_STATE_BLOB" \
  download --output "$cloud_database"

"$project_root/.venv/bin/job-notis" uninstall >/dev/null 2>&1 || true
mkdir -p "$local_root/rollback-backups"
if [[ -f "$local_database" ]]; then
  backup_name="$local_root/rollback-backups/state-$(date -u +%Y%m%d%H%M%S).sqlite3"
  cp "$local_database" "$backup_name"
  chmod 600 "$backup_name"
fi
local_temporary="${local_database}.rollback"
cp "$cloud_database" "$local_temporary"
chmod 600 "$local_temporary"
mv "$local_temporary" "$local_database"
rm -f "${local_database}-wal" "${local_database}-shm"
"$project_root/.venv/bin/job-notis" install

print -- "Rollback complete. The local LaunchAgent owns the schedule again."
