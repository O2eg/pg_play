---
name: recover-postgres-experiment
description: Inspect, cancel, and safely resume durable pg_play experiment runs through high-level MCP operations. Use when a run is queued, running, cancelling, failed, cancelled, or interrupted; when the MCP server or detached worker disappeared; when a user asks to stop, resume, recover, or explain a stuck run; or when event history and artifact validation must determine whether completed work can be reused.
---

# Recover PostgreSQL Experiment

Use only `experiment_status`, `experiment_events`, `cancel_experiment`, and
`resume_experiment`. Do not edit durable run files or signal recorded PIDs.

## Inspect the run

1. Identify the exact manifest path and `run_id`.
2. Call `experiment_status` and record `state`, `attempt`, `plan_hash`, worker
   information, the latest step, cancellation data, and error.
3. Page through `experiment_events` from `after_sequence=0`. Continue with the
   returned `last_sequence` until `has_more` is false.
4. Use state and events together. Do not infer failure or completion from
   elapsed time, a disconnected MCP client, or a partial worker log.

## Choose the operation

- For `queued` or `running`, continue monitoring unless the user explicitly
  requests cancellation.
- For `cancelling`, continue monitoring. Cancellation is cooperative and is not
  complete until state becomes `cancelled` or the run finishes.
- For `succeeded` or `partial`, do not resume. Report artifacts and explain any
  partial collection.
- For `failed`, `cancelled`, or `interrupted`, inspect the last failed or
  incomplete step and continue with the resume workflow.
- For `not_found`, verify the manifest's artifact root and the exact `run_id`;
  never create replacement state by hand.

## Cancel safely

1. Call `cancel_experiment` only after the user requests a stop. Include a
   concise operational reason.
2. Poll status and events after the request. Treat `effective_state` equal to
   `cancelling` as pending, not terminal.
3. Let pg_play verify and terminate its owned component process group. Never
   use shell commands, Docker commands, `kill`, or a PID copied from state.

## Resume safely

1. Keep the original manifest, `run_id`, and state `plan_hash`. A resumed
   attempt is not a new experiment and must not use a recalculated replacement
   hash.
2. Call `resume_experiment` only from `failed`, `cancelled`, or `interrupted`.
3. Let pg_play verify the manifest, stored plan, PostgreSQL parameters,
   component versions, safe resume policies, orphan-process identity, and
   recorded artifacts.
4. Poll status and events for the new attempt. Report which steps were reused,
   invalidated, or rerun.

Interpret recovery checks as follows:

- A missing or changed non-core step artifact may be invalidated and rerun only
  when that step has an allowlisted safe policy.
- A changed manifest, plan, configuration artifact, component version, unknown
  step, invalid resume policy, or unverifiable process identity blocks resume.
- When resume is blocked, preserve the run as evidence. Restore the exact
  prerequisite or ask the user to authorize a new planned experiment; never
  weaken the check.

## Guardrails

- Do not call synchronous `run_experiment` as a substitute for resume.
- Do not reuse a `run_id` with a changed manifest or plan.
- Do not delete partial reports, state, events, cancellation archives, or worker
  logs while diagnosing recovery.
- Do not automatically tear down the stand. Teardown requires a separate,
  explicit user request.
