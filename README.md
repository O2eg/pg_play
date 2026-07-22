# pg_play

`pg_play` is the AI-ready orchestration layer for reproducible PostgreSQL
experiments.

- Recreates the same PostgreSQL stand, backend activity, and diagnostic window.
- Applies a versioned `pg_configurator` candidate only through a reviewed
  `pg_stand` plan.
- Keeps human-facing component CLIs small while exposing a typed MCP workflow
  to an AI agent.
- Records immutable plan hashes, run state, component versions, and artifact
  hashes.
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
pg-play-mcp           thirteen typed, high-level operations
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
| `run MANIFEST --plan-hash HASH --run-id ID` | Execute exactly that plan |
| `status MANIFEST --run-id ID` | Read durable run state |
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

# Copy plan_hash from the reviewed plan.
pg-play run experiment.yaml \
  --plan-hash sha256:... \
  --run-id baseline-001

pg-play status experiment.yaml --run-id baseline-001
pg-play inspect-report .pg_play/experiments/pg18-mixed-baseline/baseline-001/report.json
```

`run` recalculates the plan and rejects a stale hash. A run id is immutable;
retry a changed or failed experiment under a new id.

## MCP server

Start the stdio server with:

```bash
pg-play-mcp
```

Configure an MCP client to launch that executable with no shell wrapper. The
server exposes only:

- `component_capabilities`
- `validate_experiment`
- `plan_experiment`
- `run_experiment`
- `experiment_status`
- `inspect_diagnostic_report`
- `compare_diagnostic_reports`
- `inspect_benchmark_report`
- `compare_benchmark_reports`
- `benchmark_profiles`
- `benchmark_join_tasks`
- `join_benchmark_reports`
- `teardown_experiment`

An agent should validate, plan, show the mutation to the user, and then call
`run_experiment` with the unchanged hash and a new run id. The implementation
uses the stable [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
1.x line and intentionally excludes the 2.x prerelease API.

## Agent skills

The wheel contains two optional workflow skills under `pg_play/skills/`:

- `run-postgres-experiment` — validate, plan, execute, and recover a run;
- `analyze-postgres-experiment` — inspect reports, compare controlled runs,
  and design the next single-variable iteration.

Skills contain procedural guidance; they do not reimplement orchestration or
invoke raw component commands. Skill registration is agent-runtime-specific,
so installing a Python wheel does not automatically activate them in every
agent product.

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
  combined plan hash. Machine-mode `pg_perf_bench benchmark` independently
  verifies a content-sensitive benchmark plan hash before resetting its database.
- Subprocesses receive argument arrays with `shell=False`.
- Password-bearing CLI arguments and password-bearing machine output are
  rejected.
- Background workload stop verifies PID ownership before signaling a process.
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
Automatic extraction of OS facts and generation or
application of TuneD/systemd artifacts remains a roadmap item; it is not
silently approximated by the current implementation.

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
