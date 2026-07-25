# pg_converter packet runs through pg_play

`pg_play` exposes a bounded, typed workflow for reviewed `pg_converter`
packets. It does not expose raw component arguments, SQL, Python source,
tracker deletion, backend termination, or `--force` through MCP.

## Prerequisites

- `pg_converter` and `pg_play` are installed in the same environment;
- the packet project is available on the MCP server host;
- the INI configuration and optional JSON input files are readable only by the
  runtime account;
- packet SQL/Python and its source-control revision were reviewed separately;
- the selected role has only the PostgreSQL privileges the packet requires.

The MCP server uses local file references. It does not accept a DSN, password,
packet body, SQL string, or Python source in a tool call.

## Plan

Call:

```text
plan_converter_run(
  project_directory,
  config_file,
  packet_name,
  database_selector="ALL",
  placeholders_file=null,
  config_overrides_file=null,
  timeout_seconds=3600
)
```

The operation invokes only the non-connecting `pg_converter --plan` path.
The result contains:

- `pg_converter/plan-v1` with the exact alias set and password-free connection
  identities;
- the v1-compatible MD5 tracker checksum;
- the SHA-256 packet-tree hash, including filenames and nested data;
- step names and an explicit Python-step risk flag;
- safe configuration and protected input-file hashes without credentials or
  values;
- a `pg_play/converter-run-plan-v1` wrapper and final `plan_hash`.

Review at least the packet name/type, both hashes, database aliases,
`execute_sql`, Python-step flag, tracker-initialization flag, and timeout.

## Start and observe

Pass the unchanged plan object and hash:

```text
start_converter_run(plan, plan_hash, output_directory, run_id)
```

`run_id` is immutable and may contain letters, digits, `_`, `-`, and `.`. The
new run directory is created with mode `0700`; files use `0600`.

The call returns after starting a detached worker. Continue with:

```text
converter_run_status(run_directory)
converter_run_events(run_directory, after_sequence=0, limit=1000)
```

Use the returned `last_sequence` as the next cursor. Terminal states are
`succeeded`, `partial`, `failed`, and `cancelled`. A vanished worker becomes
`interrupted`.

The run directory contains:

```text
RUN_ID/
|-- plan.json
|-- state.json
|-- events.jsonl
|-- result.json
|-- worker.log
|-- active-process.json       # only while the component is active
`-- cancel.request.json       # only after cancellation is requested
```

`result.json` contains the common component envelope and compact
`pg_converter/machine-result-v1`. Raw step result sets and connection strings
are not copied into durable pg_play state. Machine execution forces SQL logging
off. Export paths are also withheld because the legacy random-password export
mode can embed its password in a filename; use the per-database export counts
and inspect the protected packet directory as an operator.

## Cancellation and recovery

Call `cancel_converter_run(run_directory, reason)` only for an active run and
provide a concise audit reason. `pg_play` records the request and terminates
only the process whose PID start time, operating-system owner, and executable
identity match the active-process record.

There is deliberately no automatic resume tool for packet runs. Transactional
actions retain PG Converter's tracker guarantees, but a non-transactional or
maintenance action can finish with an ambiguous outcome. After `failed`,
`cancelled`, or `interrupted`:

1. inspect `state.json`, `events.jsonl`, `worker.log`, and target PostgreSQL;
2. follow the PG Converter `outcome_unknown` and lock recovery runbooks;
3. resolve target state explicitly;
4. build and review a new plan before starting a new immutable run id.

MCP cancellation does not authorize `wipe`, `unlock`, `stop`, or `force`.
Those remain direct operator actions.

## Agent Skill

The wheel packages `run-pg-converter-packet`. It restricts an agent to the
five typed converter tools, requires explicit authorization after showing the
resolved endpoints and source hashes, and hands partial, failed, cancelled, or
interrupted outcomes to an operator instead of retrying them.
