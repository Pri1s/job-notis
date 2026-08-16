# Azure Function Migration Playbook for Local Background Tasks

This is an implementation playbook for Codex or another engineer to follow when moving a script
that currently runs through `launchd`, cron, Task Scheduler, or a long-lived terminal session into
Azure. It uses the Job Notis migration as a reference implementation, but the process applies to
most scheduled Python jobs.

The goal is not merely to make a script run in the cloud. A safe migration must preserve its
schedule, state, secrets, failure behavior, observability, and rollback path without allowing the
local and cloud copies to run at the same time.

## How to use this playbook next time

You should not need to repeat the Azure portal or migration-planning conversation. Open the local
project in Codex and send:

```text
Migrate this project's local background task to an Azure Function by following:
/Users/pri/Projects/Git/job-notis/AZURE_BACKGROUND_JOBS_GUIDE.md

Inspect the project and current scheduler, infer everything that is safely discoverable, implement
the application adapter, tests, Bicep, deployment/cutover/rollback scripts, and documentation, and
validate them locally. Reuse my existing Azure for Students subscription and Central US when
available. Keep the Azure timer disabled until state migration is ready. Preserve local state and
secrets, prevent overlapping local/cloud execution, create cost monitoring, then perform and verify
the safe cutover. Ask me only for information or authorization that cannot be discovered safely.
```

Add any job-specific requirement that matters, such as:

```text
It currently runs every hour. Preserve that schedule and send failures to the existing Slack
webhook. The local state must remain available for rollback.
```

The implementer should inspect the repository, local scheduler, configuration, state, and Azure
account instead of asking the user to transcribe information that is already available. It must
never print or commit recovered secrets.

### Expected outcome

Unless a project-specific limitation makes a different architecture necessary, the completed
migration should include:

- a thin Azure Functions entrypoint around the existing one-shot application logic;
- environment-based cloud configuration with local development fallback;
- private durable state appropriate to the job;
- managed identity and least-privilege Azure RBAC;
- Bicep for repeatable infrastructure;
- a deployment command that leaves the cloud timer disabled;
- transactional cutover and rollback commands;
- Application Insights summaries and failure visibility;
- a small monthly Azure budget with email thresholds;
- tests for configuration, dispatch, state failure paths, packaging, and infrastructure; and
- verified manual and scheduled cloud executions with the local scheduler unloaded.

### What is already set up once

For Pri's current machine and Azure account, the following groundwork was completed during the Job
Notis migration and normally does not need to be repeated:

- Azure CLI is installed.
- The Texas A&M tenant login works through the CLI.
- The `Azure for Students` subscription is active.
- Required resource providers for Functions, Storage, Application Insights, Log Analytics, and
  Cost Management have been registered.
- Central US supports Flex Consumption at the time of the reference deployment.
- The account can create resources, role assignments, and subscription budgets.

The implementer must still verify login, selected subscription, current region availability,
provider state, quota, and current Azure product constraints. Verification is read-only or
idempotent and protects against account or platform changes.

### What remains per background job

Each job still needs its own analysis and application-specific work:

- identify the local scheduler and exact cadence;
- isolate one bounded, non-interactive run;
- identify local-only dependencies;
- choose and validate durable state;
- identify secrets without exposing them;
- define idempotency and failure semantics;
- select globally unique Azure resource names;
- deploy the code and job-specific resources;
- migrate the final state;
- validate manual and scheduled executions; and
- retain a tested rollback path.

Prefer a separate resource group per independently managed job. Resources may technically be
shared, but sharing a Function App, storage container, or deployment lifecycle couples unrelated
jobs and increases the chance that one deployment or cleanup affects another. The Azure
subscription, tenant, registered providers, and operational knowledge are the reusable layer.

### Implementation contract for the agent

When this file is cited in a migration request, the implementer should:

1. Treat the request as authorization to inspect and modify the named local project and to perform
   normal deployment steps in the already selected Azure subscription.
