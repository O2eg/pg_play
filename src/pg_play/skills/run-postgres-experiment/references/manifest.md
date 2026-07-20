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

Paths are resolved relative to the manifest. The stand project defaults to the
manifest directory and must not depend on the agent process working directory.
Keep secrets out of YAML. `db_cpu`, `db_ram`, and the stand PostgreSQL major
must describe the same target. Use `remote` collection when container OS
evidence is required; use `remote-db-only` for database-only collection.
The resource guard remains enabled; any threshold override is hashed into the
experiment plan.
