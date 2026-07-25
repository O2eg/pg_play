from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pg_play.contract import canonical_hash
from pg_play.converter_runs import (
    ConverterRunError,
    ConverterRunManager,
    plan_converter_run,
    validate_converter_run_plan,
)
from pg_play.runner import ComponentCancelledError, ComponentInvocation, process_start_ticks
from pg_play.state import read_events, read_state, write_state


def _component_envelope(
    invocation: ComponentInvocation,
    *,
    status: str,
    result: Any,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": "pg_play/component/v1",
        "component": "pg_converter",
        "component_version": "2.0b2",
        "command": "plan" if "--plan" in invocation.arguments else "run",
        "request_id": invocation.request_id,
        "status": status,
        "result": result,
        "artifacts": [],
        "warnings": [],
        "error": error,
    }


class ConverterRunner:
    def __init__(self, *, cancel: bool = False, run_status: str = "succeeded") -> None:
        self.cancel = cancel
        self.run_status = run_status
        self.source_version = "one"
        self.invocations: list[ComponentInvocation] = []

    def run(self, invocation: ComponentInvocation) -> dict[str, Any]:
        self.invocations.append(invocation)
        if "--plan" in invocation.arguments:
            option_values = {
                argument.partition("=")[0]: argument.partition("=")[2]
                for argument in invocation.arguments
                if "=" in argument
            }
            project = invocation.cwd.resolve()
            component_plan: dict[str, Any] = {
                "schema_version": "pg_converter/plan-v1",
                "operation": "run",
                "request": {
                    "packet_name": option_values["--packet-name"],
                    "database_selector": option_values["--db-name"],
                    "sequential": True,
                },
                "packet": {
                    "name": option_values["--packet-name"],
                    "type": "default",
                    "packet_hash": "a" * 32,
                    "source_hash": canonical_hash(self.source_version),
                },
                "target": {
                    "database_aliases": ["db_a", "db_b"],
                    "database_count": 2,
                },
                "sources": {
                    "project_directory": str(project),
                    "config_file": str(Path(option_values["--config-file"]).resolve()),
                    "placeholders_file": option_values.get("--placeholders-file") or None,
                    "config_overrides_file": option_values.get("--config-overrides-file") or None,
                },
                "safety": {
                    "mutates_target": True,
                    "arbitrary_python": False,
                },
            }
            component_plan["plan_hash"] = canonical_hash(component_plan)
            return _component_envelope(
                invocation,
                status="planned",
                result=component_plan,
            )
        if self.cancel:
            raise ComponentCancelledError("converter cancellation requested")
        result = {
            "schema_version": "pg_converter/machine-result-v1",
            "plan_hash": next(
                argument.partition("=")[2]
                for argument in invocation.arguments
                if argument.startswith("--plan-hash=")
            ),
            "packet": {
                "name": "test_packet",
                "type": "default",
                "packet_hash": "a" * 32,
            },
            "command_type": "run",
            "success": self.run_status == "succeeded",
            "database_count": 2,
            "result_counts": {"success": 2},
            "failure_categories": {},
            "databases": {},
        }
        return _component_envelope(
            invocation,
            status=self.run_status,
            result=None if self.run_status == "blocked" else result,
            error=(
                {
                    "code": "precondition_failed",
                    "message": "reviewed component plan is stale",
                }
                if self.run_status == "blocked"
                else None
            ),
        )


def _plan(tmp_path: Path, runner: ConverterRunner) -> dict[str, Any]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    config = project / "pg_converter.conf"
    config.write_text("[databases]\n", encoding="utf-8")
    return plan_converter_run(
        runner,  # type: ignore[arg-type]
        project_directory=project,
        config_file=str(config),
        packet_name="test_packet",
        database_selector="ALL",
        timeout_seconds=120,
    )


def _prepare_run(
    tmp_path: Path,
    runner: ConverterRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ConverterRunManager, Path, dict[str, Any]]:
    plan = _plan(tmp_path, runner)
    manager = ConverterRunManager(runner=runner)  # type: ignore[arg-type]

    def keep_queued(context: Any, state: dict[str, Any]) -> dict[str, Any]:
        state["worker"] = {
            "pid": os.getpid(),
            "process_start_ticks": process_start_ticks(os.getpid()),
            "mode": "test",
        }
        write_state(context.state_path, state)
        return state

    monkeypatch.setattr(manager, "_spawn_worker", keep_queued)
    state = manager.start(plan, plan["plan_hash"], tmp_path / "runs", "packet-run-1")
    return manager, tmp_path / "runs" / "packet-run-1", state


