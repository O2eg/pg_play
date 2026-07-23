---
name: run-postgres-experiment
description: Plan, start, and monitor new reproducible PostgreSQL stand, benchmark, workload, and diagnostic flows through the pg_play MCP server. Use when a user asks to create or rebuild a PostgreSQL stand, apply a pg_configurator candidate, benchmark with pg_perf_bench, emulate backend activity with pg_workload, collect a pg_diag report, run a changed experiment under a new id, or observe a newly started run. For cancellation or recovery of an existing failed, cancelled, interrupted, or stuck run, use recover-postgres-experiment.
---

# Run PostgreSQL Experiment

Use only the high-level `pg_play` MCP tools. Do not construct raw component shell commands or SQL.

## Workflow

1. Read the requested experiment manifest. If creating or changing one, follow [references/manifest.md](references/manifest.md).
2. Call `validate_experiment`. Stop on any validation error; do not weaken component checks.
3. Call `plan_experiment`. Present the required stand action, configuration artifact hash, optional benchmark plan, selected workload profiles, diagnostic mode, warnings, and `plan_hash`.
4. Before mutation, ensure the user authorized running the experiment and that the plan is not blocked.
5. Choose a new explicit `run_id`. A run id is permanently bound to one manifest and plan.
6. Call `start_experiment` with the exact returned `plan_hash`. Never substitute a stale hash or hold a synchronous MCP call open.
7. Poll `experiment_status` and page through `experiment_events` using `last_sequence` as the next `after_sequence` cursor. Do not infer completion from elapsed time.
8. If the run becomes `failed`, `cancelled`, `interrupted`, or remains stuck, switch to `recover-postgres-experiment`. Use that skill for cancellation as well.
9. Report artifact paths and distinguish `succeeded` from `partial`. Partial pg_diag and pg_perf_bench artifacts remain inspectable.

## Guardrails

- Treat the manifest and plan as immutable inputs. Treat run state and events as durable pg_play-owned records; never edit them to force recovery.
- Do not put passwords, tokens, password-bearing DSNs, or private-key contents in a manifest or response.
- Do not bypass ownership, plan-hash, content-integrity, or profile validation failures.
- Do not automatically destroy the stand after a run. Stand cleanup requires a separate explicit user request.
- Change one experimental variable at a time unless the user explicitly requests a combined experiment.
