"""Optional database diagnostics and independent verification for migration labs."""

from __future__ import annotations

import asyncio

from pg_play.logical import LogicalError, file_reference
from pg_play.runner import ComponentInvocation
from pg_play.state import write_json


def exact_oracle(config_path, *, deferred=False):
    from pg_logical_repl.config import load
    from pg_logical_repl.db import Database
    from pg_logical_repl.security import Redactor

    from pg_play.logical_oracle import compare

    config = load(config_path)
    redactor = Redactor()

    async def run():
        async with Database(config, "source", redactor).connect(readonly=True) as source:
            async with Database(config, "target", redactor).connect(readonly=True) as target:
                return await compare(
                    source,
                    target,
                    executor="lc_ddl_executor_" + config.migration_id,
                    deferred=deferred,
                )

    try:
        return asyncio.run(run())
    except AssertionError as exc:
        # The comparator deliberately emits only object names and row positions.
        return dict(passed=False, comparator="independent-exact-stream", message=str(exc))
    except Exception:
        raise LogicalError(
            "Independent comparison could not complete; inspect database availability"
        ) from None


def capture(manager, directory, plan, side):
    from pg_logical_repl.config import load
    from pg_logical_repl.db import Database
    from pg_logical_repl.process import pg_environment
    from pg_logical_repl.security import Redactor

    config = load(plan["config"])
    endpoint = config.endpoint(side)
    root = directory / "diagnostics" / side
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = root / "report.json"
    duration = max(30, min(900, int(plan["duration_seconds"])))
    try:
        with pg_environment(Database(config, side, Redactor()), root) as environment:
            arguments = (
                "snapshots",
                "--host",
                endpoint["host"],
                "--port",
                str(endpoint["port"]),
                "--database",
                endpoint["database"],
                "--user",
                endpoint["user"],
                "--collection-mode",
                "remote-db-only",
                "--disable-ddl",
                "--item-id=[overview.server_version,overview.database_stats,"
                "activity_locks.session_states,wal_io_checkpoints.wal_statistics,"
                "snapshot_charts_db.database_transaction_rate,snapshot_charts_db.wal_growth_rate]",
                "--duration-seconds",
                str(duration),
                "--interval-seconds",
                "5",
                "--out",
                str(root),
                "--json-out",
                str(output),
                "--html-out",
                str(root / "report.html"),
            )
            if environment.get("PGPASSFILE"):
                arguments += ("--passfile", environment["PGPASSFILE"])
            envelope = manager.runner.run(
                ComponentInvocation(
                    component="pg_diag",
                    arguments=arguments,
                    request_id=f"logical-{directory.name}-{side}",
                    environment={k: v for k, v in environment.items() if k.startswith("PG")},
                    timeout_seconds=duration + 180,
                    cancel_path=directory / "stop.request.json",
                    active_process_path=directory / f"diagnostic-{side}-process.json",
                )
            )
        write_json(root / "result.json", envelope)
        passed = envelope["status"] == "succeeded" and output.is_file()
        if output.is_file():
            checked = manager.runner.run(
                ComponentInvocation(
                    component="pg_diag",
                    arguments=("validate-artifact", str(output)),
                    request_id=f"logical-{directory.name}-{side}-validate",
                )
            )
            passed = passed and checked["status"] == "succeeded"
        return dict(
            passed=passed,
            status=envelope["status"],
            report=file_reference(output) if output.is_file() else None,
            collection_mode="remote-db-only",
        )
    except Exception:
        return dict(passed=False, status="failed", error="Diagnostic capture did not complete")
