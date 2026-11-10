"""Durable logical operations and bounded controller sessions."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from pg_play.logical import LogicalClient, LogicalError, checked_plan, file_reference
from pg_play.runner import (
    ComponentCancelledError,
    process_boot_id,
    process_executable,
    process_start_ticks,
    recorded_process_is_alive,
    terminate_recorded_process,
)
from pg_play.state import append_event, exclusive_lock, read_events, read_state, utc_now, write_json

STATE_SCHEMA = "pg_play/logical-run-v1"
ACTIVE = {"queued", "running"}
BAD_MIGRATION_STATES = {"BLOCKED", "ABORT_CLEANUP_PENDING", "DETACH_CLEANUP_PENDING"}


def save(path, value):
    value["updated_at"] = utc_now()
    durable_json(path, value)


def durable_json(path, value):
    write_json(path, value)
    for target in (path, path.parent):
        fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def event(directory, name, **data):
    return append_event(
        directory / "events.jsonl", run_id=directory.name, event_type=name, data=data
    )


@contextmanager
def controller_lock(state_dir):
    path = Path(state_dir) / "pg-play-controller.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LogicalError("Another pg_play controller owns this migration") from None
        yield
    finally:
        os.close(fd)


class LogicalRunManager:
    def __init__(self, runner=None):
        self.client = LogicalClient(runner)
        self.runner = self.client.runner

    def plan(self, *args, **kwargs):
        return self.client.plan(*args, **kwargs)

    def inspect(self, *args, **kwargs):
        return self.client.inspect(*args, **kwargs)

    def _load(self, directory):
        directory = Path(directory).expanduser().resolve()
        state = read_state(directory / "state.json")
        if state.get("schema_version") != STATE_SCHEMA or state.get("directory") != str(directory):
            raise LogicalError("Not an owned logical run directory")
        plan = checked_plan(read_state(directory / "plan.json"), state["plan_hash"], inputs=False)
        return directory, state, plan

    def start(self, plan, plan_hash, output_directory, run_id):
        checked_plan(plan, plan_hash)
        self.client.validate(plan)
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", run_id
        ):
            raise LogicalError("Invalid run_id")
        directory = Path(output_directory).expanduser().resolve() / run_id
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(directory, 0o700)
        durable_json(directory / "plan.json", plan)
        state = dict(
            schema_version=STATE_SCHEMA,
            run_id=run_id,
            directory=str(directory),
            plan_hash=plan_hash,
            state="queued",
            migration_state=None,
            worker=None,
            created_at=utc_now(),
            elapsed_seconds=0,
            last_component_event=0,
            attempt=1,
            result=None,
            error=None,
        )
        save(directory / "state.json", state)
        event(directory, "created", operation=plan["operation"], plan_hash=plan_hash)
        with exclusive_lock(directory / "control.lock"):
            return self._spawn(directory, state)

    def _spawn(self, directory, state):
        gate = directory / "worker.starting"
        write_json(gate, {"waiting": True})
        process = None
        try:
            fd = os.open(directory / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "pg_play.logical_worker",
                        str(directory),
                        state["plan_hash"],
                        str(gate),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            state["worker"] = dict(
                pid=process.pid,
                boot_id=process_boot_id(),
                process_start_ticks=process_start_ticks(process.pid),
                executable=process_executable(process.pid),
                owner_uid=os.getuid(),
            )
            save(directory / "state.json", state)
            event(directory, "worker_started", attempt=state["attempt"])
            return state
        except BaseException:
            if process is not None:
                process.terminate()
                process.wait(timeout=10)
            state.update(state="interrupted", worker=None)
            save(directory / "state.json", state)
            raise
        finally:
            gate.unlink(missing_ok=True)

    def _reconcile(self, directory):
        state = read_state(directory / "state.json")
        if state["state"] in ACTIVE and not recorded_process_is_alive(state.get("worker") or {}):
            state.update(
                state="interrupted",
                worker=None,
                cleanup_pending=True,
                error={"code": "worker_lost", "message": "Inspect migration before resuming"},
            )
            save(directory / "state.json", state)
            event(directory, "worker_lost", replication_may_continue=True)
        if state["state"] not in ACTIVE and (
            state.get("cleanup_pending")
            or state.get("workload_owned")
            or state.get("workload_start_pending")
        ):
            errors = {}

            def cleanup(label, action):
                try:
                    action()
                except Exception as exc:
                    errors[label] = (
                        str(exc) if isinstance(exc, LogicalError) else type(exc).__name__
                    )

            # An orphan CLI may still hold database locks after the parent died.
            # Match PID start time, owner and executable before signalling it.
            for process_path in directory.glob("*process.json"):
                cleanup(process_path.name, lambda p=process_path: terminate_recorded_process(p))
            _, _, plan = self._load(directory)
            if state.get("workload_owned") or state.get("workload_start_pending"):
                from pg_play.logical_experiment import workload

                cleanup("workload", lambda: workload(self, directory, plan, False))
            from pg_play.converter_runs import ConverterRunManager

            for packet in (directory / "packets").glob("packet-*"):

                def cancel(path=packet):
                    packets = ConverterRunManager(self.runner)
                    if packets.status(path)["state"] in {"queued", "running"}:
                        packets.cancel(path, reason="Owning migration worker was lost")

                cleanup(packet.name, cancel)
            state = read_state(directory / "state.json")
            state.update(cleanup_pending=bool(errors), cleanup_errors=errors)
            save(directory / "state.json", state)
        if (directory / "stop.request.json").exists() and state["state"] in ACTIVE:
            state["effective_state"] = "stopping_controller"
        return state

    def status(self, directory):
        directory, _, _ = self._load(directory)
        with exclusive_lock(directory / "control.lock"):
            return self._reconcile(directory)

    def events(self, directory, *, after_sequence=0, limit=100):
        directory, _, _ = self._load(directory)
        return read_events(directory / "events.jsonl", after_sequence=after_sequence, limit=limit)

    def stop(self, directory, reason):
        directory, _, _ = self._load(directory)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise LogicalError("A stop reason of 1 to 500 characters is required")
        with exclusive_lock(directory / "control.lock"):
            state = self._reconcile(directory)
            if state["state"] not in ACTIVE:
                return state
            write_json(directory / "stop.request.json", dict(reason=reason, created_at=utc_now()))
            event(
                directory, "controller_stop_requested", reason=reason, replication_may_continue=True
            )
            return dict(state, effective_state="stopping_controller")

    def resume(self, directory, plan_hash):
        directory, _, plan = self._load(directory)
        checked_plan(plan, plan_hash)
        self.client.validate(plan)
        with exclusive_lock(directory / "control.lock"):
            state = self._reconcile(directory)
            if state.get("cleanup_pending"):
                raise LogicalError("Controller cleanup is pending; inspect cleanup_errors")
            if state["state"] not in {"interrupted", "cancelled", "failed", "blocked"}:
                raise LogicalError(
                    "Only an interrupted, cancelled, failed or blocked controller can resume"
                )
            if plan["operation"] not in {"controller", "experiment"}:
                raise LogicalError("Inspect the native outcome and review a new operation plan")
            for packet in (directory / "packets").glob("packet-*"):
                from pg_play.converter_runs import ConverterRunManager

                if ConverterRunManager(self.runner).status(packet)["state"] != "succeeded":
                    raise LogicalError(
                        "Inspect the converter packet outcome before a new experiment"
                    )
            observed = self.client.require(self.client.call(plan["config"], ("status",)))
            if observed["state"] in BAD_MIGRATION_STATES:
                raise LogicalError("Migration requires a planned recovery or cleanup operation")
            if plan["operation"] == "experiment" and observed["state"] not in {
                "NEW",
                "PREPARING",
                "PREPARED",
                "STARTING",
                "SYNCING",
                "CATCHING_UP",
                "REPLICATING",
            }:
                raise LogicalError(
                    "Complete the interrupted cutover or abort through a new native plan"
                )
            state.update(
                state="queued",
                worker=None,
                error=None,
                attempt=state["attempt"] + 1,
                migration_state=observed["state"],
                migration=observed,
            )
            state.pop("report", None)
            stop = directory / "stop.request.json"
            if stop.exists():
                stop.rename(directory / f"stop.attempt-{state['attempt'] - 1}.json")
            save(directory / "state.json", state)
            event(directory, "resume_requested", migration_state=observed["state"])
            return self._spawn(directory, state)

    def invocation(self, directory, plan, **kwargs):
        return dict(
            request_id=f"logical-{directory.name}-{kwargs.pop('step', 'run')}",
            timeout_seconds=plan["timeout_seconds"],
            cancel_path=directory / "stop.request.json",
            active_process_path=directory / "active-process.json",
            **kwargs,
        )

    def call(self, directory, plan, args, *, step="run"):
        checked_plan(plan)
        return self.client.call(plan["config"], args, **self.invocation(directory, plan, step=step))

    def refresh_migration(self, directory, plan):
        """Inspect even after cancellation; do not present stale state as current."""
        state = read_state(directory / "state.json")
        try:
            checked_plan(plan)
            invocation = self.invocation(directory, plan, step="status")
            invocation.update(cancel_path=None, timeout_seconds=min(30, plan["timeout_seconds"]))
            observed = self.client.require(
                self.client.call(plan["config"], ("status",), **invocation)
            )
            state.update(migration_state=observed["state"], migration=observed)
            state.pop("migration_state_error", None)
        except Exception as exc:
            state.update(
                last_known_migration_state=state.get("migration_state"),
                migration_state=None,
                migration_state_error=type(exc).__name__,
            )
        save(directory / "state.json", state)
        return state

    def tick(self, directory, plan):
        envelope = self.call(directory, plan, ("run", "--once"))
        result = self.client.require(envelope)
        state = read_state(directory / "state.json")
        state["migration_state"] = result.get("state")
        state["migration"] = result
        statistics = state.setdefault("statistics", {"samples": 0})
        statistics["samples"] += 1
        for slot in result.get("slot", []):
            for metric in ("lag_bytes", "retained_bytes"):
                value = slot.get(metric)
                if isinstance(value, (int, float)):
                    key = "max_" + metric
                    statistics[key] = max(statistics.get(key, 0), value)
        lag = (result.get("health") or {}).get("lag_seconds")
        if isinstance(lag, (int, float)):
            statistics["max_lag_seconds"] = max(statistics.get("max_lag_seconds", 0), lag)
        save(directory / "state.json", state)
        event(directory, "migration_sample", migration=result)
        rows = self.client.require(
            self.call(
                directory,
                plan,
                ("events", "--after", state["last_component_event"], "--limit", 1000),
                step="events",
            )
        )
        for row in rows:
            event(directory, "component_event", component_event=row)
            state["last_component_event"] = row["sequence"]
        save(directory / "state.json", state)
        if result.get("state") in BAD_MIGRATION_STATES:
            raise LogicalError("Migration stopped in " + result["state"])
        return result

    def observe(self, directory, plan, *, on_tick=None):
        state = read_state(directory / "state.json")
        elapsed = state["elapsed_seconds"]
        started = time.monotonic()
        result = state.get("migration", {})
        while elapsed < plan["duration_seconds"]:
            result = self.tick(directory, plan)
            if on_tick is not None:
                on_tick(result)
            state = read_state(directory / "state.json")
            elapsed = state["elapsed_seconds"] + time.monotonic() - started
            started = time.monotonic()
            state["elapsed_seconds"] = elapsed
            save(directory / "state.json", state)
            if result.get("state") in {"DETACHED", "ABORTED"}:
                break
            deadline = time.monotonic() + min(
                plan["poll_interval_seconds"], max(0, plan["duration_seconds"] - elapsed)
            )
            while time.monotonic() < deadline:
                if (directory / "stop.request.json").exists():
                    raise ComponentCancelledError(
                        "Controller stopped; native replication may continue"
                    )
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return result

    def execute(self, directory):
        directory, state, plan = self._load(directory)
        with exclusive_lock(directory / "execution.lock"):
            state = read_state(directory / "state.json")
            if state["state"] != "queued":
                raise LogicalError("Worker requires queued state")
            state.update(state="running")
            save(directory / "state.json", state)
            event(directory, "execution_started")
            result = None
            outcome, error = "failed", None
            try:
                self.client.validate(plan)
                if (directory / "stop.request.json").exists():
                    raise ComponentCancelledError("Stopped before execution")
                if plan["operation"] in {"controller", "experiment"}:
                    with controller_lock(plan["configuration"]["state_dir"]):
                        if plan["operation"] == "experiment":
                            from pg_play.logical_experiment import execute_scenario

                            result = execute_scenario(self, directory, plan)
                            outcome = "succeeded" if result["passed"] else "partial"
                        else:
                            result = self.observe(directory, plan)
                            outcome = (
                                "succeeded"
                                if result.get("state") in {"REPLICATING", "DETACHED", "ABORTED"}
                                else "partial"
                            )
                else:
                    envelope = self.client.operate(plan, **self.invocation(directory, plan))
                    result = envelope.get("result")
                    outcome = envelope["status"]
                    error = envelope.get("error")
                    if isinstance(result, dict) and result.get("state") in BAD_MIGRATION_STATES:
                        outcome = "blocked" if result["state"] == "BLOCKED" else "partial"
                    if (
                        outcome == "succeeded"
                        and isinstance(result, dict)
                        and "state" not in result
                    ):
                        self.refresh_migration(directory, plan)
            except ComponentCancelledError:
                self.refresh_migration(directory, plan)
                outcome = "cancelled"
                error = dict(code="controller_stopped", message="Native replication may continue")
            except Exception as exc:
                current = self.refresh_migration(directory, plan)
                outcome = "blocked" if current.get("migration_state") == "BLOCKED" else "failed"
                # Component errors are already redacted. Never persist arbitrary
                # driver exceptions from diagnostic or independent-oracle code.
                message = str(exc) if isinstance(exc, LogicalError) else type(exc).__name__
                error = dict(code="logical_operation_failed", message=message)
            state = read_state(directory / "state.json")
            state.update(state=outcome, worker=None, result=result, error=error)
            if isinstance(result, dict) and "state" in result:
                state["migration_state"] = result["state"]
            report = dict(
                schema_version="pg_play/logical-report-v1",
                plan_hash=plan["plan_hash"],
                operation=plan["operation"],
                component_version=plan["component_version"],
                state=outcome,
                migration_state=state["migration_state"],
                migration_state_error=state.get("migration_state_error"),
                elapsed_seconds=state["elapsed_seconds"],
                statistics=state.get("statistics", {}),
                result=result,
                error=error,
                replication_may_continue=state["migration_state"] not in {"DETACHED", "ABORTED"},
            )
            # Publish the terminal state only after its report is durable.
            durable_json(directory / "report.json", report)
            state["report"] = file_reference(directory / "report.json")
            save(directory / "state.json", state)
            event(
                directory,
                "execution_finished",
                state=outcome,
                migration_state=state["migration_state"],
            )
            return state

    def report(self, directory):
        directory, state, _ = self._load(directory)
        reference = state.get("report")
        if reference is None:
            raise LogicalError("Run has no final report")
        if file_reference(directory / "report.json") != reference:
            raise LogicalError("Report hash does not match")
        return read_state(directory / "report.json")
