---
name: run-postgres-experiment
description: Plan and run reproducible PostgreSQL stand, benchmark, workload, and diagnostic flows through the pg_play MCP server. Use when a user asks to create or rebuild a PostgreSQL stand, apply a pg_configurator candidate, benchmark it with pg_perf_bench, emulate backend activity with pg_workload, collect a pg_diag report, repeat an experiment with changed parameters, or inspect experiment status.
---

# Run PostgreSQL Experiment

Use only the high-level `pg_play` MCP tools. Do not construct raw component shell commands or SQL.

## Workflow

1. Read the requested experiment manifest. If creating or changing one, follow [references/manifest.md](references/manifest.md).
2. Call `validate_experiment`. Stop on any validation error; do not weaken component checks.
3. Call `plan_experiment`. Present the required stand action, configuration artifact hash, optional benchmark plan, selected workload profiles, diagnostic mode, warnings, and `plan_hash`.
4. Before mutation, ensure the user authorized running the experiment and that the plan is not blocked.
5. Choose a new explicit `run_id`. Never reuse an id for a changed or failed run.
6. Call `run_experiment` with the exact returned `plan_hash`. Never substitute a stale hash.
7. If execution is interrupted, call `experiment_status`; do not infer completion from elapsed time.
8. Report artifact paths and distinguish `succeeded` from `partial`. Partial pg_diag and pg_perf_bench artifacts remain inspectable.

## Guardrails

- Treat the manifest, plan, and run state as separate immutable artifacts.
- Do not put passwords, tokens, password-bearing DSNs, or private-key contents in a manifest or response.
- Do not bypass ownership, plan-hash, content-integrity, or profile validation failures.
- Do not automatically destroy the stand after a run. Stand cleanup requires a separate explicit user request.
- Change one experimental variable at a time unless the user explicitly requests a combined experiment.
