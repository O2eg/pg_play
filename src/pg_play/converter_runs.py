"""Durable reviewed pg_converter packet runs for the pg_play MCP facade."""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

PLAN_SCHEMA_VERSION = "pg_play/converter-run-plan-v1"
STATE_SCHEMA_VERSION = "pg_play/converter-run-state-v1"
EVENTS_SCHEMA_VERSION = "pg_play/converter-run-events-v1"
COMPONENT_PLAN_SCHEMA_VERSION = "pg_converter/plan-v1"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTIVE_STATES = frozenset({"queued", "running"})


class ConverterRunError(RuntimeError):
    """A converter packet run cannot be planned or executed safely."""


@dataclass(frozen=True)
class ConverterRunContext:
    run_id: str
    directory: Path
    plan_path: Path
    state_path: Path
    events_path: Path
    cancel_path: Path
    active_process_path: Path
    worker_log_path: Path
    result_path: Path


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _timeout(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConverterRunError("timeout_seconds must be a number from 1 to 86400") from exc
    if not math.isfinite(result) or not 1 <= result <= 86400:
        raise ConverterRunError("timeout_seconds must be a number from 1 to 86400")
    return result


def _plan_arguments(
    *,
    packet_name: str,
    database_selector: str,
    config_file: str,
    placeholders_file: str | None,
    config_overrides_file: str | None,
) -> tuple[str, ...]:
    arguments = [
        f"--packet-name={packet_name}",
        f"--db-name={database_selector}",
        "--seq",
        f"--config-file={config_file}",
    ]
    if placeholders_file:
        arguments.append(f"--placeholders-file={placeholders_file}")
    if config_overrides_file:
        arguments.append(f"--config-overrides-file={config_overrides_file}")
    arguments.append("--plan")
    return tuple(arguments)


def plan_converter_run(
    runner: ComponentRunner,
    *,
    project_directory: str | Path,
    config_file: str,
    packet_name: str,
    database_selector: str = "ALL",
    placeholders_file: str | None = None,
    config_overrides_file: str | None = None,
    timeout_seconds: float = 3600,
) -> dict[str, Any]:
    """Ask pg_converter for an exact non-connecting plan and wrap it for pg_play."""
    project = Path(project_directory).expanduser().resolve()
    if not project.is_dir():
        raise ConverterRunError(f"project_directory does not exist: {project}")
    clean_packet_name = str(packet_name).strip()
    clean_selector = str(database_selector).strip()
    if not clean_packet_name:
        raise ConverterRunError("packet_name must be non-empty")
    if not clean_selector:
        raise ConverterRunError("database_selector must be non-empty")
    duration = _timeout(timeout_seconds)
    component_envelope = runner.run(
        ComponentInvocation(
            component="pg_converter",
            arguments=_plan_arguments(
                packet_name=clean_packet_name,
                database_selector=clean_selector,
                config_file=config_file,
                placeholders_file=placeholders_file,
                config_overrides_file=config_overrides_file,
            ),
            request_id=f"converter-plan-{clean_packet_name}",
            cwd=project,
            timeout_seconds=60,
        )
    )
    if component_envelope["status"] != "planned":
        message = (component_envelope.get("error") or {}).get("message")
        raise ConverterRunError(
            f"pg_converter plan failed: {message or component_envelope['status']}"
        )
    component_plan = component_envelope.get("result")
    if (
        not isinstance(component_plan, dict)
        or component_plan.get("schema_version") != COMPONENT_PLAN_SCHEMA_VERSION
        or not isinstance(component_plan.get("plan_hash"), str)
    ):
        raise ConverterRunError("pg_converter returned an invalid packet plan")
    sources = component_plan.get("sources")
    if not isinstance(sources, dict):
        raise ConverterRunError("pg_converter packet plan has no source references")
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "component": "pg_converter",
        "component_version": component_envelope["component_version"],
        "component_plan": component_plan,
        "execution": {
            "project_directory": str(project),
            "config_file": sources.get("config_file"),
            "placeholders_file": sources.get("placeholders_file"),
            "config_overrides_file": sources.get("config_overrides_file"),
            "packet_name": clean_packet_name,
            "database_selector": clean_selector,
            "sequential": True,
            "timeout_seconds": duration,
        },
        "safety": {
            "mutates_target": True,
            "requires_reviewed_plan_hash": True,
            "force": False,
            "administrative_operations": False,
            "sequential_databases": True,
            "arbitrary_packet_sql": True,
            "arbitrary_packet_python": bool(
                (component_plan.get("safety") or {}).get("arbitrary_python")
            ),
            "credentials_in_plan": False,
            "placeholder_values_in_plan": False,
        },
    }
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def validate_converter_run_plan(
    plan: Any,
    *,
    runner: ComponentRunner | None,
    verify_sources: bool,
) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ConverterRunError(f"plan must use {PLAN_SCHEMA_VERSION}")
    plan_hash = plan.get("plan_hash")
    if not isinstance(plan_hash, str):
        raise ConverterRunError("converter run plan has no plan_hash")
    unhashed = dict(plan)
    unhashed.pop("plan_hash", None)
    if canonical_hash(unhashed) != plan_hash:
        raise ConverterRunError("converter run plan hash does not match its content")
    execution = plan.get("execution")
    component_plan = plan.get("component_plan")
    safety = plan.get("safety")
    if not all(isinstance(value, dict) for value in (execution, component_plan, safety)):
        raise ConverterRunError("converter run plan has invalid execution or safety fields")
    if component_plan.get("schema_version") != COMPONENT_PLAN_SCHEMA_VERSION:
        raise ConverterRunError("converter run plan embeds an unsupported component plan")
    if not execution.get("sequential") or not safety.get("sequential_databases"):
        raise ConverterRunError("converter run plan must keep database execution sequential")
    if safety.get("force") or safety.get("administrative_operations"):
        raise ConverterRunError("converter run plan enables an unsupported safety bypass")
    _timeout(execution.get("timeout_seconds"))
    if verify_sources:
        if runner is None:
            raise ConverterRunError("source verification requires a component runner")
        expected = plan_converter_run(
            runner,
            project_directory=str(execution.get("project_directory", "")),
            config_file=str(execution.get("config_file", "")),
            packet_name=str(execution.get("packet_name", "")),
            database_selector=str(execution.get("database_selector", "")),
            placeholders_file=execution.get("placeholders_file"),
            config_overrides_file=execution.get("config_overrides_file"),
            timeout_seconds=execution.get("timeout_seconds", 3600),
        )
        if expected != plan:
            raise ConverterRunError("converter run plan is stale; packet or inputs changed")
    return plan


