# Lab: public pg_diag sample report (`site-demo-5m-30s`)

Everything needed to rebuild the showcase report published from the `report_trace` lab on
2026-09-12: a 5-minute snapshot window (interval 30 s) over ten pg_workload profiles at scale 3.0
on the `pg-trace-20260905` stand (PostgreSQL 18.4, primary 2 CPU / 4 GiB with a synchronous
standby and a logical subscriber), with staged maintenance activity and real incidents, collected
by pg_diag in remote (SSH) mode through pg_play, then anonymized, validated, rendered and audited.

One command, about 12 minutes:

```bash
cd pg_play
.venv/bin/python labs/site-demo-report/run_report.py            # uses params.yaml as is
.venv/bin/python labs/site-demo-report/run_report.py --dry-run  # preflight, manifest and plan only
```

The last line of the output is the path of the public HTML:
`<root>/experiments/<tag>/public/<tag>.html` (next to it `<tag>.json`, `-items.tsv`, `-audit.json`,
`-browser.json`, `-plan.png` and `run-summary.json`). The originals collected by pg_play stay in
`<root>/experiments/<tag>/`.

## What the run does

| Phase | Time | Details |
|---|---|---|
| Preflight (read-only) | seconds | container running, workload scheduler stopped, free space, profile schemas, 12 `lab_*` roles, invalid index, extensions, replication senders |
| Manifest and plan | seconds | `params.yaml` → `<root>/experiment-<tag>.yaml` → `pg-play plan` (`evidence/<tag>-plan.json`) |
| Stand preparation | seconds | `ALTER SYSTEM` `statement_timeout=120s`, `temp_file_limit=2GB` (previous values recorded), demo grant for a real extension ACL drift, `pg_stat_statements_reset()` |
| Warm-up | 3 min | the same ten profiles through `pg-workload start … --run-immediately`, then `stop` |
| Staged activity | from T−85 s | `progress.py`: idle REPEATABLE READ transaction; at T−20 s a CPU-bound `CREATE INDEX` (24k rows), a slow `COPY`, a throttled `VACUUM` and a row-lock chain on `lab.orders id=1` (holder + 3 waiters, 150 s). `events.py` from T−2 s: deadlocks, lock waits, 57014, 28P01, 58P01, 53300 every third cycle, archiver failure, manual `CHECKPOINT`, auto_explain plans in four formats |
| pg_play run | ~6 min | `start_experiment` → stand / prepare-db / start-workload / diagnostics (pg_diag snapshots 300 s / 30 s, `log_depth_time_min: 8`) / stop-workload; resources logged every 10 s; cancel guard on free space and runtime |
| Restore | seconds | `ALTER SYSTEM SET` back to the recorded values when they came from `postgresql.auto.conf`, `RESET` otherwise; reload |
| Publish (`finalize.py`) | ~1 min | collector host/user/content path replaced, lshw hardware ids redacted, `pg-diag validate-artifact`, `pg-diag render`, item table, headless-Chrome audit (graph, charts, plan viewer, no JS errors) |

Point-in-time items are collected 5–20 s after `start_experiment`, so everything staged must
already be running: the timings above are tuned for that (see `diagnostics.pre_start_seconds`).

## Parameters (`params.yaml`)

| Key | Value | Why |
|---|---|---|
| `root` | `../../../report_trace` (the `report_trace` checkout next to `pg_play`) | pg_stand project (`stand/`), pg_workload project (`workload/`), `experiments/`, `evidence/`; relative values are anchored at this directory; `--root` or `$SITE_DEMO_ROOT` override |
| `tag` | `site-demo-5m-30s` | run id, `report_name`, public file stem; `--tag` override |
| `stand.primary_port` / `primary_container` | 55540 / `pg-trace-20260905-primary-pg-stand-managed` | superuser password from `stand/.pg_stand/credentials/database/passwords.json`; pg_diag SSH goes to `root@127.0.0.1:56540` with `stand/.pg_stand/credentials/ssh/pg_stand_test` |
| `workload.profiles`, `scale` | 10 profiles, 3.0 | `simple_stock_spec_symbols` deliberately disabled; `install: false` — data come from `prepare_stand.py` |
| `configurator_inputs` | 2 CPU / 4Gi / PG 18 / mixed | what pg_play feeds to pg_configurator for the stand parameters |
| `diagnostics` | 300 s / 30 s / log 8 min / warm-up 180 s | `--duration`, `--interval`, `--warmup` override |
| `staged_activity.index_rows` | 24000 | ~3–12 ms per row on the 2-core stand; 12000 rows once finished in 36 s and missed the point-in-time collection |
| `stand_run_settings` | 120s / 2GB | the lab default `statement_timeout=15s` would cancel the lock waiters and the slow maintenance |
| `demo_grants` | `grant select on public.pg_stat_statements to lab_analyst` | real drift against `pg_init_privs` for `object_workload.extension_objects_acl_drift` |
| `publish.*` | replacements, hardware id columns, leak patterns, `playwright_python` (`../../../pg_configurator/.venv/bin/python`, used only when this venv lacks playwright) | anonymization and the browser audit |
| `expected` | 321 items, ≥260 ok, 0 error, 11 snapshots, ≥800 plans | sanity checks in `run-summary.json` |

