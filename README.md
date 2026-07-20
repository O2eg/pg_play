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
          /           \
         v             v
  pg_workload       pg_diag
         |             |
         +------ run --+----> diagnostic report
                                   |
                   change config/profile/version
                                   |
                                   +----> rebuild, rerun, compare
```

The control layers are deliberately separate:

```text
agent skills          workflow and interpretation rules
      |
      v
pg-play-mcp           seven typed, high-level operations
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
      `---- pg_diag
```

MCP does not expose arbitrary shell, SQL, Docker, or raw component-command
tools. Each component remains pleasant to use directly: orchestration flags
are hidden from its primary help and its normal human output is unchanged.

## Installation

```bash
python -m pip install pg-play
```

This installs compatible versions of all four component distributions. They
remain available through their own commands:

```bash
pg-stand --help
pg-workload --help
pg-diag --help
pg-configurator --help
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
```

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
  machine mode.
- Plans hash normalized configuration, workload profile contents, scheduler
  state, and current stand state.
- Parameters owned by stand topology, TLS, fixed CSV logging, or diagnostic
  preloads remain under `pg_stand`; the plan records those candidate values
  separately and passes only non-owned parameters to `pg_stand`.
- Managed TLS stands are rejected during validation in `pg_play/v1`: the
  component contract does not yet provision a client certificate for the
  dedicated workload role. Direct TLS use of each component remains available.
- `pg_stand apply` verifies its component plan hash; `pg_play run` verifies the
  combined plan hash.
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
selected workload profiles, a one-shot or snapshots report, and deterministic
report-summary comparison. Automatic extraction of OS facts and generation or
application of TuneD/systemd artifacts remains a roadmap item; it is not
silently approximated by the current implementation.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ../pg_stand -e ../pg_workload -e ../pg_diag -e ../pg_configurator
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```
