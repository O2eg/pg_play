"""Prepare (or re-prepare) the stand data for the demo report; run once, not per report.

Steps: sizes before → optional DROP SCHEMA of disabled profiles → lift database-level timeouts
for the bulk load → `pg-workload install` of the selected profiles at --scale with the exact
arguments pg_play uses → restore database-level timeouts → lab_* roles and objects
(setup/site-lab-users.sql, tolerant to existing roles) → the deliberately invalid index
trace_probe.trace_churn_dup_demo → demo grants → lab run-time defaults from
setup/observability-overrides.json (statement_timeout, temp_file_limit) → pg_stat_statements
reset → sizes after → setup/prepare-summary.json.

The stand itself (pg_stand project at <root>/stand, config stand/configs/trace.yaml) is created by
`pg-stand up` / the pg_play stand step; see README.md.

Usage: prepare_stand.py [--scale 3.0] [--profiles a,b,...] [--drop-schemas x,y] [--prepare-db]
                        [--skip-install] [--skip-lab-objects]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg
from lab_common import LAB_DIR, Lab, add_common_arguments, say

DB_TIMEOUTS = ("statement_timeout", "idle_in_transaction_session_timeout", "lock_timeout")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--scale", help="data scale for pg-workload install (params: workload.scale)"
    )
    parser.add_argument(
        "--profiles", help="comma-separated profiles to install (default: params.workload.profiles)"
    )
    parser.add_argument(
        "--drop-schemas", default="", help="comma-separated schemas to drop before installing"
    )
    parser.add_argument(
        "--prepare-db", action="store_true", help="run pg-workload prepare-db first (fresh stand)"
    )
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-lab-objects", action="store_true")
    return parser.parse_args()


async def db_timeouts(lab: Lab, lift: bool) -> None:
    connection = await lab.connect("postgres")
    try:
        for name in DB_TIMEOUTS:
            statement = (
                f"ALTER DATABASE {lab.database} SET {name} = 0"
                if lift
                else f"ALTER DATABASE {lab.database} RESET {name}"
            )
            await connection.execute(statement)
    finally:
        await connection.close()


async def sizes(lab: Lab, label: str, summary: dict[str, Any]) -> None:
    connection = await lab.connect()
    try:
        rows = await connection.fetch(
            "select n.nspname as schema, "
            "pg_size_pretty(sum(pg_total_relation_size(c.oid))) as size, "
            "sum(pg_total_relation_size(c.oid)) as bytes "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where c.relkind in ('r', 'p', 'm') and n.nspname !~ '^pg_' "
            "and n.nspname <> 'information_schema' "
            "group by 1 order by 3 desc limit 12"
        )
        total = await connection.fetchval(
            "select pg_size_pretty(pg_database_size(current_database()))"
        )
        invalid = [
            r["index"]
            for r in await connection.fetch(
                "select indexrelid::regclass::text as index from pg_index where not indisvalid"
            )
        ]
        roles = await connection.fetchval(
            "select count(*) from pg_roles where rolname like 'lab\\_%'"
        )
    finally:
        await connection.close()
    summary[label] = {
        "database": total,
        "schemas": [(r["schema"], r["size"]) for r in rows],
        "invalid_indexes": invalid,
        "lab_roles": roles,
        "free_gib": round(lab.free_gib(), 1),
    }
    say(
        f"{label}: db={total}, free={summary[label]['free_gib']} GiB, lab roles={roles}, "
        f"invalid={invalid}, top={summary[label]['schemas'][:6]}"
    )


def workload(
    lab: Lab, context: dict[str, Any], action: str, *extra: str, scale: str | None = None
) -> float:
    command, env = lab.workload_command(context, action, *extra)
    if scale is not None and "--scale" in command:
        command[command.index("--scale") + 1] = scale
    say(f"pg-workload {action} {' '.join(extra) if extra else ''}".rstrip())
    started = time.monotonic()
    with (lab.root / "setup" / f"prepare-{action}.log").open("w") as log:
        subprocess.run(command, env=env, check=True, stdout=log, stderr=subprocess.STDOUT)
    seconds = round(time.monotonic() - started, 1)
    say(f"pg-workload {action} done in {seconds} s")
    return seconds


async def lab_objects(lab: Lab, summary: dict[str, Any]) -> None:
    psql = (
        shutil.which("psql")
        if not os.path.exists(lab.stand["psql_binary"])
        else lab.stand["psql_binary"]
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = lab.superuser_password()
    # Roles are cluster-wide and survive a database re-creation: run without ON_ERROR_STOP and
    # count the "already exists" errors instead.
    completed = subprocess.run(
        [
            psql,
            "-h",
            "127.0.0.1",
            "-p",
            str(lab.port),
            "-U",
            "postgres",
            "-d",
            lab.database,
            "-X",
            "-f",
            str(LAB_DIR / "setup/site-lab-users.sql"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    (lab.root / "setup/prepare-lab-users.log").write_text(completed.stdout + completed.stderr)
    errors = re.findall(r"^psql:.*ERROR:\s+(.*)$", completed.stderr, re.MULTILINE)
    unexpected = [e for e in errors if "already exists" not in e]
    summary["lab_users_sql"] = {"errors": len(errors), "unexpected": unexpected}
    say(
        f"lab_* roles and objects applied: {len(errors)} error line(s), "
        f"{len(unexpected)} unexpected"
    )
    connection = await lab.connect()
    try:
        try:
            await connection.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY trace_churn_dup_demo "
                "ON trace_probe.churn ((i % 10))"
            )
            summary["invalid_index_demo"] = "unexpectedly succeeded"
        except asyncpg.PostgresError as error:
            summary["invalid_index_demo"] = {
                "sqlstate": error.sqlstate,
                "message": str(error)[:120],
            }
        for grant in lab.params.get("demo_grants") or []:
            await connection.execute(grant)
    finally:
        await connection.close()
    say(
        f"invalid index demo: {summary['invalid_index_demo']}; "
        f"{len(lab.params.get('demo_grants') or [])} demo grant(s)"
    )


async def lab_defaults(lab: Lab) -> None:
    overrides = json.loads((LAB_DIR / "setup/observability-overrides.json").read_text())
    connection = await lab.connect("postgres")
    try:
        for name in ("statement_timeout", "temp_file_limit"):
            await connection.execute(f"ALTER SYSTEM SET {name} = '{overrides[name]}'")
        await connection.execute("SELECT pg_reload_conf()")
    finally:
        await connection.close()
    workload_db = await lab.connect()
    try:
        await workload_db.execute("SELECT pg_stat_statements_reset()")
    finally:
        await workload_db.close()
    say(
        f"lab defaults: statement_timeout={overrides['statement_timeout']}, "
        f"temp_file_limit={overrides['temp_file_limit']}; pg_stat_statements reset"
    )


async def main() -> None:
    args = parse_args()
    lab = Lab.from_args(args)
    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "arguments": vars(args),
    }
    lab.write_manifest()
    context = lab.pg_play_context()
    profiles = (
        args.profiles.split(",") if args.profiles else list(lab.params["workload"]["profiles"])
    )
    scale = args.scale or str(lab.params["workload"]["scale"])
    await sizes(lab, "before", summary)
    if not args.skip_install:
        drop = [s for s in args.drop_schemas.split(",") if s]
        if drop:
            connection = await lab.connect()
            try:
                for schema in drop:
                    await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                    say(f"dropped schema {schema}")
            finally:
                await connection.close()
        connection = await lab.connect("postgres")
        try:
            for name, value in lab.params["stand_run_settings"].items():
                await connection.execute(f"ALTER SYSTEM SET {name} = '{value}'")
            await connection.execute("SELECT pg_reload_conf()")
        finally:
            await connection.close()
        await db_timeouts(lab, lift=True)
        try:
            if args.prepare_db:
                summary["prepare_db_seconds"] = workload(lab, context, "prepare-db")
            profile_args = [x for p in profiles for x in ("--profile", p)]
            summary["install_seconds"] = workload(
                lab, context, "install", *profile_args, scale=scale
            )
            summary["installed"] = {"scale": scale, "profiles": profiles}
        finally:
            await db_timeouts(lab, lift=False)
    if not args.skip_lab_objects:
        await lab_objects(lab, summary)
    await lab_defaults(lab)
    await sizes(lab, "after", summary)
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    (lab.root / "setup/prepare-summary.json").write_text(json.dumps(summary, indent=2, default=str))
    say("PREPARE COMPLETED")


if __name__ == "__main__":
    asyncio.run(main())