2. Start with read-only discovery of the code, scheduler, state, secrets' locations, current Azure
   login, and subscription. Report secrets only as present or missing.
3. Use current official Azure documentation for product limits, supported runtime versions,
   schedules, deployment APIs, pricing model, and regional availability.
4. Prefer safe assumptions that preserve existing behavior. Ask before changing user-visible
   cadence, filtering, recipients, delivery semantics, or state meaning.
5. Keep the existing local job running while code and disabled cloud infrastructure are prepared.
6. Never initialize a new production baseline when initialized local state exists.
7. Make cutover self-rolling-back until a real cloud invocation persists valid state.
8. Observe at least one scheduled invocation before declaring the migration complete.
9. Leave the local configuration and last local database intact for rollback.
10. Hand off resource names, monitoring locations, cost controls, commands, tests, and any remaining
    operational caveats.

Stop and request direction if the migration requires a materially different service, new paid
third-party account, interactive authentication that only the user can complete, destructive state
conversion, or a user-visible behavioral change. A technical failure is not by itself a reason to
abandon the workflow: first exhaust safe diagnostics and the automatic rollback path.

## 1. Decide whether the job belongs in Azure

Start by writing down:

- What starts the job: a schedule, an event, a queue message, or a manual action?
- How long does one run normally take, and what is the worst case?
- Does it need a GUI, browser profile, VPN, local network, mounted drive, or hardware?
- What state must survive between runs?
- What external systems does it call, and can they be called from an Azure datacenter?
- What happens if the same run executes twice?
- What is the acceptable monthly cost?

Do not migrate a job unchanged if it depends on an interactive desktop session, local browser
cookies, Keychain prompts, a university VPN, USB hardware, or files that exist only on the Mac.
First replace those dependencies with cloud-accessible APIs, managed identities, or durable
storage.

### Choose an Azure runtime

| Workload | Recommended starting point |
| --- | --- |
| Short scheduled or event-driven task that scales to zero | Azure Functions Flex Consumption |
| Containerized scheduled task or task needing OS packages | Azure Container Apps Jobs |
| Multi-step workflow with checkpoints, retries, or long waits | Durable Functions |
| Queue-driven worker with independent work items | Azure Functions or Container Apps Jobs |
| Heavy parallel compute, GPU, or large batch processing | Azure Batch |
| Continuously running service or listener | Container Apps, App Service, or a VM |
| Desktop automation requiring a logged-in GUI | Keep it on a managed workstation |

Azure Functions Flex Consumption is a good default when the job:

- can start from a timer or supported event;
- finishes within the platform's execution constraints;
- can treat the instance filesystem as temporary;
- can restore all required state at the beginning of a run;
- can use environment settings or managed identity for credentials; and
- does not require an always-running process.

Check current Azure limits, regional availability, and pricing before each new project. These can
change independently of this repository.

## 2. Make the script cloud-ready before adding Azure

Separate the useful work from the local scheduler. A recommended Python shape is:

```python
def check(config: Config, paths: AppPaths) -> CheckSummary:
    """Perform exactly one bounded run and return a structured result."""
```

Both the local CLI and Azure entrypoint should call this same function. The core function should
not know about `launchd`, Azure timers, portal settings, or deployment packaging.

Before migration, make one run:

- bounded: it starts, completes, and exits;
- non-interactive: it never waits for a prompt;
- repeatable: retries do not corrupt state;
- observable: it returns counts and records failures;
- testable: external APIs, clocks, and storage can be replaced with fakes;
- configurable: paths, schedules, endpoints, and secrets are not hard-coded; and
- redacted: logs never contain tokens, passwords, webhook URLs, or signed URLs.

Use explicit connect, read, and overall timeouts for every network request. Decide which expected
failures should be persisted and retried, and which unexpected failures should leave the previous
durable state untouched.

## 3. Classify and redesign state

