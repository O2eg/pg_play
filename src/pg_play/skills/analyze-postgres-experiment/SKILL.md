---
name: analyze-postgres-experiment
description: Validate, inspect, compare, and join pg_diag and pg_perf_bench artifacts produced by pg_play experiments, then design controlled follow-up iterations. Use when a user asks what changed between PostgreSQL runs, whether a configuration improved behavior, why a report is partial, which diagnostic or benchmark signals deserve attention, how to combine two or more benchmark reports under a JOIN task, or what single configuration, workload, hardware, PostgreSQL-version, storage, memory, CPU, or OS change to test next.
---

# Analyze PostgreSQL Experiment

Use `inspect_diagnostic_report` and `compare_diagnostic_reports` before interpreting report results. These tools validate the artifact contract and prevent analysis of malformed or incompatible JSON.

For benchmark artifacts, use `inspect_benchmark_report` and
`compare_benchmark_reports`. Confirm equal iteration parameters and values,
benchmark methodology, workload definition, client placement, PostgreSQL
configuration, and stand resources before interpreting TPS deltas.

## Single report

1. Inspect the report.
2. Check `has_errors`, completeness, collection-status counts, snapshot count, and content checksum before drawing conclusions.
3. Separate collection failures from PostgreSQL findings.
4. Cite item ids and measured values from the report when making a claim.

## Diagnostic comparison

1. Compare artifacts through `compare_diagnostic_reports`.
2. Confirm comparable PostgreSQL versions, workload profiles, scale, observation window, collection mode, and content checksum.
3. Treat changes in completeness or unsupported items as a comparability problem, not a performance result.
4. Analyze deltas by subsystem: query plans/statements, waits and locks, WAL/checkpoints, memory/cache, vacuum, storage, replication, and OS.
5. Prefer one next experimental change. State the expected signal and the criterion for accepting or rejecting it.

Read [references/interpretation.md](references/interpretation.md) when proposing the next configuration iteration.

## Benchmark comparison and JOIN

1. Inspect every source with `inspect_benchmark_report` before comparing it.
2. Use `compare_benchmark_reports` for an explicit baseline/candidate pair.
3. For two or more controlled runs, call `benchmark_join_tasks`, select the
   task matching the user's intended variable, and read
   [references/benchmark-joins.md](references/benchmark-joins.md).
4. Call `join_benchmark_reports` with the exact reviewed report list. Keep the
   explicitly chosen baseline first and use a unique report name.
5. Treat a JOIN rejection as evidence that a controlled dimension changed.
   Correct the experiment and rerun; do not bypass the compatibility check.
6. Interpret TPS, latency, and the vertically stacked CPU, RAM, disk, and
   network charts together. Distinguish throughput gains from a shifted host
   bottleneck or unstable run.

## Guardrails

- Do not claim causality from one run when multiple inputs changed.
- Do not equate a lower diagnostic count with improvement without inspecting severity and collection completeness.
- Keep a partial artifact; recommend a targeted recollection instead of discarding all usable evidence.
- Use a new run id for every follow-up experiment.
- Do not join reports merely because their filenames or report names look related.
