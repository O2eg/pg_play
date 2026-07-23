# pg_play

`pg_play` is the AI-ready orchestration layer for reproducible PostgreSQL
experiments.

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
- Installs every component while preserving their independent use.

The independently installable components are:

- [`pg_stand`](https://github.com/O2eg/pg_stand) — reproducible PostgreSQL
  stands;
- [`pg_workload`](https://github.com/O2eg/pg_workload) — profile-driven backend
  activity emulation;
- [`pg_diag`](https://github.com/O2eg/pg_diag) — diagnostic JSON and HTML
  artifacts;
- [`pg_configurator`](https://github.com/O2eg/pg_configurator) — version-aware
  PostgreSQL configuration candidates.
- [`pg_perf_bench`](https://github.com/O2eg/pg_perf_bench) — controlled pgbench
  execution and environment evidence.

## How it works

```text
                         pg_play
                            |
              +-------------+-------------+
              |                           |
              v                           |
      pg_configurator                     |
              | versioned config          |
              v                           |
          pg_stand ------------------------+
              |
              v
                    PostgreSQL stand
                  /          |          \
                 v           v           v
          pg_workload  pg_perf_bench   pg_diag
                 |           |           |
                 |     benchmark report  |
                 +--------- run ----------+----> diagnostic report
                                  |
                  change one reviewed input
                                  |
                                  +----> rebuild, rerun, compare
```

The control layers are deliberately separate:

```text
agent skills          workflow and interpretation rules
      |
      v
pg-play-mcp           twenty-six typed, high-level operations
      |
      v
pg_play core          validation, planning, state, comparison
      |
      v
component adapters    argv arrays + strict JSON envelopes
      |
      +---- pg_configurator
      +---- pg_stand
      +---- pg_workload
      +---- pg_diag
      `---- pg_perf_bench
```

MCP does not expose arbitrary shell, SQL, Docker, or raw component-command
tools. Each component remains pleasant to use directly: orchestration flags
are hidden from its primary help and its normal human output is unchanged.

## Installation

```bash
python -m pip install pg-play
```

This installs compatible versions of all five component distributions. They
remain available through their own commands:

```bash
pg-stand --help
pg-workload --help
pg-diag --help
pg-configurator --help
pg-perf-bench --help
```

## Experiment manifest

`pg_play/v1` is strict: unknown fields, secret-bearing configurator inputs,
missing projects, invalid profile names, and invalid diagnostic windows are
errors.

```yaml
api_version: pg_play/v1
kind: PostgreSQLExperiment
metadata:
  id: pg18-mixed-baseline
spec:
  artifact_root: .pg_play/experiments/pg18-mixed-baseline
  stand:
    config: ../pg_stand/configs/single.yaml
    project: ./stand
  configurator:
    inputs:
      db_cpu: 4
      db_ram: 8Gi
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
    system_metrics_interval: 1
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
pg_diag OS sampler runs during every benchmark window; use
`system_metrics_interval` to control its cadence and
`system_metrics_duration` only when a custom command has no pgbench `-T` or
`--time` option.

Paths are resolved relative to the manifest. `spec.stand.project` defaults to
the manifest directory and fixes where `pg_stand` stores state, credentials,
and storage regardless of the caller's current directory. Credentials never
belong in this file. `pg_play` obtains the stand-owned administrator credential, creates a
random workload credential, and stores project-local passfiles with mode
`0600`.

The workload resource guard is always enabled by `pg_play`. Its thresholds are
manifest inputs and therefore part of the reviewed plan hash; override them
only when the host policy is explicitly known.

The packaged JSON Schema is available as the MCP resource
`pgplay://experiment-schema`.

## Shared CLI conventions

`pg_diag` naming is the reference for equivalent options. Components now use
`--host`, `--port`, `--database`, `--user`, `--password`, `--out`, and
`--pg-version` wherever those concepts apply. Existing `pg_perf_bench --pg-*`,
`pg_workload --pg-major`/`--workload-user`, `pg_stand --postgres-version`, and
`pg_configurator --output-file-name` spellings remain compatibility aliases.
Secrets are the deliberate exception: `pg_workload` continues to accept
passwords only through environment/passfile mechanisms.

All five components use the same hidden orchestration options:
`--machine`, `--request-id`, and `--component-capabilities`. Their advertised
`machine_interface` object makes these names machine-verifiable.

## Human CLI

The `pg-play` CLI contains only complete experiment operations:

| Command | Effect |
| --- | --- |
| `capabilities` | Read installed component contracts |
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
pg-play inspect-report .pg_play/experiments/pg18-mixed-baseline/baseline-001/report.json
```

`start` recalculates the plan and rejects a stale hash. A `run_id` remains
permanently bound to the same manifest and plan. Use `resume` with that same id
for a verified failed, cancelled, or interrupted attempt; use a new id when the
manifest or plan changes. `run` retains the old synchronous behavior for
scripts that explicitly need it.

## MCP server

Start the stdio server with:

```bash
pg-play-mcp
```

Configure an MCP client to launch that executable with no shell wrapper. The
server exposes only:

See [Agent client setup](https://github.com/O2eg/pg_play/blob/main/docs/agent-client-setup.md)
for concise MCP and Agent Skills configuration examples for Codex CLI, Claude
Code, Hermes Agent, Kimi Code CLI, Gemini CLI, and OpenCode.

- `component_capabilities`
- `plan_live_diagnostics`
- `start_live_diagnostics`
- `live_diagnostics_status`
- `live_diagnostics_events`
- `cancel_live_diagnostics`
- `plan_configuration_review`
- `collect_configuration_facts`
- `generate_configuration_candidate`
- `compare_configuration_candidate`
- `validate_experiment`
- `plan_experiment`
- `start_experiment`
- `run_experiment`
- `experiment_status`
- `experiment_events`
- `cancel_experiment`
- `resume_experiment`
- `inspect_diagnostic_report`
- `compare_diagnostic_reports`
- `inspect_benchmark_report`
- `compare_benchmark_reports`
- `benchmark_profiles`
- `benchmark_join_tasks`
- `join_benchmark_reports`
- `teardown_experiment`

An agent should validate, plan, show the mutation to the user, and then call
`start_experiment` with the unchanged hash and a new run id. It can poll
`experiment_status` and page through `experiment_events` without holding one
long MCP request open. The implementation uses the stable
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 1.x line
and intentionally excludes the 2.x prerelease API.

The durable schemas are also exposed as `pgplay://run-state-schema` and
`pgplay://run-event-schema`.

## Agent skills

The wheel contains five optional workflow skills under `pg_play/skills/`:

- `run-postgres-experiment` — validate, plan, start, and monitor a new run;
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

## Live-server incident diagnostics

`plan_live_diagnostics` accepts PostgreSQL and SSH access references, one of
the reviewed intents (`performance`, `locks`, `io`, or `cpu`), and a bounded
snapshots window. It returns missing inputs, the exact versioned item allowlist,
safety limits, and a content hash without connecting to the target. Passwords,
private-key contents, arbitrary SQL, shell commands, tags, and caller-selected
item ids are not accepted.

`start_live_diagnostics` stores the unchanged plan in a new immutable capture
directory and starts a detached worker. The worker invokes `pg_diag snapshots`
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
`pg_diag one-shot` collection containing only server version, effective
`pg_settings`, database size, CPU, RAM, filesystem, mount, disk, and extension
inventory items. It stores both the original diagnostic report and a compact
`pg_diag/configuration-facts-v1` artifact.

`generate_configuration_candidate` combines those observed facts with the
explicit database duty, storage class, replication mode, and PITR intent. The
result remains a `pg_configurator/v1` candidate. Finally,
`compare_configuration_candidate` writes JSON and Markdown containing only
changed or unobserved parameters, including current source, apply mode,
pending-restart state, calculation rule, and warnings.

The workflow never applies configuration, reloads PostgreSQL, restarts a
service, or treats a candidate as a benchmark-proven optimum. During collection,
`pg_diag` remote mode opens a bounded dynamic local SSH forward to the requested
PostgreSQL endpoint and closes it with the collection session. Passwords and
private-key contents are not accepted; callers provide passfile, key, and strict
known-hosts paths.

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

- Every component returns the exact `pg_play/component/v1` envelope in hidden
  machine mode and advertises `pg_play/capabilities/v1` through the common
  `--component-capabilities` flag.
- Plans hash normalized configuration, workload profile contents, scheduler
  state, and current stand state.
- Parameters owned by stand topology, TLS, fixed CSV logging, or diagnostic
  preloads remain under `pg_stand`; the plan records those candidate values
  separately and passes only non-owned parameters to `pg_stand`.
- Managed TLS stands are rejected during validation in `pg_play/v1`: the
  component contract does not yet provision a client certificate for the
  dedicated workload role. Direct TLS use of each component remains available.
- `pg_stand apply` verifies its component plan hash; `pg_play run` verifies the
  combined plan hash. `start` and `resume` enforce the same hash. Machine-mode
  `pg_perf_bench benchmark` independently
  verifies a content-sensitive benchmark plan hash before resetting its database.
- Subprocesses receive argument arrays with `shell=False`.
- Password-bearing CLI arguments and password-bearing machine output are
  rejected.
- Detached component cleanup verifies PID start time, process ownership, and
  executable identity before signaling a process group.
- A failed cleanup cannot hide the original collection failure; a cleanup
  failure after successful diagnostics marks the run partial.
- `pg_diag` partial artifacts are retained and explicitly marked partial.
- `pg_play` never automatically destroys a stand.

Remote OS collection uses strict SSH host verification. For a newly created
local `pg_stand`, `pg_play` captures its host key into the run directory before
starting `pg_diag`; the captured file is permission-restricted and retained as
run evidence.

## Current scope

The first contract covers one configuration candidate, one managed stand,
an optional controlled benchmark, selected workload profiles, a one-shot or
snapshots report, and deterministic diagnostic and benchmark comparison.
Existing-server incident capture is separately limited to the four reviewed
read-only profiles and one bounded snapshots window per immutable capture id.
Configuration review extracts the bounded CPU, RAM, filesystem, mount, and disk
facts listed above. Generation or application of TuneD/systemd artifacts remains
a roadmap item; it is not silently approximated by the current implementation.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ../pg_stand -e ../pg_workload -e ../pg_diag \
  -e ../pg_configurator -e ../pg_perf_bench
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

For coordinated releases, publish the component distributions before tagging
`pg_play`: first `pg_configurator`, `pg_diag`, `pg_stand`, and `pg_workload`,
then `pg_perf_bench` (which depends on `pg_diag`), and finally `pg_play`.
Ordinary branch CI checks out the component sources so a coordinated source
change can be tested before those versions reach PyPI. Tagged publish jobs use
the package index deliberately and therefore enforce this release order.