An Azure Function instance can disappear after an invocation. Files written to its temporary disk
must not be treated as durable.

Choose the smallest durable-state pattern that fits:

| State type | Storage option |
| --- | --- |
| No cross-run state | No application storage |
| Small cursor, timestamp, or JSON document | Private Blob Storage |
| Existing small SQLite database updated by one invocation at a time | Compressed SQLite snapshot in Blob Storage |
| Concurrent records or queryable application data | Azure SQL, PostgreSQL, or Cosmos DB |
| Independent work waiting to be processed | Storage Queue or Service Bus |
| Large files or generated artifacts | Blob Storage |

### SQLite snapshot pattern

The Job Notis pattern is appropriate when an existing SQLite database is modest in size and only
one invocation may update it:

1. Acquire a finite lease on the state blob.
2. Start renewing the lease before it expires.
3. Download and decompress the snapshot to temporary local storage.
4. Validate gzip integrity, SQLite integrity, schema version, and initialization metadata.
5. Run the existing one-shot operation against the temporary database.
6. Use SQLite's backup API to create a consistent new database.
7. Validate and gzip the new backup.
8. Replace the blob while presenting the active lease ID.
9. Stop renewal and release the lease in `finally`.

Fail closed when production state is missing, corrupt, uninitialized, or from an unsupported schema.
Never silently create a fresh baseline because doing so can resend old alerts or lose cursors.

A finite lease protects against overlapping application work. Its expiry also provides automatic
recovery if an instance crashes. Design the job for at-least-once execution even when locking is
present: a crash can occur after an external side effect succeeds but before the new state is
uploaded.

### Idempotency and outboxes

For externally visible actions such as sending messages or creating tickets:

- assign a stable identity to every logical event;
- record the pending action before attempting delivery;
- mark it delivered only after the remote service accepts it;
- retry pending actions on later runs; and
- tolerate a rare duplicate when atomic coordination with the external service is impossible.

If duplicates are unacceptable, use a destination that accepts an idempotency key or add a
transactional queue/database architecture rather than relying only on a blob snapshot.

## 4. Design the Function entrypoint

For a Python v2 timer-triggered Function:

```python
import azure.functions as func

app = func.FunctionApp()


@app.timer_trigger(
    schedule="%MY_JOB_TIMER_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def my_job_timer(timer: func.TimerRequest) -> None:
    run_one_cloud_check()
```

Use a six-field NCRONTAB expression:

```text
second minute hour day month day-of-week
```

Azure timer schedules use UTC unless the hosting configuration explicitly and validly supports a
different timezone. Document both the expression and its intended local-time behavior, especially
across daylight-saving transitions.

Keep `run_on_startup` disabled for production jobs. A cold start, deployment, or host restart should
not create an extra business run.

Timer coordination prevents duplicate timer dispatches for the same Function App, but application
state should still have its own concurrency control. Manual invocations, deployment mistakes, or a
second app can otherwise overlap.

Do not add an HTTP trigger unless the job genuinely requires a public API. Azure's authenticated
administrative endpoint can be used for controlled validation of a non-HTTP trigger.

## 5. Handle configuration and secrets

Use three categories:

1. **Non-secret settings:** schedule, feature flags, container name, blob name.
2. **Secret settings:** API tokens, passwords, webhooks, connection strings.
3. **Azure resource access:** prefer a system-managed identity and Azure RBAC.

Use environment variables in Azure and retain a local configuration-file fallback only when local
development or rollback requires it. Azure environment values should take precedence.

Recommended conventions:

```text
MY_JOB_TIMER_SCHEDULE
MY_JOB_STORAGE_ACCOUNT_URL
MY_JOB_STATE_CONTAINER
MY_JOB_STATE_BLOB
MY_JOB_API_TOKEN
AzureWebJobs.my_job_timer.Disabled
```

Never:

