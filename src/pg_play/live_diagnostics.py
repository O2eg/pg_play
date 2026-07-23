"""Durable, read-only pg_diag captures for incidents on existing servers."""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_play.configuration_review import ConfigurationReviewError, normalize_review_target
from pg_play.contract import canonical_hash
from pg_play.runner import (
    ComponentCancelledError,
    ComponentInvocation,
    ComponentRunner,
    process_start_ticks,
    recorded_process_is_alive,
    terminate_recorded_process,
)
from pg_play.state import (
    TERMINAL_STATES,
    append_event,
    exclusive_lock,
    read_events,
    read_state,
    utc_now,
    write_json,
    write_state,
    write_text,
)

PLAN_SCHEMA_VERSION = "pg_play/live-diagnostics-plan-v1"
STATE_SCHEMA_VERSION = "pg_play/live-diagnostics-state-v1"
EVENTS_SCHEMA_VERSION = "pg_play/live-diagnostics-events-v1"
CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTIVE_STATES = frozenset({"queued", "running"})

_LOCK_ITEMS = (
    "overview.server_version",
    "overview.stat_reset_times",
    "activity_locks.connection_pressure",
    "activity_locks.session_states",
    "activity_locks.wait_events",
    "activity_locks.wait_event_sample_profile",
    "activity_locks.pg_wait_sampling_capabilities",
    "activity_locks.pg_wait_sampling_profile",
    "activity_locks.long_transactions",
    "activity_locks.idle_in_transaction",
    "activity_locks.lock_waits",
    "activity_locks.lock_modes",
    "snapshot_delta_workload.database_session_outcomes_delta",
    "snapshot_charts_db.database_transaction_rate",
    "snapshot_charts_db.activity_sessions_by_state",
    "snapshot_charts_db.database_deadlocks",
)

_IO_ITEMS = (
    "overview.server_version",
    "overview.pg_settings",
    "overview.database_stats",
    "os.disk_usage",
    "os.mounts",
    "sql_workload.pg_stat_statements_capabilities",
    "sql_workload.top_sql_by_total_time",
    "sql_workload.top_sql_by_shared_io",
    "sql_workload.top_sql_by_temp_io",
    "sql_workload.top_sql_by_wal",
    "snapshot_delta_workload.database_workload_delta",
    "snapshot_delta_workload.sql_io_delta",
    "snapshot_delta_workload.sql_temp_io_delta",
    "snapshot_delta_workload.sql_wal_delta",
    "snapshot_delta_workload.postgresql_io_delta",
    "snapshot_delta_workload.checkpointer_delta",
    "snapshot_delta_workload.background_writer_delta",
    "snapshot_delta_workload.wal_activity_delta",
    "wal_io_checkpoints.wal_statistics",
    "wal_io_checkpoints.wal_archiver",
    "wal_io_checkpoints.bgwriter",
    "wal_io_checkpoints.checkpointer",
    "wal_io_checkpoints.pg_stat_io",
    "snapshot_charts_os.os_memory_pressure",
    "snapshot_charts_os.os_disk_read_throughput",
    "snapshot_charts_os.os_disk_write_throughput",
    "snapshot_charts_os.os_disk_iops",
    "snapshot_charts_os.os_disk_utilization",
    "snapshot_charts_os.os_disk_latency",
    "snapshot_charts_db.wal_growth_rate",
    "snapshot_charts_db.io_read_write_rate",
    "snapshot_charts_db.database_block_access_rate",
    "snapshot_charts_db.database_temp_bytes_rate",
    "snapshot_charts_db.database_io_time_rate",
)

