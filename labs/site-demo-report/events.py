"""Bounded real PostgreSQL incidents for the demo window; no fabricated log records.

Every cycle (pause events_cycle_pause_seconds): auto_explain plans in four formats, a trigger
update, a sort spilling to disk, a WARNING, churn updates, a missing-file ERROR (58P01), a
statement_timeout cancellation (57014), a deadlock pair (40P01) and a failed login (28P01).
Every second cycle a manual CHECKPOINT, every third cycle a real archiver failure (flag file
in the container, cleared immediately) and a connection-capacity probe (53300). In parallel a
lock-wait loop (3 s row lock plus a 500 ms VACUUM timeout under ACCESS EXCLUSIVE).
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import time

import asyncpg
from lab_common import Lab, add_common_arguments, record_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument("--seconds", type=int, required=True, help="helper lifetime")
    return parser.parse_args()


class Events:
    def __init__(self, lab: Lab, seconds: int) -> None:
        self.lab = lab
        self.seconds = seconds
        self.started = time.monotonic()
        self.log = lab.evidence("events.jsonl")
        self.pause = int(lab.params["staged_activity"]["events_cycle_pause_seconds"])
        self.min_free = float(lab.params["guards"]["min_free_gib"])
        self.admin = lab.superuser_password()
        self.password = lab.workload_password()
        self.archive_flag = "/tmp/pg-trace-archive-fail"  # archive_command fails while it exists

    def record(self, event: str, **fields: object) -> None:
        record_event(self.log, self.started, event, **fields)

    def active(self) -> bool:
        return (
            time.monotonic() - self.started < self.seconds
            and not self.lab.stop_file.exists()
            and self.lab.free_gib() > self.min_free
        )

    async def connect(
        self, name: str, privileged: bool = False, **settings: str
    ) -> asyncpg.Connection:
        return await asyncpg.connect(
            host="127.0.0.1",
            port=self.lab.port,
            user="postgres" if privileged else self.lab.params["workload"]["user"],
            password=self.admin if privileged else self.password,
            database=self.lab.database,
            timeout=5,
            command_timeout=12,
            server_settings={"application_name": f"trace_{name}", **settings},
        )

    async def expected(self, connection: asyncpg.Connection, sql: str, label: str) -> None:
        try:
            await connection.execute(sql)
            self.record(label, outcome="completed")
        except asyncpg.PostgresError as error:
            self.record(label, sqlstate=error.sqlstate, error=type(error).__name__)

    def container(self, *command: str) -> None:
        subprocess.run(["docker", "exec", self.lab.container, *command], check=True)

    async def locks(self) -> None:
        while self.active():
            holder = waiter = None
            try:
                holder = await self.connect("lock_holder")
                waiter = await self.connect("lock_waiter")
                await holder.execute(
                    "BEGIN; UPDATE trace_probe.accounts SET balance = balance WHERE id = 1"
                )
                task = asyncio.create_task(
                    self.expected(
                        waiter,
                        "UPDATE trace_probe.accounts SET balance = balance + 1 WHERE id = 1",
                        "lock_wait",
                    )
                )
                await asyncio.sleep(3)
                await holder.execute("ROLLBACK")
                await task
                await holder.execute("BEGIN; LOCK trace_probe.churn IN ACCESS EXCLUSIVE MODE")
                await waiter.execute("SET statement_timeout = '500ms'")
                await self.expected(
                    waiter, "VACUUM (ANALYZE) trace_probe.churn", "maintenance_timeout"
                )
                await holder.execute("ROLLBACK")
            except Exception as error:  # noqa: BLE001
                self.record("lock_loop_error", error=type(error).__name__)
            finally:
                for connection in (holder, waiter):
                    if connection:
                        await connection.close()
            await asyncio.sleep(7)

    async def deadlock(self) -> None:
        a = await self.connect("deadlock_a")
        b = await self.connect("deadlock_b")
        try:
            await a.execute("BEGIN; UPDATE trace_probe.accounts SET balance = balance WHERE id = 1")
            await b.execute("BEGIN; UPDATE trace_probe.accounts SET balance = balance WHERE id = 2")

            async def finish(connection: asyncpg.Connection, sql: str) -> None:
                try:
                    await self.expected(connection, sql, "deadlock")
                finally:
                    await connection.execute("ROLLBACK")

            await asyncio.gather(
                finish(a, "UPDATE trace_probe.accounts SET balance = balance WHERE id = 2"),
                finish(b, "UPDATE trace_probe.accounts SET balance = balance WHERE id = 1"),
            )
        finally:
            await a.close()
            await b.close()

    async def capacity(self) -> None:
        connections: list[asyncpg.Connection] = []
        failures: dict[str, int] = {}
        try:
            # Mostly idle backends consume slots briefly, not CPU or a large work_mem budget.
            for _ in range(100):
                try:
                    connections.append(await self.connect("connection_capacity"))
                except asyncpg.PostgresError as error:
                    failures[error.sqlstate] = failures.get(error.sqlstate, 0) + 1
            self.record("connection_capacity", connected=len(connections), failures=failures)
            await asyncio.sleep(2)
        finally:
            await asyncio.gather(*(c.close() for c in connections), return_exceptions=True)

    async def scenarios(self) -> None:
        cycle = 0
        while self.active():
            connection = None
            try:
                connection = await self.connect("events", True)
                for fmt in ("json", "text", "yaml", "xml"):
                    await connection.execute(f"SET auto_explain.log_format = '{fmt}'")
                    await connection.execute(
                        "SELECT trace_probe.observe(15000), pg_sleep(1.0) "
                        "/* trace_plan_format_details */"
                    )
                await connection.execute(
                    "SET auto_explain.log_format = 'json'; SET work_mem = '64kB'"
                )
                await connection.execute(
                    "WITH pause AS MATERIALIZED (SELECT pg_sleep(1.0)) "
                    "UPDATE trace_probe.trigger_details SET value = $1 FROM pause WHERE id = 1",
                    f"synthetic_trigger_cycle_{cycle}",
                )
                await connection.fetchval(
                    "SELECT count(*) FROM (SELECT i, md5(i::text) FROM generate_series(1, 30000) i "
                    "ORDER BY md5(i::text)) s"
                )
                await connection.execute(
                    "DO $$ BEGIN RAISE WARNING 'Controlled diagnostic workload warning'; END $$"
                )
                await connection.execute(
                    "UPDATE trace_probe.churn SET payload = md5(random()::text) WHERE i % 5 = 0"
                )
                await self.expected(
                    connection,
                    "SELECT pg_read_file('/tmp/pg_trace_missing_control_file')",
                    "missing_file",
                )
                await connection.execute("SET statement_timeout = '150ms'")
                await self.expected(connection, "SELECT pg_sleep(0.5)", "statement_timeout")
                await connection.execute("SET statement_timeout = '12s'")
                if cycle % 2 == 0:
                    await connection.execute("CHECKPOINT")
                    self.record("checkpoint")
                if cycle % 3 == 0:
                    # A real archiver failure, cleared immediately; retained WAL stays small.
                    self.container("touch", self.archive_flag)
                    await connection.execute("SELECT pg_switch_wal()")
                    await asyncio.sleep(3)
                    self.container("rm", "-f", self.archive_flag)
                    await connection.execute("SELECT pg_switch_wal()")
                    self.record("archive_failure_cycle")
                await connection.close()
                connection = None
                await self.deadlock()
                try:
                    bad = await asyncpg.connect(
                        host="127.0.0.1",
                        port=self.lab.port,
                        user=self.lab.params["workload"]["user"],
                        password="deliberately-invalid-trace-password",
                        database=self.lab.database,
                        timeout=3,
                    )
                    await bad.close()
                except asyncpg.PostgresError as error:
                    self.record("authentication_failure", sqlstate=error.sqlstate)
                if cycle % 3 == 1:
                    await self.capacity()
            except Exception as error:  # noqa: BLE001
                self.record("scenario_error", error=type(error).__name__, detail=str(error)[:250])
            finally:
                if connection:
                    await connection.close()
            cycle += 1
            await asyncio.sleep(self.pause)

    async def main(self) -> None:
        self.record("started", max_duration=self.seconds)
        try:
            await asyncio.gather(self.locks(), self.scenarios())
        finally:
            subprocess.run(
                ["docker", "exec", self.lab.container, "rm", "-f", self.archive_flag],
                stdout=subprocess.DEVNULL,
                check=False,
            )
            self.record("stopped")


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(Events(Lab.from_args(arguments), arguments.seconds).main())