- commit `.env` files, local TOML secrets, deployment parameter files, or host keys;
- print secrets as part of deployment output;
- put credentials in Bicep outputs;
- use a storage account key for application state when managed identity can be used; or
- log complete exception strings without checking that libraries have not embedded a secret URL.

For larger deployments, store secrets in Azure Key Vault and grant the Function's managed identity
only the permissions it needs.

## 6. Provision infrastructure as code

Keep the infrastructure beside the application. A typical project contains:

```text
function_app.py
host.json
requirements.txt
src/
infra/
  main.bicep
  budget.bicep
scripts/
  deploy-azure.sh
  cutover-azure.sh
  rollback-azure.sh
tests/
```

For the Job Notis-style architecture, `main.bicep` should provision:

- a private Standard LRS StorageV2 account;
- a private deployment container and state container;
- a Flex Consumption plan with zero always-ready instances;
- a Python 3.12 Function App;
- a system-managed identity;
- container-scoped `Storage Blob Data Contributor`;
- a Log Analytics workspace;
- Application Insights with conservative sampling; and
- the timer setting initially disabled.

Use HTTPS only, TLS 1.2 or newer, disabled FTP, disabled public blob access, and narrowly scoped RBAC.
If the project later needs stricter network isolation, add private endpoints and a compatible
network design deliberately; do not disable public networking before confirming that deployments,
the Functions host, and required outbound calls still work.

Give every project its own resource group unless several jobs deliberately share a lifecycle. A
separate resource group makes cost analysis and cleanup easier.

## 7. Automate deployment

A deployment script should:

1. Check required tools and minimum Azure CLI version.
2. Confirm Azure login, tenant, and subscription.
3. Validate that the selected region supports the intended hosting plan.
4. Register required resource providers.
5. Compile or validate Bicep.
6. Resolve secrets without echoing them.
7. Deploy infrastructure with the timer disabled.
8. Package only runtime files.
9. Use a remote Python build when native wheels may differ from the Mac.
10. Save non-secret deployed identifiers to a gitignored local file.
11. Verify that the Function and trigger were indexed.

Treat deployment and cutover as separate operations. Provisioning or package failure must not stop
the working local scheduler.

Pin Python dependencies and test imports from the same package layout that Azure receives. Avoid
shipping the virtual environment, tests, local databases, logs, `.git`, or secret configuration.

## 8. Perform a safe cutover

Use this order:

1. Deploy the cloud job with its timer disabled.
2. Confirm the cloud trigger exists and Application Insights is connected.
3. Disable and unload the local scheduler without deleting its configuration or state.
4. Capture the final consistent local state.
5. Upload that state to the private cloud store.
6. Enable the cloud timer.
7. Manually invoke one validation run.
8. Verify preserved initialization metadata, cursors, counts, outbox state, and a new cloud
   snapshot version.
9. Observe one genuinely scheduled invocation.
10. Keep the local database as rollback state.

Install an exit trap in the cutover script. Until cloud validation succeeds, any failure should:

1. disable the cloud timer; and
2. restore the local scheduler.

Never allow both schedulers to remain enabled. If the job performs side effects, overlap can create
duplicates even when each environment has its own lock.

## 9. Monitor health and cost

Emit one concise, redacted completion summary per run:

```text
Cloud check complete: 4 source(s), 2 new item(s), 1 delivered, 0 pending;
15213 observations persisted.
```

Also log expected source failures, delivery failures, lease contention, and unexpected exceptions
at appropriate levels.

Monitor four signals:

| Signal | What healthy looks like |
| --- | --- |
| Function invocations | One successful invocation at each scheduled time |
| Application logs | A completion summary and no unhandled exception |
| Durable state | Snapshot version and last-success timestamp advance |
| Cost | Actual spend remains well below the monthly budget |

Useful Application Insights queries:

```kusto
requests
| where timestamp > ago(7d)
| where name == "my_job_timer"
| project timestamp, success, duration, resultCode, operation_Id
| order by timestamp desc
```