def _context(directory: str | Path, run_id: str | None = None) -> ConverterRunContext:
    path = Path(directory).expanduser().resolve()
    identifier = run_id or path.name
    if not RUN_ID_RE.fullmatch(identifier):
        raise ConverterRunError("run_id contains unsupported characters")
    return ConverterRunContext(
        run_id=identifier,
        directory=path,
        plan_path=path / "plan.json",
        state_path=path / "state.json",
        events_path=path / "events.jsonl",
        cancel_path=path / "cancel.request.json",
        active_process_path=path / "active-process.json",
        worker_log_path=path / "worker.log",
        result_path=path / "result.json",
    )


def _event(
    context: ConverterRunContext,
    event_type: str,
    *,
    state: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return append_event(
        context.events_path,
        run_id=context.run_id,
        event_type=event_type,
        state=state,
        step="converter",
        data=data,
    )


class ConverterRunManager:
    """Create, execute, inspect, and cancel an immutable reviewed packet run."""

    def __init__(self, runner: ComponentRunner | None = None) -> None:
        self.runner = runner or ComponentRunner()

    def start(
        self,
        plan: dict[str, Any],
        plan_hash: str,
        output_directory: str | Path,
        run_id: str,
    ) -> dict[str, Any]:
        validated = validate_converter_run_plan(
            plan,
            runner=self.runner,
            verify_sources=True,
        )
        if validated["plan_hash"] != plan_hash:
            raise ConverterRunError(
                f"reviewed plan hash is {validated['plan_hash']}, request supplied {plan_hash}"
            )
        root = Path(output_directory).expanduser().resolve()
        context = _context(root / run_id, run_id)
        if context.directory.exists():
            raise ConverterRunError(f"run_id {run_id} already exists")
        context.directory.mkdir(parents=True, exist_ok=False)
        os.chmod(context.directory, 0o700)
        created_at = utc_now()
        state: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "run_directory": str(context.directory),
            "plan_hash": plan_hash,
            "component_plan_hash": validated["component_plan"]["plan_hash"],
            "state": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "worker": None,
            "artifacts": [
                {
                    "kind": "ConverterRunPlan",
                    "path": str(context.plan_path),
                    "hash": plan_hash,
                },
                {"kind": "ConverterRunEvents", "path": str(context.events_path)},
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
            "converter_run_created",
            state="queued",
            data={
                "plan_hash": plan_hash,
                "packet_name": validated["execution"]["packet_name"],
                "database_aliases": validated["component_plan"]["target"]["database_aliases"],
            },
        )
        with exclusive_lock(context.directory / "control.lock"):
            return self._spawn_worker(context, state)

    def status(self, run_directory: str | Path) -> dict[str, Any]:
        context = self._load_context(run_directory)
        with exclusive_lock(context.directory / "control.lock"):
            return self._reconcile_worker(context)

    def events(
        self,
        run_directory: str | Path,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        context = self._load_context(run_directory)
        return {
            "schema_version": EVENTS_SCHEMA_VERSION,
            "run_id": context.run_id,
            **read_events(
                context.events_path,
                after_sequence=after_sequence,
                limit=limit,
            ),
        }

    def cancel(
        self,
        run_directory: str | Path,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        context = self._load_context(run_directory)
        with exclusive_lock(context.directory / "control.lock"):
            state = self._reconcile_worker(context)
            current = state.get("state")
            if current == "cancelled":
                return state
            if current in TERMINAL_STATES or current == "interrupted":
                raise ConverterRunError(
                    f"converter run {context.run_id} is already terminal: {current}"
                )
            clean_reason = (reason or "requested by operator").strip()
            if not clean_reason or len(clean_reason) > 500:
                raise ConverterRunError("cancellation reason must contain 1 to 500 characters")
            request = {
                "schema_version": "pg_play/cancel-request-v1",
                "run_id": context.run_id,
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

    def execute(self, run_directory: str | Path) -> dict[str, Any]:
        context = self._load_context(run_directory)
        plan = validate_converter_run_plan(
            read_state(context.plan_path),
            runner=self.runner,
            verify_sources=True,
        )
        state = read_state(context.state_path)
        if state.get("state") != "queued":
            raise ConverterRunError(f"converter run cannot start from state {state.get('state')!r}")
        state["state"] = "running"
        state["worker"] = {
            "pid": os.getpid(),
            "process_start_ticks": process_start_ticks(os.getpid()),
            "started_at": utc_now(),
            "mode": "background",
        }
        write_state(context.state_path, state)
        _event(context, "converter_run_started", state="running")
        try:
            component_envelope = self.runner.run(self._run_invocation(plan, context))
            component_status = component_envelope["status"]
            if component_status not in {
                "succeeded",
                "partial",
                "cancelled",
                "failed",
                "blocked",
            }:
                raise ConverterRunError(
                    f"pg_converter returned unsupported status: {component_status}"
                )
            durable_status = "failed" if component_status == "blocked" else component_status
            write_json(context.result_path, component_envelope)
            state = read_state(context.state_path)
            state["state"] = durable_status
            state["worker"] = None
            state["artifacts"].append(
                {
                    "kind": "ConverterRunResult",
                    "schema_version": "pg_converter/machine-result-v1",
                    "path": str(context.result_path),
                    "hash": _file_hash(context.result_path),
                }
            )
            state["artifacts"].extend(component_envelope.get("artifacts") or [])
            state["result"] = component_envelope.get("result")
            state["error"] = component_envelope.get("error")
            write_state(context.state_path, state)
            _event(
                context,
                (
                    "converter_run_blocked"
                    if component_status == "blocked"
                    else "converter_run_completed"
                ),
                state=durable_status,
                data={
                    "component_status": component_status,
                    "result_path": str(context.result_path),
                    "result_counts": (state.get("result") or {}).get("result_counts"),
                },
            )
            return state
        except ComponentCancelledError as exc:
            state = read_state(context.state_path)
            state["state"] = "cancelled"
            state["worker"] = None
            state["cancellation"] = read_state(context.cancel_path)
            state["error"] = {"code": "cancelled", "message": str(exc)}
            write_state(context.state_path, state)
            _event(context, "converter_run_cancelled", state="cancelled")
            return state
        except BaseException as exc:
            state = read_state(context.state_path)
            state["state"] = "failed"
            state["worker"] = None
            state["error"] = {
                "code": "converter_run_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            write_state(context.state_path, state)
            _event(
                context,
                "converter_run_failed",
                state="failed",
                data={"message": str(exc)},
            )
            return state

    @staticmethod
    def _run_invocation(
        plan: dict[str, Any],
        context: ConverterRunContext,
    ) -> ComponentInvocation:
        execution = plan["execution"]
        arguments = list(
            _plan_arguments(
                packet_name=execution["packet_name"],
                database_selector=execution["database_selector"],
                config_file=execution["config_file"],
                placeholders_file=execution.get("placeholders_file"),
                config_overrides_file=execution.get("config_overrides_file"),
            )
        )
        arguments.remove("--plan")
        arguments.append(f"--plan-hash={plan['component_plan']['plan_hash']}")
        return ComponentInvocation(
            component="pg_converter",
            arguments=tuple(arguments),
            request_id=f"converter-run-{context.run_id}",
            cwd=Path(execution["project_directory"]),
            timeout_seconds=float(execution["timeout_seconds"]),
            cancel_path=context.cancel_path,
            active_process_path=context.active_process_path,
        )

    def _load_context(self, run_directory: str | Path) -> ConverterRunContext:
        context = _context(run_directory)
        state = read_state(context.state_path)
        if state.get("state") == "not_found":
            raise ConverterRunError(f"converter run does not exist: {context.directory}")
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ConverterRunError("converter run state uses an unsupported schema")
        if state.get("run_id") != context.run_id:
            raise ConverterRunError("converter run directory and run_id do not match")
        if state.get("run_directory") != str(context.directory):
            raise ConverterRunError("converter run state belongs to another directory")
        plan = validate_converter_run_plan(
            read_state(context.plan_path),
            runner=None,
            verify_sources=False,
        )
        if state.get("plan_hash") != plan["plan_hash"]:
            raise ConverterRunError("converter run state and plan hashes do not match")
        return context

    def _spawn_worker(
        self,
        context: ConverterRunContext,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        gate = context.directory / "worker.starting"
        write_text(gate, "wait\n")
        command = [
            sys.executable,
            "-m",
            "pg_play.converter_run_worker",
            "--run-directory",
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
            raise ConverterRunError(f"cannot start converter worker: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            gate.unlink(missing_ok=True)

    def _reconcile_worker(self, context: ConverterRunContext) -> dict[str, Any]:
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
        state["error"] = {
            "code": "worker_lost",
            "message": (
                "converter worker is no longer running; inspect target state before a new run"
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