## Expected result (run of 2026-09-12)

321 items: 271 ok / 46 empty / 4 unsupported / 0 error; 11 snapshots; 1063 auto_explain plans;
log window 8 min, ~3170 records, `ranking_complete`. Filled sections: server_log, activity_locks
(blocking tree of 4 sessions, idle transaction), maintenance_progress (VACUUM, CREATE INDEX, COPY),
users_roles, replication on a primary, `os.cgroup_limits`, a real extension ACL drift. Empty items are
expected for standby-only charts, subscriptions on a publisher, crash recovery / wraparound and
systemd / sudoers / backup checks inside Docker.

## Re-running

* `run_report.py --force` archives an existing `experiments/<tag>` (plus stop file and evidence) as
  `<tag>-attemptN` and reuses the tag; otherwise pick another `--tag`.
* `run_report.py --skip-finalize` keeps the raw artifact only; `finalize.py --tag …` publishes later.
* `run_report.py --no-staged-activity` collects the plain workload without incidents.
* `workload_ctl.py start|stop` drives the workload manually with pg_play's arguments (pg_play refuses
  to start while the scheduler is already running; `pg-workload stop` accepts only `--root`).

## Preparing the stand from scratch

1. Bring the stand up from the pg_stand project: `cd <root>/stand && pg-stand up --config configs/trace.yaml`
   (the copy of the config is `setup/stand-trace.yaml`; postgres parameters come from
   `setup/configurator-parameters.json` and the lab observability overrides
   `setup/observability-overrides.json` — auto_explain everything, `log_min_duration_statement=100ms`,
   `deadlock_timeout=100ms`, `archive_command` that fails while `/tmp/pg-trace-archive-fail` exists).
   The pg_play stand step also does this when the manifest's stand is down.
2. `prepare_stand.py --prepare-db --scale 3.0` installs the profiles with the exact `pg-workload`
   arguments pg_play uses (database-level timeouts lifted for the bulk load), applies
   `setup/site-lab-users.sql` (12 `lab_*` roles, schema `lab` with RLS, column grants, default
   privileges, large objects, publication), leaves the invalid index `trace_probe.trace_churn_dup_demo`,
   applies the demo grants, sets the lab defaults `statement_timeout=15s` / `temp_file_limit=128MB`
   and resets pg_stat_statements. `--drop-schemas` removes profiles you disable (drop one schema per
   transaction: many_objects reinstall in one DO block runs out of `max_locks_per_transaction`).
3. Disk: scale 3.0 needs ~3.3 GB in the primary plus the standby and WAL; the run guard cancels below
   12 GiB free on the lab root filesystem.

## Gotchas that cost earlier attempts

* pg_diag has a 1 s budget per SQL item; `indexes.redundant_indexes` and `storage_vacuum.sequence_status`
  carry `timeout_ms: 2000` — keep `many_objects` at two schemas and `pss_overflow` small on 2 CPUs.
* The lock chain must be a ROW lock (`UPDATE` of one row). `LOCK TABLE … ACCESS EXCLUSIVE` kills
  `indexes.unused_indexes` (`pg_relation_size` waits on the lock) and ddl extraction.
* Waiters need `lock_timeout=0` and `auto_explain.log_min_duration=-1`, or they are cancelled by the
  stand's 15 s `lock_timeout` and their 150 s "plans" dominate the auto_explain chart.
* `statement_timeout` is restored with `ALTER SYSTEM SET` (the 15 s lives in `postgresql.auto.conf`);
  a plain `RESET` would fall back to the 30 min of `pg_stand_parameters.conf`.
* Each `ALTER SYSTEM` is its own statement; a multi-statement `psql -c` is one transaction.
* `/proc/stat` inside the container is the host's: OS CPU charts show 16 host cores, the cgroup quota is
  2 cores (`os.cgroup_limits`).

## Files

`run_report.py` (orchestrator), `lab_common.py` (parameters, paths, credentials, manifest, pg_play
context), `progress.py`, `events.py` (staged activity), `finalize.py`, `audit_report.py`,
`browser_audit.py` (publishing), `prepare_stand.py`, `workload_ctl.py`, `params.yaml`,
`setup/site-lab-users.sql`, `setup/configurator-parameters.json`, `setup/observability-overrides.json`,
`setup/stand-trace.yaml`. Historical scripts of the first runs stay in `report_trace/scripts/site_*.py`.
