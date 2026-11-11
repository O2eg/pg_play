# pg-play

`pg-play` is the AI-ready orchestration layer for reproducible PostgreSQL
experiments.

The distribution and primary installed command are `pg-play`; the MCP command
is `pg-play-mcp`. The import package and GitHub repository are named `pg_play`.

- Recreates the same PostgreSQL stand, backend activity, and diagnostic window.
- Applies a versioned `pg_configurator` candidate only through a reviewed
  `pg_stand` plan.
- Keeps human-facing component CLIs small while exposing a typed MCP workflow
  to an AI agent.
- Records immutable plan hashes, durable run state, append-only events,
  component versions, and artifact hashes.
- Validates and compares `pg_diag` JSON reports without treating a partial
  collection as a successful complete run.
- Plans and runs `pg_perf_bench` only against an explicitly selected disposable
  database, then validates and compares benchmark artifacts and TPS evidence.
- Plans and runs reviewed `pg_converter` packets against an exact alias set
  through a detached, auditable, sequential workflow.
- Installs all eight component distributions while preserving
  their independent use.
- Optionally builds PostgreSQL through pg_maker, records package/image provenance,
  and runs the resulting immutable image in pg_stand. See
  [the maker workflow and interface contract](docs/pg_maker-integration.md).
- Optionally orchestrates logical migration experiments with `pg-logical-repl`:
  durable controllers, DDL delivery, paired diagnostics, independent verification,
  planned recovery and cutover. See [the logical integration guide](docs/pg_logical_repl-integration.md).

The eight independently installable components are listed below. Distribution
and CLI names use hyphens; Python packages, repository names and component IDs
use underscores (for example, `pg-maker` and `pg_maker`).