_CPU_ITEMS = (
    "overview.server_version",
    "overview.pg_settings",
    "overview.database_stats",
    "os.cpu_info",
    "os.memory_info",
    "os.total_ram",
    "activity_locks.connection_pressure",
    "activity_locks.session_states",
    "activity_locks.wait_events",
    "activity_locks.wait_event_sample_profile",
    "sql_workload.pg_stat_statements_capabilities",
    "sql_workload.pg_stat_kcache_capabilities",
    "sql_workload.top_sql_by_total_time",
    "sql_workload.top_sql_by_mean_time",
    "sql_workload.top_sql_by_calls",
    "snapshot_delta_workload.database_workload_delta",
    "snapshot_delta_workload.sql_time_delta",
    "snapshot_delta_workload.sql_kernel_cpu_delta",
    "snapshot_delta_workload.sql_cpu_efficiency_delta",
    "snapshot_delta_workload.sql_context_switches_delta",
    "snapshot_delta_workload.sql_page_faults_delta",
    "snapshot_charts_os.os_cpu_utilization",
    "snapshot_charts_os.os_cpu_load",
    "snapshot_charts_os.os_memory_usage",
    "snapshot_charts_os.os_memory_pressure",
    "snapshot_charts_db.database_transaction_rate",
    "snapshot_charts_db.activity_sessions_by_state",
    "snapshot_charts_db.database_kernel_cpu_rate",
    "snapshot_charts_db.database_page_fault_rate",
    "snapshot_charts_db.database_backends",
)