```kusto
traces
| where timestamp > ago(24h)
| where message has_any ("complete", "failed", "Skipped")
| project timestamp, severityLevel, message, operation_Id
| order by timestamp desc
```

Create a small monthly budget at the subscription or resource-group scope with alerts to an email
you actively read. A budget is an alerting mechanism, not a hard spending cap. Cost data and budget
evaluation can lag, so also inspect Cost Analysis after the first day and first week.

Consider an Azure Monitor alert when:

- the function has failures;
- no successful run appears for more than two schedule intervals;
- execution duration approaches its normal deadline; or
- the durable state's last-success timestamp becomes stale.

## 10. Prepare rollback before cutover

A rollback script should:

1. disable the Azure timer first;
2. acquire the cloud state lease;
3. download and validate the newest snapshot;
4. back up the current local database;
5. install the cloud snapshot locally;
6. restore local secrets and configuration; and
7. reinstall the local scheduler.

Do not delete Azure resources during rollback. Leave them disabled until the local job has run
successfully, then decide whether to retain or remove the resource group.

Document recovery for a corrupt snapshot separately. Blob soft delete or versioning can help, but a
restored version must still pass application-level validation before use.

## 11. Test before trusting the migration

Keep all existing local tests and add:

- environment-over-local configuration precedence;
- missing and malformed secret rejection;
- secret redaction;
- timer entrypoint dispatch;
- schedule-setting validation;
- lease acquisition, contention, renewal, and release;
- missing and corrupt state rejection;
- schema-version and initialization checks;
- successful snapshot round trips;
- upload failure preserving the prior blob;
- lease release on every exit path;
- expected source and delivery failures being persisted;
- unexpected exceptions not replacing durable state;
- deployment-package import tests;
- Bicep compilation; and
- shell syntax checks for deployment, cutover, and rollback scripts.

Acceptance criteria should use real application invariants, not merely an HTTP success code:

- the migrated state has the expected record counts and initialization timestamp;
- a manual cloud run completes without repeating existing side effects;
- a scheduled cloud run occurs in the next expected window;
- the local scheduler remains unloaded;
- the durable state advances; and
- monitoring and budget alerts are active.

## 12. Per-job migration worksheet

Copy this section into the new project's issue or README:

```markdown
## Cloud migration worksheet

- Job name:
- Current scheduler and schedule:
- Desired Azure schedule in UTC:
- Normal and worst-case runtime:
- Local-only dependencies:
- External APIs and required network access:
- Durable state and current location:
- State migration method:
- Idempotency strategy:
- Secret settings:
- Managed-identity permissions:
- Expected completion summary:
- Failure alerts:
- Monthly budget:
- Local disable command:
- Cloud disable command:
- Manual validation method:
- Rollback command:
- Acceptance record counts/invariants:
```

## Job Notis reference files

This repository provides a working example:

- [`function_app.py`](function_app.py) defines the timer trigger.
- [`src/job_notis/azure_function.py`](src/job_notis/azure_function.py) adapts the existing check.
- [`src/job_notis/azure_state.py`](src/job_notis/azure_state.py) implements leased SQLite snapshots.
- [`infra/main.bicep`](infra/main.bicep) provisions the runtime, storage, identity, and telemetry.
- [`infra/budget.bicep`](infra/budget.bicep) creates cost alerts.
- [`scripts/deploy-azure.sh`](scripts/deploy-azure.sh) provisions and deploys safely.
- [`scripts/cutover-azure.sh`](scripts/cutover-azure.sh) transfers ownership to Azure.
- [`scripts/rollback-azure.sh`](scripts/rollback-azure.sh) restores local execution.

Reuse the architecture and safety properties, but rename every app setting, resource, timer
function, state validator, log message, and budget for the new job. Do not copy Job Notis-specific
schema checks or secrets into an unrelated process.
