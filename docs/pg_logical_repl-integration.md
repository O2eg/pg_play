# Logical replication experiments

`pg_play` can orchestrate `pg-logical-repl` through its CLI, Python API and typed
MCP tools. pg-play 0.6.0 requires Python 3.11+ and installs
`pg-logical-repl>=0.2.0,<0.3` as a standard dependency. The replication component
also remains independently installable with `python -m pip install pg-logical-repl`.

See the [migration scenario](user-scenarios.md#6-rehearse-logical-copy-ddl-delivery-and-cutover)
for the component flow. Install the orchestrator and verify the component:

```sh
python -m pip install pg-play
pg-play capabilities --component pg_logical_repl
```

For unreleased sources, follow the [development installation](../README.md#development)
to install all eight component checkouts and pg-play in the same environment.

The migration component owns its local state, PostgreSQL resources, catalog
checks, native plans and recovery rules. pg_play owns the experiment worker,
reviewed input references, event stream and reports. Stopping a worker does not
disable a PostgreSQL subscription. Source and target must currently use the
same PostgreSQL major version, from 15 through 18.

## Endpoint preparation

Start from the native [migration configuration example](https://github.com/O2eg/pg_logical_repl/blob/main/examples/local.yaml)
and [connection prerequisites](https://github.com/O2eg/pg_logical_repl#connections).
Use dedicated laboratory servers: the current profile requires superuser access,
matching major-version client tools, compatible cluster-wide roles and a fresh
capacity measurement file bound to the source/target system identifiers. Obtain
real measurements; copying sample timestamps or free-space numbers does not
satisfy preflight. Unsupported database objects are rejected by native checks.

The controller connects through `source.admin`; the subscriber uses
`source.replication`. A loopback address reachable by the host process may not
be reachable from the target container. Provision networking, both databases and
protected credential references before planning. pg-play does not create those
endpoints from a logical scenario. For two local stands, see
[independent projects](user-scenarios.md#7-use-independent-stands).

## Reviewed operations

Use an existing migration YAML with credential references. Database passwords,
connection strings and SQL bodies are not MCP parameters.

```sh
pg-play logical inspect migration.yaml --operation check
pg-play logical plan migration.yaml --operation prepare > prepare-plan.json
# Review source, target, role changes and the returned plan_hash.
pg-play logical start prepare-plan.json --plan-hash REVIEWED_HASH \
  --output-directory .pg_play/logical --run-id prepare-01
pg-play logical status .pg_play/logical/prepare-01
# Read the report after this operation reaches a terminal state.
pg-play logical report .pg_play/logical/prepare-01
```

Create the `start` plan after `prepare` completes. Each native plan is bound to
the current catalogs and migration state. pg_play binds the configuration,
component version and referenced files to an outer plan hash, then passes the
original native plan hash to the component. These hashes are different.

```sh
# Run only after prepare-01 succeeds; review this new plan independently.
pg-play logical plan migration.yaml --operation start > start-plan.json
pg-play logical start start-plan.json --plan-hash REVIEWED_START_HASH \
  --output-directory .pg_play/logical --run-id start-01
pg-play logical status .pg_play/logical/start-01
```

Use these separate prepare/start operations for a manually controlled migration.
An `experiment` operation performs prepare and start itself; plan it against a
fresh migration instead of first executing this manual sequence.

Other planned operations are `pause`, `resume`, `resolve`, `resync-table`,
`ddl-exec`, `ddl-queue-skip`, `cutover-finalize`, `cutover-commit`, `abort` and
`cleanup`. Their options correspond to the native CLI. For example:

```sh
pg-play logical plan migration.yaml --operation pause \
  --reason 'Maintenance window' --max-duration 60 --wal-rate 1048576
pg-play logical plan migration.yaml --operation resume --reason 'Repair verified'
pg-play logical plan migration.yaml --operation abort \
  --source-unavailable --reason 'Source connection unavailable'
pg-play logical plan migration.yaml --operation cleanup --reason 'Source restored'
pg-play logical plan migration.yaml --operation cutover-finalize --write-fence fence.json
```

Planning does not execute these operations. Execute each reviewed plan using
`logical start`. Finalize requires a fresh record of an actual external write
fence; pg_play does not manufacture a fence or redirect application traffic.
Emergency detachment can leave cleanup pending, which is reported as `partial`.

## Controller sessions

Start a controller after native prepare/start have succeeded. It drives bootstrap
completion and monitoring; it does not provision or prepare a new migration.

```sh
pg-play logical plan migration.yaml --operation controller \
  --duration-seconds 300 --poll-interval-seconds 5 > controller-plan.json
pg-play logical start controller-plan.json --plan-hash REVIEWED_HASH \
  --output-directory .pg_play/logical --run-id observe-01
pg-play logical events .pg_play/logical/observe-01 --after-sequence 0
pg-play logical stop-controller .pg_play/logical/observe-01 --reason 'Stop observation'
# Wait for cancellation/cleanup to finish, then resume only when appropriate.
pg-play logical status .pg_play/logical/observe-01
pg-play logical resume-controller .pg_play/logical/observe-01 --plan-hash REVIEWED_HASH
```

The detached worker invokes bounded `run --once` iterations. It records elapsed
active time, native migration state, lag and WAL-retention samples, and component
events. Only one pg_play controller may own a migration at a time. Native
database locks also protect operations from other clients.

PID start time and the host boot identifier distinguish a live worker from a
stale process record. A lost worker becomes `interrupted`; its recorded CLI
children are terminated only after process identity checks. Resume is explicit.
Cleanup failures remain visible in `cleanup_errors` with `cleanup_pending: true`;
subsequent status requests retry cleanup, and resume waits for it to complete.
`BLOCKED` requires a separate native recovery plan. Once native recovery succeeds,
the same blocked controller or experiment can be resumed. Interrupted one-shot
mutations require inspecting their outcome and reviewing a new operation plan;
they are not automatically replayed by the controller resume command.

`state` describes the pg_play run; `migration_state` describes the migration.
A successful status query can report a blocked migration. A bounded controller
that ends before replication becomes ready returns `partial`.

## Migration experiment

Prepare the infrastructure first using the existing pg_stand/pg_play experiment
workflow. Select dedicated source and empty target databases. Populate the source
and install workload profiles before planning the migration, because prepare
records a baseline of roles and catalogs. Existing target role attributes and
password verifiers must match; pg_play does not normalize arbitrary server roles.
Keep pg_stand's own publication/subscription separate from migration resources.

Create a scenario using [examples/logical/scenario.yaml](../examples/logical/scenario.yaml).
The example below assumes `add-column.sql` already contains one reviewed,
supported DDL statement against a source object; use `ddl_files: []` for a
copy-only rehearsal:

```yaml
schema_version: pg_play/logical-experiment-v1
ddl_files: [add-column.sql]
converter_plans: []
# workload_manifest: ../baseline.yaml
diagnostics: true
oracle: true
quiescent_at_verification: true
readiness_timeout_seconds: 600
finish: replicating
```

Paths are relative to the scenario. Unknown fields and duplicate YAML keys are
rejected. `ddl_files` use the migration component's managed DDL path and require
`managed_only` or `captured` mode. `converter_plans` reference reviewed JSON plans
from `plan-converter-run`; each must select exactly the migration source and
requires `captured` mode. The currently integrated pg_converter release declares
`tracker_may_be_initialized: true` and always submits compound initialization
DDL. Such plans are rejected before migration starts, even if tracker tables
already exist. Execute those packets before prepare, or select supported SQL
files explicitly through `ddl_files`. Live packet support requires a converter
contract guaranteeing that execution will not issue tracker initialization DDL.
Packets must also obey the component's DDL restrictions: arbitrary compound SQL,
`DO` blocks and queued operations within the same transaction are not supported.
An interrupted converter packet requires operator inspection, not automatic replay.

An optional `workload_manifest` selects an already installed pg_workload project.
Its connection must match the migration source. The scenario starts the reviewed
scheduler, then stops it before verification. It does not reinstall profiles or
recreate databases during replication. Use a dedicated workload root for each run.
The report checks the current scheduler-log window for successful completion of
every selected job, failed jobs and resource-guard activation. Starting a scheduler
alone does not prove that the intended workload ran. Missing completion evidence
or a resource guard produces a partial experiment and prevents cutover/abort.

The worker records the scheduler PID, process start time, executable and boot ID
after a successful start, and stops only that process. A competing scheduler is
never stopped by workload-root path. If the start response is lost before process
ownership is recorded, an active scheduler requires operator inspection; cleanup
stays pending until the root is stopped.

```sh
pg-play logical plan migration.yaml --operation experiment \
  --scenario scenario.yaml --duration-seconds 120 > experiment-plan.json
pg-play logical start experiment-plan.json --plan-hash REVIEWED_HASH \
  --output-directory .pg_play/logical --run-id migration-01
```

The workflow prepares the migration, starts native copying, waits for readiness,
runs the workload and reviewed DDL/packets, observes replication, stops its
workload, and verifies the result. Readiness includes the DDL queue and new-table
synchronization. Fresh native plans and completion receipts are saved per stage.
If the worker loses a stage result after the native operation completes, resume
looks for a native completion event matching the exact operation and plan hash.
It reconstructs the local receipt without repeating that operation. An
unconfirmed native outcome still requires inspection and native recovery.

`finish: replicating` leaves the native subscription active. `finish: abort`
performs a planned abort after all checks pass. `finish: cutover` additionally
requires `write_fence: /path/to/fence.json` and performs finalize and commit.
Failures and incomplete evidence prevent those final actions. A partially
completed cutover must be completed or aborted using explicit native operation
plans, rather than restarting the experiment from prepare.

## Evidence and limits

The run directory is private (`0700`), with private state, plans and reports
(`0600`). It contains `plan.json`, `state.json`, `events.jsonl`, `worker.log`,
per-stage receipts, `oracle.json`, diagnostic artifacts and `report.json`.
The final report includes artifact hashes and separate controller/migration
outcomes. It can be read through `logical report` or `logical_report` in MCP.
The report is flushed before a terminal run state is published. Migration state
is refreshed after each native stage and on failure; an unavailable inspection
is reported as unknown with `migration_state_error`, rather than a stale state.

Diagnostics capture both endpoints concurrently using pg_diag's DB-only mode.
The bounded profile collects version, database statistics, sessions, transaction
rate and WAL activity. Capture windows are 30–900 seconds, with five-second
intervals; a shorter experiment waits for the minimum diagnostic window.
Host CPU, disk and memory collection requires the existing SSH-based live
diagnostic workflow. A partial capture does not count as a successful experiment.
Setting `diagnostics: false` or `oracle: false` omits that check; it does not
establish the missing evidence. Keep both enabled for an acceptance rehearsal.

The independent oracle streams exact rows and independently queries catalogs,
roles and ACLs. It does not call the migration engine's checksum or fingerprint
implementation. Connection and credential handling are shared with the optional
component. Row values and password verifiers are never written to comparison
reports. After finalize it also checks deferred objects and sequence values.

Exact comparison requires quiescent source and target databases. The scenario's
`quiescent_at_verification` declaration records that responsibility; it is not a
write fence. Stop external writers separately. Full comparison is intended for
bounded laboratory datasets and may sort/scan entire tables. Passing a local
scenario does not qualify arbitrary databases or cross-major migration.

MCP tools are `logical_inspect`, `logical_plan`, `logical_start`, `logical_status`,
`logical_events`, `logical_stop_controller`, `logical_resume_controller` and
`logical_report`. Python exposes the same workflow through
`PgPlayService().logical`.
