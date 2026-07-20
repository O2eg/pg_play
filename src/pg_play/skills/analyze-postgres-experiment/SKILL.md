---
name: analyze-postgres-experiment
description: Validate, inspect, and compare pg_diag artifacts produced by pg_play experiments, then design controlled follow-up iterations. Use when a user asks what changed between PostgreSQL runs, whether a configuration improved behavior, why a report is partial, which diagnostic signals deserve attention, or what single configuration or workload change to test next.
---

# Analyze PostgreSQL Experiment

Use `inspect_diagnostic_report` and `compare_diagnostic_reports` before interpreting report results. These tools validate the artifact contract and prevent analysis of malformed or incompatible JSON.

## Single report

1. Inspect the report.
2. Check `has_errors`, completeness, collection-status counts, snapshot count, and content checksum before drawing conclusions.
3. Separate collection failures from PostgreSQL findings.
4. Cite item ids and measured values from the report when making a claim.

## Comparison

1. Compare artifacts through `compare_diagnostic_reports`.
2. Confirm comparable PostgreSQL versions, workload profiles, scale, observation window, collection mode, and content checksum.
3. Treat changes in completeness or unsupported items as a comparability problem, not a performance result.
4. Analyze deltas by subsystem: query plans/statements, waits and locks, WAL/checkpoints, memory/cache, vacuum, storage, replication, and OS.
5. Prefer one next experimental change. State the expected signal and the criterion for accepting or rejecting it.

Read [references/interpretation.md](references/interpretation.md) when proposing the next configuration iteration.

## Guardrails

- Do not claim causality from one run when multiple inputs changed.
- Do not equate a lower diagnostic count with improvement without inspecting severity and collection completeness.
- Keep a partial artifact; recommend a targeted recollection instead of discarding all usable evidence.
- Use a new run id for every follow-up experiment.
