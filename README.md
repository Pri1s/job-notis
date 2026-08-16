# Job Notis

Job Notis checks public internship repositories and independent first-party ATS employer boards
every 15 minutes and posts newly discovered matching roles to separate software and hardware
Discord channels. Its primary runtime is an Azure Functions Python
3.12 timer on Flex Consumption, with durable SQLite state stored as a leased, compressed Azure
Blob snapshot.

The existing macOS CLI and LaunchAgent remain available for development and rollback.

To migrate another local background task without repeating this setup process, give Codex the
[Azure Function Migration Playbook](AZURE_BACKGROUND_JOBS_GUIDE.md).

## What it watches

- [vanshb03/Summer2027-Internships][vansh], using structured listings JSON
- [sndsh404/summer-2027-internships][sndsh], using only the main internship table
- [Automated Summer 2027/Fall 2026 internship engine][zshah], using its public JSON API
- [SimplifyJobs/Summer2027-Internships][simplify], using structured listings JSON

It includes software/developer, backend, frontend, full-stack, mobile, platform, infrastructure,
systems, and site-reliability internships or co-ops in the software channel. Firmware, embedded,
RTOS/BSP/bootloader/device-driver, FPGA/ASIC/RTL/UVM, analog/mixed-signal, physical-design, DFT,
STA, SoC, and silicon-validation internships or co-ops go exclusively to the hardware channel.
It keeps US, US-remote, and generic remote postings and preserves explicit citizenship, clearance,
export-control, and sponsorship restrictions as visible eligibility labels.

First-party boards are registered independently for Greenhouse, Ashby, Lever, and SmartRecruiters.
Each board has conditional HTTP validators, retries, pagination, complete-snapshot validation, and
per-host limits. A failed or partial board preserves its prior jobs; a valid empty board closes its
prior jobs.

## Azure architecture

- The timer runs at minutes 7, 22, 37, and 52 of every hour.
- Flex Consumption has no always-ready instances, so it scales to zero between checks.
- The Function downloads `state.sqlite3.gz` under a renewable 60-second blob lease, restores it
  onto local ephemeral disk, runs the existing check, and uploads a new consistent SQLite backup.
- A missing, corrupt, uninitialized, or unsupported database fails closed. Azure never creates a
  production baseline automatically.
- Software and hardware Discord webhooks are encrypted Function App settings and are redacted from
  application logs.
- Application Insights sampling limits telemetry. A $5 monthly budget sends notifications at 50%,
  80%, and 100%.

## Prerequisites

- An active Azure subscription, such as [Azure for Students][azure-students]
- Azure CLI 2.60 or newer
- Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/)
- A Discord incoming webhook
- An initialized local Job Notis database for the first cloud migration

On macOS:

```bash
brew install azure-cli
az login
uv sync --locked --all-groups
```

The deployment defaults to Central US. Override it only when the preflight reports that Flex
Consumption is unavailable:

```bash
export JOB_NOTIS_AZURE_LOCATION=eastus2
```

Set `JOB_NOTIS_BUDGET_EMAIL` if the signed-in Azure account name is not the email that should
receive cost alerts.

## Deploy and cut over

Deployment and cutover are separate so a failed Azure build cannot interrupt the working local
notifier.

```bash
scripts/deploy-azure.sh
scripts/cutover-azure.sh
```

`deploy-azure.sh`:

1. Validates Azure login, CLI version, region availability, configuration, and budget email.
2. Provisions the private storage account, Function App, managed identity access, Application
   Insights, and monthly budget from `infra/`.
3. Builds and remotely deploys the Python package with the timer disabled.
4. Saves non-secret resource identifiers under `.azure/job-notis.env`.

`cutover-azure.sh`:

1. Confirms the Azure timer is disabled.
2. Unloads the local LaunchAgent while preserving its configuration and database.
3. Creates a consistent SQLite backup and uploads it under a finite blob lease.
4. Enables Azure, manually starts one timer invocation, and waits for the state blob ETag and
   `last_check_at` metadata to change.
5. Automatically disables Azure and reinstalls the local LaunchAgent if validation fails.

The original local database remains in place after a successful cutover.

## Operations

Inspect the latest durable cloud state:

```bash
source .azure/job-notis.env
uv run python scripts/manage_azure_state.py \
  --account-url "$JOB_NOTIS_STORAGE_ACCOUNT_URL" \
  --container "$JOB_NOTIS_STATE_CONTAINER" \
  --blob "$JOB_NOTIS_STATE_BLOB" \
  status
```

View recent Function executions:

```bash
az monitor app-insights query \
  --app "$(az resource list --resource-group "$JOB_NOTIS_RESOURCE_GROUP" \
    --resource-type Microsoft.Insights/components --query '[0].name' --output tsv)" \
  --analytics-query \
    "requests | where name == 'job_notis_timer' | order by timestamp desc | take 20"
```

Azure delivery remains at-least-once. A crash after Discord accepts a message but before the
updated snapshot is uploaded can cause a rare duplicate. Source failures and controlled Discord
failures are persisted normally and retried on the next scheduled occurrence.

## Roll back to the Mac

```bash
scripts/rollback-azure.sh
```

Rollback disables the Azure timer first, downloads and validates the newest cloud snapshot, saves
the previous local database under `rollback-backups/`, installs the cloud state locally, and then
reinstalls the LaunchAgent. It never runs the cloud and local schedules concurrently.

## Local CLI

The local workflow remains supported:

```bash
uv run job-notis configure
uv run job-notis init
uv run job-notis test-notification --channel all
uv run job-notis install
uv run job-notis status
```

| Command | Purpose |
| --- | --- |
| `job-notis configure` | Save or replace the software and optional hardware Discord webhooks |
| `job-notis init` | Silently baseline current source records |
| `job-notis check` | Run one local check and deliver pending alerts |
| `job-notis status` | Show local state, source health, outbox, and LaunchAgent health |
| `job-notis test-notification --channel software\|hardware\|all` | Send harmless connection tests |
| `job-notis install` | Install or reload the macOS LaunchAgent |
| `job-notis uninstall` | Remove the LaunchAgent while preserving local state |

Local files:

| Path | Contents |
| --- | --- |
| `~/Library/Application Support/job-notis/config.toml` | Discord webhook, mode `0600` |
| `~/Library/Application Support/job-notis/state.sqlite3` | Jobs, source cursors, and outbox |
| `~/Library/Logs/job-notis/job-notis.log` | Rotating, webhook-redacted diagnostics |
| `~/Library/LaunchAgents/com.pri.job-notis.plist` | Local fallback schedule |

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest
uv lock --check
```

Validate the infrastructure after installing Azure CLI:

```bash
az bicep build --file infra/main.bicep
az bicep build --file infra/budget.bicep
```

[azure-students]: https://azure.microsoft.com/en-us/free/students
[vansh]: https://github.com/vanshb03/Summer2027-Internships
[sndsh]: https://github.com/sndsh404/summer-2027-internships
[zshah]: https://zshah101.github.io/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/api/jobs.json
[simplify]: https://github.com/SimplifyJobs/Summer2027-Internships
