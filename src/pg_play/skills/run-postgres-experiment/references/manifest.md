# Experiment manifest reference

Use `api_version: pg_play/v1` and `kind: PostgreSQLExperiment`.

Required structure:

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
      db_cpu: "4"
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
    database: pg_perf_bench_test
    report_name: pg18-mixed-benchmark
    benchmark_type: default
    clients: [1, 4, 16]              # or times_seconds
    init_command: >-
      ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT
      -U ARG_PG_USER ARG_PG_DATABASE
    workload_command: >-
      ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS
      -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE
  phases:
    benchmark: true
    workload_diagnostics: true
    recreate_workload_database: true
```

Paths are resolved relative to the manifest. The stand project defaults to the
manifest directory and must not depend on the agent process working directory.
Keep secrets out of YAML. `db_cpu`, `db_ram`, and the stand PostgreSQL major
must describe the same target. Use `remote` collection when container OS
evidence is required; use `remote-db-only` for database-only collection.
The resource guard remains enabled; any threshold override is hashed into the
experiment plan.

When `benchmark` is present, use a dedicated disposable database. Exactly one
of `clients` and `times_seconds` is required. A custom benchmark also requires
`benchmark_type: custom` and an existing `workload_path`; its content is part
of the reviewed plan hash.

For a packaged maximum-TPS benchmark, use `workload_profile: imdb` or
`workload_profile: pagila`, optional `workload_scale`,
`workload_duration_seconds`, and `clients`. Omit
`init_command`, `workload_command`, and `workload_path`; pg_perf_bench supplies
and embeds the profile sources and exact commands. The newest local pgbench and
matching psql are selected automatically. `system_metrics_interval` defaults to
one second; set `system_metrics_duration` only for a custom command which has no
pgbench `-T` or `--time` option. The pg_diag OS sampler runs on the Docker host
during every measured workload window.