| Component | Role | Installation with pg-play |
| --- | --- | --- |
| [pg-stand](https://github.com/O2eg/pg_stand) | PostgreSQL containers, topology, storage and credentials | Core |
| [pg-workload](https://github.com/O2eg/pg_workload) | Datasets and scheduled backend activity | Core |
| [pg-diag](https://github.com/O2eg/pg_diag) | Database/OS diagnostics and JSON/HTML reports | Core |
| [pg-configurator](https://github.com/O2eg/pg_configurator) | Version-aware PostgreSQL configuration candidates | Core |
| [pg-converter](https://github.com/O2eg/pg_converter) | Reviewed SQL/Python packets against selected database aliases | Core |
| [pg-perf-bench](https://github.com/O2eg/pg_perf_bench) | Controlled pgbench measurements and benchmark comparisons | Core |
| [pg-maker](https://github.com/O2eg/pg_maker) | PostgreSQL builds, extensions, patches, package evidence and publication | Core |
| [pg-logical-repl](https://github.com/O2eg/pg_logical_repl) | Native logical replication, supported DDL delivery, verification and migration recovery | Core |

## Documentation

- [User scenarios](docs/user-scenarios.md): tasks, component flows and ASCII diagrams.
- [Maker integration](docs/pg_maker-integration.md): builds, runtime images and publication.
- [Logical replication integration](docs/pg_logical_repl-integration.md): copy, DDL, verification and recovery.
- [Converter integration](docs/pg_converter-integration.md): reviewed packet execution.
- [Agent client setup](docs/agent-client-setup.md): MCP and the six packaged skills.
- [Changelog](CHANGELOG.md): release history; 0.6.0 is currently unreleased.

## How it works

```text
                   pg-play: plan -> run -> evidence
                                  |
                    +-------------+-------------+
                    |                           |
                pg-maker                 pg-configurator
              packages/image                settings
                    |                           |
                    +--------> pg-stand <-------+
                                  |
                                  v
             pg-converter ---> PostgreSQL <--- pg-workload
                                  ^
                                  +----------> pg-perf-bench
                                  |
                                  +----------> pg-diag -> JSON / HTML
                                  |
                source DB == pg-logical-repl ==> target DB
                              copy + DML +
                              supported DDL
```

These are available workflows, not one unconditional pipeline. A standard
experiment runs the optional benchmark before workload diagnostics. Converter
packets and logical migrations have separate reviewed plans. Live diagnostics
and configuration review can use existing servers without pg-stand.

The control layers are deliberately separate:

```text
agent skills          workflow and interpretation rules
      |
      v
pg-play-mcp           typed, high-level operations
      |
      v
pg_play core          validation, planning, state, comparison
      |
      v
component adapters    argv arrays + strict JSON envelopes
      |
      +---- pg_configurator
      +---- pg_converter
      +---- pg_maker
      +---- pg_logical_repl
      +---- pg_stand
      +---- pg_workload
      +---- pg_diag
      `---- pg_perf_bench
```

MCP exposes typed workflow operations. The `maker_dev_exec` tool runs
an argument vector inside a private copy of a pg_maker build container; it does
not provide a host shell or a database SQL console. Component CLIs remain
independently usable. The logical adapter explicitly selects
`--contract pg_play/component/v2`; native pg-logical-repl machine output has a
different contract.

## Installation

Use Linux and Python 3.11 or newer. Managed stands and maker builds need Docker
Engine access; stand credential generation also needs OpenSSL and `ssh-keygen`.
Benchmark/workload execution needs PostgreSQL client tools on the runner host.
Logical migration requires `psql`, `pg_dump` and `pg_restore` matching the
source/target major. Remote OS diagnostics need SSH access and verified host
keys. Python installation does not provision these system dependencies.

```bash
python -m pip install pg-play
```

Starting with pg-play 0.6.0, this installs all eight component distributions.
The minimum versions are:

| Distribution | Required version |
| --- | --- |
| pg-configurator | `>=0.11.1,<1` |
| pg-converter | `>=2.0b2,<3` |
| pg-diag | `>=0.19.1,<1` |
| pg-logical-repl | `>=0.2.0,<0.3` |
| pg-maker | `>=0.6.0,<1` |
| pg-perf-bench | `>=0.7.4,<1` |
| pg-stand | `>=0.3.0,<1` |
| pg-workload | `>=0.6.1,<1` |

The wheel and source distribution include the README, changelog, public guides
and example configurations. Installed documents retain their relative layout
under `share/doc/pg-play`. Locate them in the active environment with:

```bash
python -c 'import sysconfig; print(sysconfig.get_path("data") + "/share/doc/pg-play")'
```

Each component remains a separate Python distribution with its own CLI and
release cycle. It can be installed independently, for example
`python -m pip install pg-stand` or `python -m pip install pg-logical-repl`.
Components do not depend on the pg-play orchestrator; their own dependencies
still apply, such as pg-perf-bench using pg-diag.

Newer compatible versions are allowed. To update an existing installation and
its components together:

```bash
python -m pip install --upgrade --upgrade-strategy eager pg-play
pg-play capabilities
```

Maker and logical replication are required dependencies; installation extras
for those components are not provided. Workflows still check component
capabilities before execution. Maker runtime preflight requires `runtime check`,
included in the required 0.6.0 release. See the
[maker integration guide](docs/pg_maker-integration.md),
[logical integration guide](docs/pg_logical_repl-integration.md) and
[changelog](CHANGELOG.md).

The components remain available through their own commands:

```bash
pg-stand --help
pg-workload --help
pg-diag --help
pg-configurator --help
pg-converter --help
pg-perf-bench --help
pg-maker --help
pg-logical-repl --help
```

## Experiment manifest

`pg_play/v1` is strict: unknown fields, secret-bearing configurator inputs,
missing projects, invalid profile names, and invalid diagnostic windows are
errors.

Initialize `stand` and `workload` as shown in the CLI flow below, and save this
example as `experiment.yaml` in their parent directory. Its CPU/RAM values match
the bundled PG18 single-node profile (1 CPU, 1 GiB). When changing stand resources
or the PostgreSQL major, update the configurator inputs to match. This is a small
laboratory example; the workload guard checks the host's available resources.

```yaml
api_version: pg_play/v1
kind: PostgreSQLExperiment
metadata:
  id: pg18-mixed-baseline
spec:
  artifact_root: .pg_play/experiments/pg18-mixed-baseline
  stand:
    config: ./stand/configs/single.yaml
    project: ./stand
    # Optional values applied after pg_configurator recommendations:
    # parameter_overrides:
    #   auto_explain.log_min_duration: 0ms
    #   auto_explain.log_format: json
  configurator:
    inputs:
      db_cpu: 1
      db_ram: 1Gi
      pg_version: "18"
      db_duty: mixed
  workload:
    project: ./workload
    profiles: [pagila, simple_stock]
    scale: 1.0
    database: workload_db
    user: workload_user
    install: true
    stop_after_report: true
    pgbench_duration_seconds: 30
    job_interval_seconds: 5
    resource_guard:
      disk_max_used_pct: 90
      mem_min_available_pct: 10
      mem_min_available_mb: 2048
      cpu_max_pct: 90
      cpu_window_seconds: 60
      check_interval: 5
  diagnostics:
    mode: snapshots
    collection_mode: remote-db-only
    duration_seconds: 60
    interval_seconds: 10
    report_name: pg18-mixed-diagnostics
    # log_depth_time_min: 15         # optional: parse server csvlog for the
                                     # last N minutes (0-1440); needs
                                     # collection_mode local or remote, since
                                     # remote-db-only never reaches the log dir
  benchmark:                         # optional
    database: pg_perf_bench_test     # dedicated and disposable
    report_name: pg18-mixed-benchmark
    benchmark_type: default
    clients: [1, 4, 16]              # or times_seconds, never both
    init_command: >-
      ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT
      -U ARG_PG_USER ARG_PG_DATABASE
    workload_command: >-
      ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS
      -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE
    command_timeout: 120
    system_metrics_interval: 5
    drop_os_caches: false
    collect_pg_logs: true
  phases:
    benchmark: true
    workload_diagnostics: true
    recreate_workload_database: true
```

To use a packaged `pg_perf_bench` maximum-TPS profile, replace
`benchmark_type`, `init_command`, `workload_command`, and `workload_path` with:

```yaml
    workload_profile: imdb            # imdb or pagila
    workload_scale: 1.0
    workload_duration_seconds: 30
    clients: [1, 2, 4, 8, 16]
```

The profile supplies its schema, deterministic generator, SQL query set and
command templates. `pg_play` includes the selected profile and scale in the
reviewed benchmark plan. `pg_perf_bench` selects the newest local pgbench/psql
pair automatically; optional `pgbench_path` and `psql_path` overrides are
accepted only when they are not older than the newest installed clients. The
pg_diag OS sampler runs on the benchmark runner host during every benchmark
window. `system_metrics_interval` defaults to 5 seconds, the shortest interval
pg-perf-bench accepts; a smaller value is rejected during validation. Use
`system_metrics_duration` only when a custom command has no pgbench `-T` or
`--time` option.

Paths are resolved relative to the manifest. `spec.stand.project` defaults to
the manifest directory and fixes where `pg_stand` stores state, credentials,
and storage regardless of the caller's current directory. Credentials never
belong in this file. `pg_play` obtains the stand-owned administrator credential, creates a
random workload credential, and stores project-local passfiles with mode
`0600`.

`spec.stand.parameter_overrides` replaces selected generated PostgreSQL values.
Use it for an experiment that needs specific logging thresholds or connection
limits. Overrides pass the same validation as `pg_stand` parameters: topology,
TLS, fixed CSV paths and preloaded libraries remain stand-managed. The effective
values and explicit overrides are included in the reviewed plan and its hash;
the configurator artifact hash identifies the original generated candidate.
Configurator safety budgets describe that candidate before manual overrides.

The workload resource guard is always enabled by `pg_play`. Its thresholds are
manifest inputs and therefore part of the reviewed plan hash; override them
only when the host policy is explicitly known.

The packaged JSON Schema is available as the MCP resource
`pgplay://experiment-schema`.

## Shared CLI conventions

`pg_diag` naming is the reference for equivalent options. Components now use
`--host`, `--port`, `--database`, `--user`, `--password`, `--out`, and
`--pg-version` wherever those concepts apply. Existing `pg-perf-bench --pg-*`,
`pg-workload --pg-major`/`--workload-user`, `pg-stand --postgres-version`, and
`pg-configurator --output-file-name` spellings remain compatibility aliases.
Secrets are the deliberate exception: `pg_workload` continues to accept
passwords only through environment/passfile mechanisms.

All eight component adapters use the common orchestration options:
`--machine`, `--request-id`, and `--component-capabilities`. Their advertised
`machine_interface` object makes these names machine-verifiable. The logical
adapter also supplies the contract selection described above.

## Human CLI

The `pg-play` CLI exposes experiment operations and separate maker, converter
and logical workflows. Live-server capture and configuration review are exposed
through MCP and `PgPlayService`, not dedicated CLI subcommands.

| Command | Effect |
| --- | --- |
| `capabilities` | Read installed component contracts |
| `maker ...` | Build, inspect, develop, export and publish through the maker adapter |
| `logical ...` | Plan and monitor logical migration operations and experiments |
| `plan-converter-run --project DIR --config FILE --packet NAME` | Resolve packet content and exact database aliases without connecting |
| `start-converter-run PLAN.json --plan-hash HASH --out DIR --run-id ID` | Start an unchanged packet plan in a detached worker |
| `converter-run-status RUN_DIR` | Read durable packet-run state and detect a lost worker |
| `converter-run-events RUN_DIR [--after-sequence N]` | Read ordered packet-run events |
| `cancel-converter-run RUN_DIR [--reason TEXT]` | Request cooperative packet cancellation |
| `validate MANIFEST` | Validate the manifest and non-mutating component inputs |
| `plan MANIFEST` | Calculate the current read-only plan and its hash |
| `start MANIFEST --plan-hash HASH --run-id ID` | Start exactly that plan in a detached worker and return immediately |
| `status MANIFEST --run-id ID` | Read durable state and detect a lost worker |
| `events MANIFEST --run-id ID [--after-sequence N]` | Read ordered durable events, with cursor pagination |
| `cancel MANIFEST --run-id ID [--reason TEXT]` | Request cooperative cancellation |
| `resume MANIFEST --plan-hash HASH --run-id ID` | Verify and resume a failed, cancelled, or interrupted attempt |
| `run MANIFEST --plan-hash HASH --run-id ID` | Execute synchronously for compatibility |
| `inspect-report REPORT.json` | Validate and summarize one diagnostic artifact |
| `compare-reports BASELINE.json CANDIDATE.json` | Produce deterministic summary deltas |
| `inspect-benchmark-report REPORT.json` | Validate and summarize one benchmark artifact |
| `compare-benchmark-reports BASELINE.json CANDIDATE.json` | Check server, environment and methodology identity, then produce TPS deltas |
| `benchmark-profiles` | List packaged maximum-TPS workload profiles |
| `benchmark-join-tasks` | List documented benchmark JOIN scenarios |
| `join-benchmark-reports --report ... --join-task TASK --out DIR --report-name NAME` | Join only the explicitly named benchmark reports |
| `teardown MANIFEST [--clear-stand-data]` | Stop workload processes and remove the managed stand |

Typical flow:

```bash
mkdir -p stand workload
pg-stand init --directory stand
pg-workload init --directory workload

pg-play validate experiment.yaml
pg-play plan experiment.yaml > plan.json

# Copy plan_hash from the reviewed plan. This command returns after spawning
# the durable worker; it does not wait for the experiment to finish.
pg-play start experiment.yaml \
  --plan-hash sha256:... \
  --run-id baseline-001

pg-play status experiment.yaml --run-id baseline-001
pg-play events experiment.yaml --run-id baseline-001 --after-sequence 0
# After the run reaches succeeded or partial, inspect its diagnostic artifact:
pg-play inspect-report .pg_play/experiments/pg18-mixed-baseline/baseline-001/pg18-mixed-diagnostics.json
```

`start` recalculates the plan and rejects a stale hash. A `run_id` remains
permanently bound to the same manifest and plan. Use `resume` with that same id
for a verified failed, cancelled, or interrupted attempt; use a new id when the
manifest or plan changes. `run` retains the old synchronous behavior for
scripts that explicitly need it. Plan/start are separate calls: do not start if
validation fails or the plan is blocked. Hashes such as `sha256:...` in examples
are placeholders for the full hash returned by the corresponding planner.

CLI operations print JSON on success. Exit code 0 means the request completed;
a status request can still return a failed, blocked or interrupted run. Inspect
the returned state and evidence, not just the process exit code. Caught input or
operation errors print to stderr and return 2.

Teardown is explicit. Wait for active experiments to finish or cancel them first,
then use `pg-play teardown experiment.yaml`. It stops the workload and removes
the selected stand containers while retaining database storage. Add
`--clear-stand-data` only to discard that stand's data. Keep run artifacts and
project credentials for inspection or recovery.

### Reviewed pg_converter packet runs

`pg_converter` packets are trusted deployment code and are intentionally
separate from the general experiment manifest. Plan an exact packet, review
its legacy tracker checksum, SHA-256 source-tree hash, Python-step flag, and
resolved database aliases and password-free connection identities, then start
the unchanged plan:

```bash
pg-play plan-converter-run \
  --project /opt/pg_converter/current \
  --config /etc/pg_converter/pg_converter.conf \
  --packet release_42 \
  --database-selector 'prod_a,prod_b' \
  --placeholders-file /run/pg_converter/release_42.json \
  --timeout-seconds 3600 > converter-plan.json

pg-play start-converter-run converter-plan.json \
  --plan-hash sha256:... \
  --out /var/lib/pg_play/converter-runs \
  --run-id release-42
```

The machine path always uses sequential database processing and never enables
`--force`, `--wipe`, `--stop`, `--unlock`, template copying, or skip-on-cancel
modes. The reviewed plan replaces only the interactive multi-database prompt;
it does not bypass PG Converter checksum or ambiguous-outcome safeguards.
Credentials stay in the protected configuration file. Placeholder and
configuration-override values are accepted only through protected JSON files
and are represented in the plan by names and hashes, not values.
See the
[pg_converter integration runbook](docs/pg_converter-integration.md)
for the durable directory, cancellation, and recovery contracts.

## MCP server

Start the stdio server with:

```bash
pg-play-mcp
```

Configure an MCP client to launch that executable with no shell wrapper. See
[Agent client setup](docs/agent-client-setup.md) for Codex CLI, Claude Code,
Hermes Agent, Kimi Code CLI, Gemini CLI and OpenCode.

The server registers the following tools. Optional tools are registered even
when their component is absent; install the corresponding extra before use.

| Workflow | MCP tools |
| --- | --- |
| Component discovery | `component_capabilities` |
| Experiments | `validate_experiment`, `plan_experiment`, `start_experiment`, `run_experiment`, `experiment_status`, `experiment_events`, `cancel_experiment`, `resume_experiment`, `teardown_experiment` |
| Reports and benchmarks | `inspect_diagnostic_report`, `compare_diagnostic_reports`, `inspect_benchmark_report`, `compare_benchmark_reports`, `benchmark_profiles`, `benchmark_join_tasks`, `join_benchmark_reports` |
| Live diagnostics | `plan_live_diagnostics`, `start_live_diagnostics`, `live_diagnostics_status`, `live_diagnostics_events`, `cancel_live_diagnostics` |
| Configuration review | `plan_configuration_review`, `collect_configuration_facts`, `generate_configuration_candidate`, `compare_configuration_candidate` |
| Converter packets | `plan_converter_run`, `start_converter_run`, `converter_run_status`, `converter_run_events`, `cancel_converter_run` |
| Logical replication | `logical_inspect`, `logical_plan`, `logical_start`, `logical_status`, `logical_events`, `logical_stop_controller`, `logical_resume_controller`, `logical_report` |
| Maker builds and runtime | `maker_validate`, `maker_init`, `maker_config_show`, `maker_package_names`, `maker_prepare`, `maker_resolve`, `maker_plan`, `maker_start`, `maker_status`, `maker_events`, `maker_cancel`, `maker_resume`, `maker_artifacts`, `maker_export`, `maker_runtime_check`, `maker_runtime_plan`, `maker_runtime_start` |
| Maker development | `maker_dev_open`, `maker_dev_status`, `maker_dev_patch`, `maker_dev_build`, `maker_dev_explain`, `maker_dev_exec`, `maker_dev_pin`, `maker_dev_prune`, `maker_changes_workspace`, `maker_changes_validate`, `maker_changes_export` |
| Maker extensions and publication | `maker_extension_init`, `maker_extension_prepare`, `maker_extension_plan`, `maker_extension_build`, `maker_publish_plan`, `maker_publish_run` |

An agent should validate, plan, show the mutation to the user, and then call
`start_experiment` with the unchanged hash and a new run id. It can poll
`experiment_status` and page through `experiment_events` without holding one
long MCP request open. The dependency range for the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) is
`mcp>=1.28,<2`; the 2.x API is outside the supported dependency range.

The durable schemas are also exposed as `pgplay://run-state-schema` and
`pgplay://run-event-schema`. Additional resources are
`pgplay://component-contract` and `pgplay://configuration-facts-schema`.
Core experiment schemas do not describe maker-owned operations or logical run
state; use each workflow's integration guide and returned schema identifiers.

For packet deployment, an agent must call `plan_converter_run`, show the exact
packet/source hashes, target aliases, Python-step risk, and mutation flags to
the user, and pass the unchanged plan and hash to `start_converter_run`.
Observe it through `converter_run_status` and `converter_run_events`. A failed
or interrupted packet is not automatically retried because non-transactional
actions can have an ambiguous outcome.

## Agent skills

The wheel contains six optional workflow skills under `pg_play/skills/`:

- `run-postgres-experiment` — validate, plan, start, and monitor a new run;
- `run-pg-converter-packet` — review exact packet sources and targets, start
  the immutable run, and preserve ambiguous outcomes for operator recovery;
- `recover-postgres-experiment` — inspect, cancel, and safely resume an
  existing durable run;
- `diagnose-live-postgres` — capture a bounded, read-only diagnostic window on
  an existing PostgreSQL host and interpret the validated report;
- `review-postgres-configuration` — inspect an existing server with a minimal
  read-only diagnostic set, generate a candidate, and compare changed settings;
- `analyze-postgres-experiment` — inspect reports, compare or join controlled
  runs, and design the next single-variable iteration.

Skills contain procedural guidance; they do not reimplement orchestration or
invoke raw component commands. Skill registration is agent-runtime-specific,
so installing a Python wheel does not automatically activate them in every
agent product.

## Comparing evidence

`compare-reports` compares diagnostic summary counts and checks artifact/content
identity, server version and collection modes. It does not calculate per-query
performance changes or verify workload, hardware, topology, observation-window
or seed equality. Check those inputs in the run plans and examine the report
items before drawing conclusions.

`compare-benchmark-reports` checks iteration axes/values, methodology, server
version and environment identity. It reports configuration changes separately
and still returns deltas when `comparability.comparable` is false. A successful
command alone does not establish a valid comparison. The mean TPS delta is an
aggregate of the reported measurements, not a statistical significance test.

For deliberate configuration, CPU, memory, storage, OS or PostgreSQL-version
changes, select a task from `benchmark-join-tasks` and use
`join-benchmark-reports`. JOIN enforces the controls for that task and rejects
incompatible inputs. See the [comparison scenario](docs/user-scenarios.md#2-compare-configuration-or-capacity).

## Live-server incident diagnostics

`plan_live_diagnostics` accepts PostgreSQL and SSH access references, one of
the reviewed intents (`performance`, `locks`, `io`, or `cpu`), and a bounded
snapshots window. It returns missing inputs, the exact versioned item allowlist,
safety limits, and a content hash without connecting to the target. Passwords,
private-key contents, arbitrary SQL, shell commands, tags, and caller-selected
item ids are not accepted.

SSH authentication is selected explicitly in `target.ssh`. Existing key-path
requests remain valid and are normalized to `auth: "key"`:

```json
{
  "ssh": {
    "host": "db.example",
    "user": "postgres",
    "auth": "key",
    "key_path": "/secure/path/id_ed25519",
    "known_hosts_path": "/secure/path/known_hosts"
  }
}
```

To use a key already loaded into a local agent, set `auth: "agent"` and omit
`key_path` and `key_passphrase_env`:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh-add -l
```

```json
{
  "ssh": {
    "host": "db.example",
    "user": "postgres",
    "auth": "agent",
    "known_hosts_path": "/secure/path/known_hosts"
  }
}
```

Agent plans contain only the authentication method, not the ephemeral socket
path. Planning and starting require a live Unix socket in `SSH_AUTH_SOCK`.
The detached diagnostics worker explicitly inherits that variable and passes
it to `pg-diag`; the same agent and socket must therefore remain alive until
the capture reaches a terminal state. `pg-play` never starts an agent, runs
`ssh-add`, or forwards the agent to the target.

`start_live_diagnostics` stores the unchanged plan in a new immutable capture
directory and starts a detached worker. The worker invokes `pg-diag snapshots`
in full remote mode, writes portable JSON and HTML, validates the JSON artifact,
and records ordered events and terminal state. MCP disconnection does not stop
the worker. Use `live_diagnostics_status` and `live_diagnostics_events` to
observe it and `cancel_live_diagnostics` for cooperative cancellation.

Capture duration is limited to 30–900 seconds, interval to 5–60 seconds, and
the schedule to at most 121 snapshots. A lost worker produces `interrupted`;
the existing evidence is retained and a new time window requires a new capture
id rather than a misleading resume or merge.

## Existing-server configuration review

`pg_play` exposes a deliberately read-only review workflow for an existing
PostgreSQL server. `plan_configuration_review` reports missing database, SSH,
and tuning-intent inputs. `collect_configuration_facts` runs one bounded
`pg-diag one-shot` collection containing only server version, effective
`pg_settings`, database size, CPU, RAM, filesystem, mount, disk, and extension
inventory items. It stores both the original diagnostic report and a compact
`pg_diag/configuration-facts-v1` artifact.

`generate_configuration_candidate` combines those observed facts with the
explicit database duty, storage class, replication mode, and PITR intent. The
result is a `pg_configurator/v1` or `pg_configurator/v2` candidate; the two
differ in how the candidate reports its own findings, not in the parameter
mappings this workflow reads. Finally,
`compare_configuration_candidate` writes JSON and Markdown containing only
changed or unobserved parameters, including current source, apply mode,
pending-restart state, calculation rule, and warnings.

The workflow never applies configuration, reloads PostgreSQL, restarts a
service, or treats a candidate as a benchmark-proven optimum. During collection,
`pg-diag` remote mode opens a bounded dynamic local SSH forward to the requested
PostgreSQL endpoint and closes it with the collection session. Passwords and
private-key contents are not accepted; callers provide a passfile reference,
strict known-hosts path, and either a key-path reference or explicit agent mode.

## Recovery model

Each run directory contains `state.json`, the original `experiment.yaml`, the
reviewed `plan.json`, `postgresql-parameters.json`, append-only `events.jsonl`,
and `worker.log`. `start_experiment` launches a detached worker with its own
session, so closing or crashing the MCP stdio process does not terminate the
experiment. If the worker itself disappears, the next `experiment_status`
changes an active run to `interrupted`.

Cancellation is a durable request in `cancel.request.json`. The worker checks
it before every step and while a component is running. A running component is
started in its own process group; before pg_play signals a process left behind
by a crashed worker, it verifies the recorded PID start time, operating-system
user, and executable identity.

Resume is intentionally conservative:

| Step | Safe resume rule |
| --- | --- |
| maker (when configured) | Verify the locked plan, reuse completed build/runtime operations, or explicitly resume their native workers |
| stand | Recalculate a read-only pg_stand plan and reconcile only the reviewed desired configuration |
| benchmark | Retry only through pg_perf_bench's idempotent dedicated-database reset |
| prepare database | Retry the declarative database preparation operation |
| install workload | Retry the idempotent profile installation operation |
| start/stop workload | Reconcile the requested running/stopped state |
| diagnostics | Retry the read-only collection operation |

Before a resume, pg_play verifies the manifest hash, plan hash, configuration
artifact, installed component versions, the exact allowlisted resume policy
for every recorded step, and all recorded artifact paths, sizes, and SHA-256
hashes that are available.

Completed benchmark and diagnostic steps must have report artifacts. A valid
completed step is reused; an incomplete step or a step with missing or changed
artifacts is rerun only when its operation is in the allowlist above. Any
unknown step or changed core artifact blocks recovery.

## Determinism and safety

- Every component returns an exact component envelope in hidden machine mode
  and advertises `pg_play/capabilities/v1` through the common
  `--component-capabilities` flag. Two envelope versions are accepted while the
  components migrate: `pg_play/component/v1` carries `warnings`, a list of
  sentences, and `pg_play/component/v2` carries `advisories`, objects with a
  stable `code`, a `severity` of `warning`, `assumption` or `info`, and the
  `setting` each one is about. Neither may carry the other's field. Anything
  read through `envelope_advisories` arrives in the v2 shape either way; a v1
  sentence becomes a `warning` with a null `code`, because that is all v1 can
  express.
- Plans hash normalized configuration, workload profile contents, scheduler
  state, and current stand state.
- Parameters owned by stand topology, TLS, fixed CSV logging, or diagnostic
  preloads remain under `pg_stand`; the plan records those candidate values
  separately and passes only non-owned parameters to `pg_stand`.
- Managed TLS stands are rejected during validation in `pg_play/v1`: the
  component contract does not yet provision a client certificate for the
  dedicated workload role. Direct TLS use of each component remains available.
- `pg-stand apply` verifies its component plan hash; `pg-play run` verifies the
  combined plan hash. `start` and `resume` enforce the same hash. Machine-mode
  `pg-perf-bench benchmark` independently
  verifies a content-sensitive benchmark plan hash before resetting its database.
- `pg-converter run` independently verifies the reviewed component plan hash,
  including the exact alias set, packet source tree, and file-based inputs.
- Subprocesses receive argument arrays with `shell=False`.
- The adapter rejects `--password` and `--show-password` arguments; workflow
  validators restrict credential inputs to their supported references. Envelope
  validation checks structure, not arbitrary secret content. Reports, SQL/log
  text, component errors and exports may contain sensitive data; review and
  redact artifacts before sharing them. There is no universal output scrubber.
- Detached component cleanup verifies PID start time, process ownership, and
  executable identity before signaling a process group.
- A failed cleanup cannot hide the original collection failure; a cleanup
  failure after successful diagnostics marks the run partial.
- `pg_diag` partial artifacts are retained and explicitly marked partial.
- Normal experiment completion retains the stand. A reviewed stand action can
  recreate containers to apply configuration; `teardown` explicitly removes
  them and deletes storage only with `--clear-stand-data`.

Remote OS collection uses strict SSH host verification. For a newly created
local `pg_stand`, `pg_play` captures its host key into the run directory before
starting `pg_diag`; the captured file is permission-restricted and retained as
run evidence.

## Current scope

The standard `pg_play/v1` manifest covers one configuration candidate, one managed stand,
an optional controlled benchmark, selected workload profiles, a one-shot or
snapshots report, and deterministic diagnostic and benchmark comparison.
Existing-server incident capture is separately limited to the four reviewed
read-only profiles and one bounded snapshots window per immutable capture id.
Configuration review extracts the bounded CPU, RAM, filesystem, mount, and disk
facts listed above. Generation or application of TuneD/systemd artifacts remains
a roadmap item; it is not silently approximated by the current implementation.
Reviewed `pg_converter` runs are a separate durable workflow and are not
silently injected into every experiment manifest. Optional `spec.maker` adds a
build/runtime-image phase. Logical migration uses a separate migration YAML and
`pg_play/logical-experiment-v1` scenario, with its own worker and recovery model.
It currently supports same-major PostgreSQL 15–18 migrations in a restricted
laboratory profile, not cross-major upgrade qualification.

Independent experiments can select different `spec.stand.project` directories.
Concurrent stands require pg-stand 0.3.0+ and distinct names, ports, storage and
workload roots. The pg-stand dependency includes support for those features.
See [multiple-stand scenarios](docs/user-scenarios.md#7-use-independent-stands).

## Labs

`labs/` contains repository-only scenarios that drive real stands through pg-play.
They are tracked in Git and excluded from both the wheel and source distribution.
[The site demo lab](https://github.com/O2eg/pg_play/blob/main/labs/site-demo-report/README.md) collects a five-minute report
with ten workload profiles and staged incidents, then anonymizes and audits it.
Its README describes stand/data preparation, current reproduction limits and
historical reference measurements.

## Development

Use Python 3.11+ and current component checkouts whose versions satisfy the
minimums above. Install the orchestrator and all eight components in one resolver
transaction, so unpublished local releases satisfy each other's requirements:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install \
  -e ../pg_configurator -e ../pg_converter -e ../pg_diag \
  -e ../pg_logical_repl -e ../pg_maker -e ../pg_perf_bench \
  -e ../pg_stand -e ../pg_workload -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python -m pip check
```

Branch CI checks out all eight component repositories and runs the tests on
Python 3.11 and 3.12. It builds a wheel set, installs only pg-play into a clean
environment and verifies automatic dependency installation, component identities,
versions and capability contracts. It also installs each component in a separate
clean environment and verifies that pg-play is absent. The same installation
checks run before publication using the component releases available from PyPI.
Docker/database integration tests have separate prerequisites; these installation
and unit checks do not qualify full database migrations or PostgreSQL builds.

For coordinated releases, publish all required component distributions before
tagging pg-play. Publish pg-diag before any pg-perf-bench release that requires
it, and publish pg-maker, pg-stand and pg-logical-repl updates before a pg-play
release that requires their versions. Ordinary branch CI can exercise coordinated
source changes before publication; tagged publishing jobs deliberately resolve
components from PyPI and enforce this release order.

Scheduler cleanup during resume, cancellation, and completion requires the identity
returned by `pg-workload start` (PID, process start ticks, and OS boot ID). A
replacement scheduler is not stopped. Older runs without this evidence cannot
automatically stop a running scheduler; inspect it and stop it explicitly if
appropriate. This requires a pg-workload version supporting `--expected-identity`;
older versions reject the guarded stop instead of executing an unguarded one.
