"""Long-running maintenance activity for the demo window (real sessions only).

Started by run_report.py. IDLE_DELAY seconds after start one idle REPEATABLE READ transaction
is opened and kept. OPS_DELAY seconds after start a CPU-bound CREATE INDEX, a slow COPY, a
throttled VACUUM on trace_probe.io_ring and a row-lock chain (one holder on lab.orders id=1 plus
three waiters with lock_timeout=0 and auto_explain disabled) start together, so
pg_stat_progress_*, the blocking tree and long-transaction items have rows at the point-in-time
collection and in the snapshots. Everything is cancelled at the stop file or at MAX_SECONDS.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import asyncpg
from lab_common import Lab, add_common_arguments, record_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument("--idle-delay", type=int, default=0)
    parser.add_argument("--ops-delay", type=int, default=65)
    parser.add_argument("--max-seconds", type=int, default=505)
    return parser.parse_args()


class Progress:
    def __init__(self, lab: Lab, args: argparse.Namespace) -> None:
        self.lab = lab
        self.args = args
        staged = lab.params["staged_activity"]
        self.index_rows = int(staged["index_rows"])
        self.copy_rows = int(staged["copy_rows"])
        self.lock_hold = int(staged["lock_hold_seconds"])
        self.started = time.monotonic()
        self.log = lab.evidence("progress.jsonl")
        self.min_free = float(lab.params["guards"]["min_free_gib"])

    def record(self, event: str, **fields: object) -> None:
        record_event(self.log, self.started, event, **fields)

    def active(self) -> bool:
        return (
            time.monotonic() - self.started < self.args.max_seconds
            and not self.lab.stop_file.exists()
            and self.lab.free_gib() > self.min_free
        )

    async def wait_until(self, seconds: float) -> bool:
        while time.monotonic() - self.started < seconds and self.active():
            await asyncio.sleep(1)
        return self.active()

    async def conn(self, name: str, **extra: str) -> asyncpg.Connection:
        settings = {
            "application_name": name,
            "statement_timeout": "900s",
            "idle_in_transaction_session_timeout": "900s",
            "lock_timeout": "30s",
        }
        settings.update(extra)  # waiters override lock_timeout and auto_explain
        return await self.lab.connect(**settings)

    async def guarded(self, coro, label: str) -> None:
        try:
            await coro
            self.record(label, outcome="completed")
        except asyncio.CancelledError:
            self.record(label, outcome="cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - evidence, not control flow
            self.record(label, outcome="error", error=type(error).__name__, detail=str(error)[:200])

    async def lock_chain(self, tasks: list, connections: list) -> None:
        # A row lock, never LOCK TABLE: waiters queue on the holder's transaction id, so
        # pg_blocking_pids shows a tree while catalog items (pg_relation_size) stay unblocked.
        holder = await self.conn("demo_lock_holder")
        connections.append(holder)
        await holder.execute("BEGIN; UPDATE lab.orders SET status = status WHERE id = 1")
        self.record("lock_holder_acquired", hold_seconds=self.lock_hold, lock="row lab.orders id=1")
        for index in range(3):
            waiter = await self.conn(
                f"demo_lock_waiter_{index + 1}",
                lock_timeout="0",
                **{
                    "auto_explain.log_min_duration": "-1"
                },  # waiting UPDATEs stay out of the plans chart
            )
            connections.append(waiter)
            tasks.append(
                asyncio.create_task(
                    self.guarded(
                        waiter.execute("UPDATE lab.orders SET amount = amount WHERE id = 1"),
                        f"lock_waiter_{index + 1}",
                    )
                )
            )

        async def release() -> None:
            await asyncio.sleep(self.lock_hold)
            await holder.execute("ROLLBACK")
            self.record("lock_holder_released")

        tasks.append(asyncio.create_task(self.guarded(release(), "lock_holder")))

    async def main(self) -> None:
        self.record(
            "started",
            idle_delay=self.args.idle_delay,
            ops_delay=self.args.ops_delay,
            max_seconds=self.args.max_seconds,
        )
        idle = None
        tasks: list = []
        connections: list = []
        try:
            if not await self.wait_until(self.args.idle_delay):
                self.record("skipped")
                return
            idle = await self.conn("demo_idle_transaction")
            # CPU-bound (~3-12 ms/row on the 2-core stand) so CREATE INDEX shows up as CPU in
            # pg_stat_kcache, not as pg_sleep waits.
            await idle.execute(
                f"""
                CREATE TABLE IF NOT EXISTS trace_probe.progress_index AS
                    SELECT i FROM generate_series(1, 5000) i;
                CREATE TABLE IF NOT EXISTS trace_probe.progress_copy(id int, payload text);
                TRUNCATE trace_probe.progress_copy;
                DROP INDEX IF EXISTS trace_probe.trace_slow_index;
                INSERT INTO trace_probe.progress_index
                    SELECT i FROM generate_series(
                        (SELECT coalesce(max(i), 0) + 1 FROM trace_probe.progress_index),
                        {self.index_rows}) i;
                CREATE OR REPLACE FUNCTION trace_probe.slow_identity(i integer) RETURNS integer
                    LANGUAGE plpgsql IMMUTABLE AS $$
                    DECLARE h text := i::text;
                    BEGIN FOR n IN 1..4000 LOOP h := md5(h); END LOOP; RETURN i; END $$;
                """
            )
            await idle.execute(
                "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT pg_current_xact_id(); "
                "SELECT count(*) FROM trace_probe.scratch;"
            )
            self.record("idle_transaction_opened")
            if not await self.wait_until(self.args.ops_delay):
                self.record("skipped_ops")
                return
            index = await self.conn("demo_create_index")
            copy = await self.conn("demo_copy")
            vacuum = await self.conn("demo_vacuum")
            connections += [index, copy, vacuum]
            tasks.append(
                asyncio.create_task(
                    self.guarded(
                        index.execute(
                            "CREATE INDEX trace_slow_index ON trace_probe.progress_index"
                            "(trace_probe.slow_identity(i))"
                        ),
                        "create_index",
                    )
                )
            )

            async def records():
                for row in range(self.copy_rows):
                    yield row, "bounded live COPY progress"
                    await asyncio.sleep(0.03)

            tasks.append(
                asyncio.create_task(
                    self.guarded(
                        copy.copy_records_to_table(
                            "progress_copy", schema_name="trace_probe", records=records()
                        ),
                        "copy",
                    )
                )
            )
            await vacuum.execute("SET vacuum_cost_delay = '10ms'; SET vacuum_cost_limit = 50")
            tasks.append(
                asyncio.create_task(
                    self.guarded(
                        vacuum.execute(
                            "VACUUM (DISABLE_PAGE_SKIPPING, ANALYZE) trace_probe.io_ring"
                        ),
                        "vacuum_io_ring",
                    )
                )
            )
            await self.lock_chain(tasks, connections)
            self.record("operations_started")
            while self.active() and not all(task.done() for task in tasks):
                await asyncio.sleep(2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in connections:
                try:
                    await asyncio.wait_for(connection.close(), 10)
                except Exception:  # noqa: BLE001
                    connection.terminate()
            if idle is not None:
                try:
                    await idle.execute("ROLLBACK")
                except Exception as error:  # noqa: BLE001
                    self.record("rollback_error", error=type(error).__name__)
                await idle.close()
            self.record("stopped")


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(Progress(Lab.from_args(arguments), arguments).main())
