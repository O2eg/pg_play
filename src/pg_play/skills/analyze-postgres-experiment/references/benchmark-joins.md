# Benchmark JOIN guidance

Call `benchmark_join_tasks` before selecting a scenario. Treat its installed
catalog and controlled paths as authoritative; the summary below is for task
selection, not a replacement for validation.

| User question | JOIN task | Intended variable |
| --- | --- | --- |
| Which PostgreSQL configuration is best? | `optimize-db-config` | Effective database settings |
| How does throughput scale with CPU? | `scale-cpu` | CPU capacity |
| Does additional RAM help without retuning PostgreSQL? | `scale-memory` | Memory capacity |
| Which storage device or layout is better? | `compare-storage` | Storage, filesystem, or layout |
| What changes after a major upgrade? | `compare-postgresql-major` | PostgreSQL version, build, and version-dependent settings |
| Does an OS or kernel policy help? | `tune-os-kernel` | Kernel, network, or mount tuning |
| Is the result repeatable or a regression? | `repeatability` | Run time and unavoidable background noise only |

## Prepare the report set

1. State the hypothesis and the one dimension intentionally allowed to change.
2. Choose the reference report explicitly; do not infer it from filename order.
3. Inspect every report and confirm it is complete enough for the question.
4. Use the exact source paths in `join_benchmark_reports`. Do not join every
   file found in a directory.
5. Use at least two reports. Prefer at least three nominally identical runs for
   `repeatability`.

The workload execution hash must preserve schema, generator source, generated
dataset, query set, profile, and scale. The benchmark methodology must preserve
the client sweep, duration, cache/reset policy, pgbench placement, and load
generator. The selected JOIN task then enforces the remaining hardware, OS,
PostgreSQL build, and effective-settings controls appropriate to its intended
variable.

## Interpret the joined report

1. Compare the complete TPS curve and latency, not only the single maximum TPS
   point.
2. Compare the client count at peak TPS; a capacity change may move the
   saturation point.
3. Read vertically stacked CPU, RAM, disk, and network charts at matching axis
   values. Do not overlay unrelated iterations mentally.
4. For CPU scaling, report absolute TPS gain and scaling efficiency:
   `TPS_new / TPS_old / (CPU_new / CPU_old)`.
5. For repeatability, examine the spread at every client count and investigate
   background CPU, I/O, thermal throttling, autovacuum, and noisy neighbours.
6. Report any allowed secondary change, such as extension versions during a
   PostgreSQL-major comparison, before making a causal claim.

If JOIN rejects a report, quote the mismatched controlled paths and stop the
comparison. A rejected report belongs to another experiment unless the source
run can be repeated with the original controls restored.
