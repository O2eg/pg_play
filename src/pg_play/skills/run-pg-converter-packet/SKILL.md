---
name: run-pg-converter-packet
description: Plan, authorize, start, monitor, cancel, and safely hand off reviewed PG Converter packet runs through the pg_play MCP server. Use when a user asks to deploy or run an existing migration, maintenance, read-only, no-commit, export-data, SQL, or Python packet against one or more configured PostgreSQL aliases. Do not use this skill to author arbitrary SQL, pass credentials or packet bodies through MCP, invoke administrative recovery operations, or automatically retry an ambiguous packet outcome.
---

# Run PG Converter Packet

Use only `plan_converter_run`, `start_converter_run`,
`converter_run_status`, `converter_run_events`, and `cancel_converter_run`.
Do not construct raw component commands.

## Plan and review

1. Collect the local project directory, INI configuration path, packet name,
   database selector, timeout, and optional protected placeholder/config-
   override JSON file paths. Keep passwords in the INI file. Never request a
   password-bearing DSN, inline placeholder values, SQL, Python, or private
   file contents.
2. Call `plan_converter_run`. Resolve every plan error without weakening its
   sequential execution, source validation, or safety restrictions.
3. Present the exact database aliases and password-free connection identities,
   `execute_sql` mode, packet type, legacy tracker checksum, SHA-256 source-tree
   hash, step names, Python-step flag, multi-database flag, tracker-
   initialization risk, timeout, and final `plan_hash`.
4. Explain that every packet run is treated as a target mutation. A nominally
   read-only packet can still initialize or update tracker state, and an
   export-data packet can run setup or cleanup actions.
5. Obtain explicit authorization before calling `start_converter_run`.

## Start and observe

1. Choose a unique run id and a dedicated output directory. Pass the unchanged
   plan object and exact `plan_hash` to `start_converter_run`.
2. Save the returned run directory. Poll `converter_run_status` and page
   through `converter_run_events`, passing `last_sequence` as the next
   `after_sequence` cursor.
3. Treat `queued` and `running` as active,
   `effective_state=cancelling` as cancellation in progress, and `succeeded`,
   `partial`, `failed`, `cancelled`, or `interrupted` as terminal.
4. Call `cancel_converter_run` only when the user requests cancellation or an
   externally confirmed safety condition requires stopping. Supply a concise
   audit reason; never signal recorded PIDs directly.

## Preserve ambiguous outcomes

- Never automatically retry, resume, or reuse a run id after `partial`,
  `failed`, `cancelled`, or `interrupted`. Non-transactional and maintenance
  actions may have completed with `outcome_unknown`.
- Preserve the plan, state, events, worker log, and compact result. Ask an
  operator to inspect target PostgreSQL and follow the PG Converter tracker,
  lock, and ambiguous-outcome runbooks before building a new plan.
- Do not invoke or suggest that MCP invoked `--force`, `--wipe`, `--unlock`,
  `--stop`, template copying, or skip-on-cancel modes. They remain separate,
  explicit operator actions.
- Machine execution suppresses SQL logging and raw step results. Export paths
  are withheld because legacy random-password exports can embed the password
  in a filename; report only export counts and direct the operator to the
  protected packet directory.