def test_plan_wraps_exact_component_plan_and_safety(tmp_path: Path) -> None:
    runner = ConverterRunner()

    plan = _plan(tmp_path, runner)

    assert plan["schema_version"] == "pg_play/converter-run-plan-v1"
    assert plan["component"] == "pg_converter"
    assert plan["component_plan"]["schema_version"] == "pg_converter/plan-v1"
    assert plan["component_plan"]["target"]["database_aliases"] == ["db_a", "db_b"]
    assert plan["execution"]["sequential"] is True
    assert plan["execution"]["timeout_seconds"] == 120
    assert plan["safety"]["mutates_target"] is True
    assert plan["safety"]["force"] is False
    assert plan["plan_hash"].startswith("sha256:")
    invocation = runner.invocations[0]
    assert invocation.component == "pg_converter"
    assert "--seq" in invocation.arguments
    assert "--plan" in invocation.arguments
    assert not any(argument.startswith("--force") for argument in invocation.arguments)


def test_plan_rejects_tampering_and_changed_packet_source(tmp_path: Path) -> None:
    runner = ConverterRunner()
    plan = _plan(tmp_path, runner)
    plan["execution"]["database_selector"] = "db_a"

    with pytest.raises(ConverterRunError, match="hash does not match"):
        validate_converter_run_plan(plan, runner=runner, verify_sources=False)

    plan = _plan(tmp_path / "second", runner)
    runner.source_version = "two"
    with pytest.raises(ConverterRunError, match="stale"):
        validate_converter_run_plan(plan, runner=runner, verify_sources=True)


def test_run_executes_reviewed_component_hash_and_writes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ConverterRunner()
    manager, run_directory, queued = _prepare_run(tmp_path, runner, monkeypatch)

    result = manager.execute(run_directory)

    assert queued["state"] == "queued"
    assert result["state"] == "succeeded"
    assert result["result"]["schema_version"] == "pg_converter/machine-result-v1"
    assert (run_directory / "result.json").stat().st_mode & 0o777 == 0o600
    execute = runner.invocations[-1]
    assert execute.component == "pg_converter"
    assert "--seq" in execute.arguments
    assert f"--plan-hash={queued['component_plan_hash']}" in execute.arguments
    assert execute.cancel_path == run_directory / "cancel.request.json"
    assert execute.active_process_path == run_directory / "active-process.json"
    assert not any(argument.startswith("--force") for argument in execute.arguments)
    events = read_events(run_directory / "events.jsonl")["events"]
    assert [event["type"] for event in events] == [
        "converter_run_created",
        "converter_run_started",
        "converter_run_completed",
    ]


def test_cancellation_is_durable_and_component_cooperative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ConverterRunner(cancel=True)
    manager, run_directory, _state = _prepare_run(tmp_path, runner, monkeypatch)

    requested = manager.cancel(run_directory, reason="operator stopped migration")
    result = manager.execute(run_directory)

    assert requested["effective_state"] == "cancelling"
    assert result["state"] == "cancelled"
    assert result["error"]["code"] == "cancelled"


def test_component_precondition_block_is_preserved_as_failed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ConverterRunner(run_status="blocked")
    manager, run_directory, _state = _prepare_run(tmp_path, runner, monkeypatch)

    result = manager.execute(run_directory)

    assert result["state"] == "failed"
    assert result["result"] is None
    assert result["error"]["code"] == "precondition_failed"
    events = read_events(run_directory / "events.jsonl")["events"]
    assert events[-1]["type"] == "converter_run_blocked"
    assert events[-1]["data"]["component_status"] == "blocked"


def test_status_marks_lost_converter_worker_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ConverterRunner()
    manager, run_directory, _state = _prepare_run(tmp_path, runner, monkeypatch)
    state = read_state(run_directory / "state.json")
    state["worker"] = {"pid": 999_999_999, "process_start_ticks": 1}
    write_state(run_directory / "state.json", state)
    cleanup_calls: list[Path] = []
    monkeypatch.setattr(
        "pg_play.converter_runs.terminate_recorded_process",
        lambda path: cleanup_calls.append(path) or True,
    )

    result = manager.status(run_directory)

    assert result["state"] == "interrupted"
    assert result["error"]["code"] == "worker_lost"
    assert cleanup_calls == [run_directory / "active-process.json"]
