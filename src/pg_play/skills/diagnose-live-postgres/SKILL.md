---
name: diagnose-live-postgres
description: Plan, start, monitor, cancel, and interpret a bounded read-only pg_diag snapshots capture on an existing PostgreSQL host. Use when a user reports a live performance incident such as a slow server, blocking or lock contention, high CPU, memory pressure, WAL or checkpoint pressure, storage latency, or unexplained throughput degradation and can provide PostgreSQL plus SSH access by local file or environment reference. Do not use this skill to run arbitrary SQL or shell commands, change configuration, terminate database sessions, or restart services.
---

# Diagnose Live PostgreSQL

Use only `plan_live_diagnostics`, `start_live_diagnostics`,
`live_diagnostics_status`, `live_diagnostics_events`,
`cancel_live_diagnostics`, and `inspect_diagnostic_report`.

## Plan the capture

1. Call `plan_live_diagnostics` with all known target fields. Ask only for
   `missing_inputs` and resolve every plan error. Pass credentials only as a
   passfile path, SSH key path, known-hosts path, or key-passphrase environment
   variable name. Never request passwords or private-key contents. Treat the
   database host as the endpoint visible from the SSH target, matching
   `pg_diag` remote-mode semantics.
2. Select one intent:
   - `performance` for an unclear or mixed slowdown;
   - `locks` for blocked sessions, long transactions, or deadlocks;
   - `io` for latency, WAL, checkpoint, temporary-file, or disk saturation;
   - `cpu` for CPU saturation or expensive SQL.
3. Prefer 60 seconds with a 5-second interval. Use 30 seconds for a host where
   collection must be especially brief. Increase the duration only when the
   user needs a longer observation window; never bypass the plan limits.
4. Present the intent, exact duration, interval, item count, target host, and
   `plan_hash`. Explain that collection is read-only but adds bounded diagnostic
   queries and OS sampling. Obtain authorization before starting on a live
   server.

## Start and observe

1. Choose a unique `capture_id` and a dedicated output directory. Call
   `start_live_diagnostics` with the exact returned plan and `plan_hash`.
2. Save the returned capture directory. Poll `live_diagnostics_status` and page
   through `live_diagnostics_events` using `last_sequence` as the next
   `after_sequence` cursor.
3. Treat `queued` and `running` as active, `effective_state=cancelling` as a
   pending cancellation, and `succeeded`, `partial`, `failed`, `cancelled`, or
   `interrupted` as terminal.
4. Call `cancel_live_diagnostics` only when the user requests a stop or an
   external observation shows the capture must end. Do not signal worker or
   component PIDs directly.

## Interpret the report

1. For `succeeded` or `partial`, call `inspect_diagnostic_report` with the JSON
   report path from status. A partial report remains evidence; distinguish
   collection failures from PostgreSQL findings.
2. Correlate the selected intent with measured signals:
   sessions and waits for `locks`; throughput, latency, WAL, checkpoints, and
   disk charts for `io`; CPU, memory, SQL time, and context-switch signals for
   `cpu`; all of these for `performance`.
3. Cite item ids and measured values. State the observation window and whether
   pg_stat_statements, pg_stat_kcache, or pg_wait_sampling data was unavailable.
4. Recommend the smallest next action. Do not terminate sessions, apply a
   configuration candidate, or claim causality when several signals changed.

## Preserve failure evidence

- A capture id is immutable and cannot be reused.
- If the worker is lost, preserve its plan, state, events, worker log, and any
  unverified partial outputs. Let status verify and terminate only the recorded
  orphan component process; never signal a copied PID. Start a new capture with
  a new id; snapshots from separate time windows must not be silently merged.
- Never weaken SSH host verification or replace the versioned item allowlist
  with arbitrary SQL, shell commands, tags, or item ids.