def _ordered_union(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


LIVE_DIAGNOSTIC_PROFILES: dict[str, tuple[str, ...]] = {
    "locks": _LOCK_ITEMS,
    "io": _IO_ITEMS,
    "cpu": _CPU_ITEMS,
    "performance": _ordered_union(_LOCK_ITEMS, _IO_ITEMS, _CPU_ITEMS),
}


class LiveDiagnosticsError(RuntimeError):
    """A live diagnostic capture request cannot safely proceed."""


@dataclass(frozen=True)
class LiveDiagnosticsContext:
    capture_id: str
    directory: Path
    plan_path: Path
    state_path: Path
    events_path: Path
    cancel_path: Path
    active_process_path: Path
    worker_log_path: Path


def _number(
    value: Any,
    *,
    default: float,
    field: str,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a number from {minimum:g} to {maximum:g}")
        return default
    if not math.isfinite(result) or not minimum <= result <= maximum:
        errors.append(f"{field} must be a number from {minimum:g} to {maximum:g}")
        return default
    return result


def plan_live_diagnostics(
    target: dict[str, Any],
    intent: str = "performance",
    duration_seconds: float = 60,
    interval_seconds: float = 5,
) -> dict[str, Any]:
    """Build a non-mutating, content-hashed capture plan and report missing inputs."""
    try:
        normalized_target, missing, errors = normalize_review_target(target)
    except (AttributeError, ConfigurationReviewError) as exc:
        normalized_target, missing, errors = {}, [], [str(exc)]
    normalized_intent = str(intent).strip().lower()
    if normalized_intent not in LIVE_DIAGNOSTIC_PROFILES:
        errors.append("intent must be one of: " + ", ".join(sorted(LIVE_DIAGNOSTIC_PROFILES)))
    duration = _number(
        duration_seconds,
        default=60,
        field="duration_seconds",
        minimum=30,
        maximum=900,
        errors=errors,
    )
    interval = _number(
        interval_seconds,
        default=5,
        field="interval_seconds",
        minimum=5,
        maximum=60,
        errors=errors,
    )
    if interval > duration:
        errors.append("interval_seconds must not exceed duration_seconds")
    regular_points = math.floor(duration / interval)
    snapshot_count = regular_points + 1
    if not math.isclose(regular_points * interval, duration, rel_tol=0.0, abs_tol=1e-9):
        snapshot_count += 1
    if snapshot_count > 121:
        errors.append("capture would exceed the limit of 121 snapshots; increase interval_seconds")
    item_ids = LIVE_DIAGNOSTIC_PROFILES.get(normalized_intent, ())
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "ready": not missing and not errors,
        "missing_inputs": sorted(set(missing)),
        "errors": errors,
        "target": normalized_target,
        "intent": normalized_intent,
        "capture": {
            "component": "pg_diag",
            "mode": "snapshots",
            "collection_mode": "remote",
            "duration_seconds": duration,
            "interval_seconds": interval,
            "item_ids": list(item_ids),
            "output_formats": ["json", "html"],
        },
        "safety": {
            "mutation": False,
            "arbitrary_sql": False,
            "arbitrary_shell": False,
            "max_duration_seconds": 900,
            "max_snapshot_count": 121,
        },
    }
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def validate_live_diagnostics_plan(
    plan: Any,
    *,
    verify_access_files: bool,
) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise LiveDiagnosticsError(f"plan must use {PLAN_SCHEMA_VERSION}")
    plan_hash = plan.get("plan_hash")
    if not isinstance(plan_hash, str):
        raise LiveDiagnosticsError("live diagnostics plan has no plan_hash")
    unhashed = dict(plan)
    unhashed.pop("plan_hash", None)
    if canonical_hash(unhashed) != plan_hash:
        raise LiveDiagnosticsError("live diagnostics plan hash does not match its content")
    if not plan.get("ready"):
        raise LiveDiagnosticsError("live diagnostics plan is not ready")
    target = plan.get("target")
    capture = plan.get("capture")
    if not isinstance(target, dict) or not isinstance(capture, dict):
        raise LiveDiagnosticsError("live diagnostics plan has invalid target or capture fields")
    if verify_access_files:
        expected = plan_live_diagnostics(
            target,
            str(plan.get("intent", "")),
            capture.get("duration_seconds"),
            capture.get("interval_seconds"),
        )
        if expected != plan:
            raise LiveDiagnosticsError(
                "live diagnostics plan is stale or access-file validation changed"
            )
    else:
        intent = str(plan.get("intent", ""))
        if intent not in LIVE_DIAGNOSTIC_PROFILES:
            raise LiveDiagnosticsError("live diagnostics plan has an unsupported intent")
        if capture.get("item_ids") != list(LIVE_DIAGNOSTIC_PROFILES[intent]):
            raise LiveDiagnosticsError("live diagnostics plan item allowlist was modified")
    return plan


def _context(directory: str | Path, capture_id: str | None = None) -> LiveDiagnosticsContext:
    path = Path(directory).expanduser().resolve()
    identifier = capture_id or path.name
    if not CAPTURE_ID_RE.fullmatch(identifier):
        raise LiveDiagnosticsError("capture_id contains unsupported characters")
    return LiveDiagnosticsContext(
        capture_id=identifier,
        directory=path,
        plan_path=path / "plan.json",
        state_path=path / "state.json",
        events_path=path / "events.jsonl",
        cancel_path=path / "cancel.request.json",
        active_process_path=path / "active-process.json",
        worker_log_path=path / "worker.log",
    )


def _event(
    context: LiveDiagnosticsContext,
    event_type: str,
    *,
    state: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return append_event(
        context.events_path,
        run_id=context.capture_id,
        event_type=event_type,
        state=state,
        step="diagnostics",
        data=data,
    )


def _unverified_outputs(context: LiveDiagnosticsContext) -> list[str]:
    return [
        str(path)
        for path in (
            context.directory / f"{context.capture_id}.json",
            context.directory / f"{context.capture_id}.html",
        )
        if path.is_file()
    ]


class LiveDiagnosticsManager:
    """Create, execute, inspect, and cancel one immutable live capture."""

    def __init__(self, runner: ComponentRunner | None = None) -> None:
        self.runner = runner or ComponentRunner()

    def start(
        self,
        plan: dict[str, Any],
        plan_hash: str,
        output_directory: str | Path,
        capture_id: str,
    ) -> dict[str, Any]:
        validated = validate_live_diagnostics_plan(plan, verify_access_files=True)
        if validated["plan_hash"] != plan_hash:
            raise LiveDiagnosticsError(
                f"reviewed plan hash is {validated['plan_hash']}, request supplied {plan_hash}"
            )
        root = Path(output_directory).expanduser().resolve()
        context = _context(root / capture_id, capture_id)
        if context.directory.exists():
            raise LiveDiagnosticsError(f"capture_id {capture_id} already exists")
        context.directory.mkdir(parents=True, exist_ok=False)
        os.chmod(context.directory, 0o700)
        created_at = utc_now()
        state: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "capture_id": capture_id,
            "capture_directory": str(context.directory),
            "plan_hash": plan_hash,
            "state": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "worker": None,
            "artifacts": [
                {
                    "kind": "LiveDiagnosticsPlan",
                    "path": str(context.plan_path),
                    "hash": plan_hash,
                },
                {"kind": "LiveDiagnosticsEvents", "path": str(context.events_path)},
                {"kind": "WorkerLog", "path": str(context.worker_log_path)},
            ],
            "result": None,
            "cancellation": None,
            "error": None,
        }
        write_json(context.plan_path, validated)
        write_state(context.state_path, state)
        _event(
            context,
            "capture_created",
            state="queued",
            data={"plan_hash": plan_hash, "intent": validated["intent"]},
        )
        with exclusive_lock(context.directory / "control.lock"):
            return self._spawn_worker(context, state)

    def status(self, capture_directory: str | Path) -> dict[str, Any]:
        context = self._load_context(capture_directory)
        with exclusive_lock(context.directory / "control.lock"):
            return self._reconcile_worker(context)

    def events(
        self,
        capture_directory: str | Path,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        context = self._load_context(capture_directory)
        return {
            "schema_version": EVENTS_SCHEMA_VERSION,
            "capture_id": context.capture_id,
            **read_events(
                context.events_path,
                after_sequence=after_sequence,
                limit=limit,
            ),
        }

    def cancel(
        self,
        capture_directory: str | Path,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        context = self._load_context(capture_directory)
        with exclusive_lock(context.directory / "control.lock"):
            state = self._reconcile_worker(context)
            current = state.get("state")
            if current == "cancelled":
                return state
            if current in TERMINAL_STATES or current == "interrupted":
                raise LiveDiagnosticsError(
                    f"capture {context.capture_id} is already terminal: {current}"
                )
            clean_reason = (reason or "requested by operator").strip()
            if not clean_reason or len(clean_reason) > 500:
                raise LiveDiagnosticsError("cancellation reason must contain 1 to 500 characters")
            request = {
                "schema_version": "pg_play/cancel-request-v1",
                "run_id": context.capture_id,
                "requested_at": utc_now(),
                "reason": clean_reason,
            }
            write_json(context.cancel_path, request)
            state["cancellation"] = request
            write_state(context.state_path, state)
            _event(
                context,
                "cancellation_requested",
                state=current,
                data={"reason": clean_reason},
            )
            result = dict(state)
            result["effective_state"] = "cancelling"
            return result

    def execute(self, capture_directory: str | Path) -> dict[str, Any]:
        """Execute a prepared capture; called by the detached worker and tests."""
        context = self._load_context(capture_directory)
        plan = validate_live_diagnostics_plan(
            read_state(context.plan_path), verify_access_files=False
        )
        state = read_state(context.state_path)
        if state.get("state") != "queued":
            raise LiveDiagnosticsError(f"capture cannot start from state {state.get('state')!r}")
        state["state"] = "running"
        state["worker"] = {
            "pid": os.getpid(),
            "process_start_ticks": process_start_ticks(os.getpid()),
            "started_at": utc_now(),
            "mode": "background",
        }
        write_state(context.state_path, state)
        _event(context, "capture_started", state="running")
        report_path = context.directory / f"{context.capture_id}.json"
        html_path = context.directory / f"{context.capture_id}.html"
        try:
            envelope = self.runner.run(
                self._collection_invocation(plan, context, report_path, html_path)
            )
            if envelope["status"] not in {"succeeded", "partial"}:
                message = (envelope.get("error") or {}).get("message") or envelope["status"]
                raise LiveDiagnosticsError(f"pg_diag capture failed: {message}")
            if not report_path.is_file():
                raise LiveDiagnosticsError("pg_diag did not create the requested JSON report")
            validation = self.runner.run(
                ComponentInvocation(
                    component="pg_diag",
                    arguments=("validate-artifact", str(report_path)),
                    request_id=f"live-diagnostics-{context.capture_id}-validate",
                    timeout_seconds=60,
                )
            )
            if validation["status"] != "succeeded":
                message = (validation.get("error") or {}).get("message") or validation["status"]
                raise LiveDiagnosticsError(f"pg_diag artifact validation failed: {message}")
            state = read_state(context.state_path)
            state["state"] = "partial" if envelope["status"] == "partial" else "succeeded"
            state["worker"] = None
            state["artifacts"].extend(envelope.get("artifacts") or [])
            state["artifacts"].extend(validation.get("artifacts") or [])
            state["result"] = {
                "report_path": str(report_path),
                "html_path": str(html_path) if html_path.is_file() else None,
                "summary": validation["result"].get("summary"),
                "file_hash": validation["result"].get("file_hash"),
            }
            state["error"] = None
            write_state(context.state_path, state)
            _event(
                context,
                "capture_completed",
                state=state["state"],
                data={"report_path": str(report_path)},
            )
            return state
        except ComponentCancelledError as exc:
            state = read_state(context.state_path)
            state["state"] = "cancelled"
            state["worker"] = None
            state["cancellation"] = read_state(context.cancel_path)
            state["result"] = {"unverified_partial_outputs": _unverified_outputs(context)}
            state["error"] = {"code": "cancelled", "message": str(exc)}
            write_state(context.state_path, state)
            _event(context, "capture_cancelled", state="cancelled")
            return state
        except BaseException as exc:
            state = read_state(context.state_path)
            state["state"] = "failed"
            state["worker"] = None
            state["result"] = {"unverified_partial_outputs": _unverified_outputs(context)}
            state["error"] = {
                "code": "capture_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            write_state(context.state_path, state)
            _event(
                context,
                "capture_failed",
                state="failed",
                data={"message": str(exc)},
            )
            return state

    @staticmethod
    def _collection_invocation(
        plan: dict[str, Any],
        context: LiveDiagnosticsContext,
        report_path: Path,
        html_path: Path,
    ) -> ComponentInvocation:
        database = plan["target"]["database"]
        ssh = plan["target"]["ssh"]
        capture = plan["capture"]
        arguments = [
            "snapshots",
            "--host",
            database["host"],
            "--port",
            str(database["port"]),
            "--database",
            database["database"],
            "--user",
            database["user"],
            "--collection-mode",
            "remote",
            "--ssh-host",
            ssh["host"],
            "--ssh-port",
            str(ssh["port"]),
            "--ssh-user",
            ssh["user"],
            "--ssh-key",
            ssh["key_path"],
            "--ssh-known-hosts",
            ssh["known_hosts_path"],
            "--duration-seconds",
            str(capture["duration_seconds"]),
            "--interval-seconds",
            str(capture["interval_seconds"]),
            "--item-id=[" + ",".join(capture["item_ids"]) + "]",
            "--out",
            str(context.directory),
            "--json-out",
            str(report_path),
            "--html-out",
            str(html_path),
            "--output-format=[json,html]",
        ]
        if database.get("passfile"):
            arguments.extend(("--passfile", database["passfile"]))
        if ssh.get("connect_timeout") is not None:
            arguments.extend(("--ssh-connect-timeout", str(ssh["connect_timeout"])))
        environment = None
        if ssh.get("key_passphrase_env"):
            name = ssh["key_passphrase_env"]
            arguments.extend(("--ssh-key-passphrase-env", name))
            environment = {name: os.environ[name]}
        return ComponentInvocation(
            component="pg_diag",
            arguments=tuple(arguments),
            request_id=f"live-diagnostics-{context.capture_id}-collect",
            environment=environment,
            timeout_seconds=float(capture["duration_seconds"]) + 300,
            cancel_path=context.cancel_path,
            active_process_path=context.active_process_path,
        )

    def _load_context(self, capture_directory: str | Path) -> LiveDiagnosticsContext:
        context = _context(capture_directory)
        state = read_state(context.state_path)
        if state.get("state") == "not_found":
            raise LiveDiagnosticsError(
                f"live diagnostic capture does not exist: {context.directory}"
            )
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise LiveDiagnosticsError("live diagnostic state uses an unsupported schema")
        if state.get("capture_id") != context.capture_id:
            raise LiveDiagnosticsError("live diagnostic directory and capture_id do not match")
        if state.get("capture_directory") != str(context.directory):
            raise LiveDiagnosticsError("live diagnostic state belongs to another directory")
        plan = validate_live_diagnostics_plan(
            read_state(context.plan_path), verify_access_files=False
        )
        if state.get("plan_hash") != plan["plan_hash"]:
            raise LiveDiagnosticsError("live diagnostic state and plan hashes do not match")
        return context

    def _spawn_worker(
        self,
        context: LiveDiagnosticsContext,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        gate = context.directory / "worker.starting"
        write_text(gate, "wait\n")
        command = [
            sys.executable,
            "-m",
            "pg_play.live_diagnostics_worker",
            "--capture-directory",
            str(context.directory),
            "--plan-hash",
            str(state["plan_hash"]),
            "--start-gate",
            str(gate),
        ]
        descriptor: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            descriptor = os.open(
                context.worker_log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            worker_log = os.fdopen(descriptor, "ab")
            descriptor = None
            with worker_log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    start_new_session=True,
                    close_fds=True,
                )
            state = read_state(context.state_path)
            state["worker"] = {
                "pid": process.pid,
                "process_start_ticks": process_start_ticks(process.pid),
                "started_at": utc_now(),
                "mode": "background",
            }
            write_state(context.state_path, state)
            _event(
                context,
                "worker_started",
                state="queued",
                data={"pid": process.pid},
            )
            return state
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            state = read_state(context.state_path)
            state["state"] = "failed"
            state["worker"] = None
            state["error"] = {"code": "worker_start_failed", "message": str(exc)}
            write_state(context.state_path, state)
            _event(
                context,
                "worker_start_failed",
                state="failed",
                data={"message": str(exc)},
            )
            raise LiveDiagnosticsError(f"cannot start live diagnostics worker: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            gate.unlink(missing_ok=True)

    def _reconcile_worker(self, context: LiveDiagnosticsContext) -> dict[str, Any]:
        state = read_state(context.state_path)
        if state.get("state") not in ACTIVE_STATES:
            return state
        worker = state.get("worker")
        if isinstance(worker, dict) and recorded_process_is_alive(worker):
            if context.cancel_path.exists():
                result = dict(state)
                result["effective_state"] = "cancelling"
                result["cancellation"] = read_state(context.cancel_path)
                return result
            return state
        orphan_terminated = False
        cleanup_error = None
        try:
            orphan_terminated = terminate_recorded_process(context.active_process_path)
        except RuntimeError as exc:
            cleanup_error = str(exc)
        state["state"] = "interrupted"
        state["worker"] = None
        state["result"] = {"unverified_partial_outputs": _unverified_outputs(context)}
        state["error"] = {
            "code": "worker_lost",
            "message": (
                "live diagnostics worker is no longer running; start a new capture"
                + (f"; orphan cleanup failed: {cleanup_error}" if cleanup_error else "")
            ),
        }
        write_state(context.state_path, state)
        _event(
            context,
            "worker_lost",
            state="interrupted",
            data={
                "orphan_component_terminated": orphan_terminated,
                "cleanup_error": cleanup_error,
            },
        )
        return state
