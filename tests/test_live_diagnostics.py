from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from pg_diag import runtime_config
from pg_diag.content_loader import load_content
from pg_diag.planner import build_plan

from pg_play.live_diagnostics import (
    LIVE_DIAGNOSTIC_PROFILES,
    LiveDiagnosticsError,
    LiveDiagnosticsManager,
    plan_live_diagnostics,
    validate_live_diagnostics_plan,
)
from pg_play.runner import ComponentCancelledError, ComponentInvocation, process_start_ticks
from pg_play.state import read_events, read_state, write_state


def _target(tmp_path: Path) -> dict[str, Any]:
    key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("test-key", encoding="utf-8")
    known_hosts.write_text("db.example ssh-ed25519 test", encoding="utf-8")
    return {
        "database": {
            "host": "127.0.0.1",
            "port": 5432,
            "database": "postgres",
            "user": "diag",
        },
        "ssh": {
            "host": "db.example",
            "port": 22,
            "user": "postgres",
            "key_path": str(key),
            "known_hosts_path": str(known_hosts),
        },
    }


def _envelope(
    invocation: ComponentInvocation,
    *,
    status: str = "succeeded",
    result: Any = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": "pg_play/component/v1",
        "component": invocation.component,
        "component_version": "test",
        "command": " ".join(invocation.arguments),
        "request_id": invocation.request_id,
        "status": status,
        "result": result,
        "artifacts": artifacts or [],
        "warnings": [],
        "error": None,
    }


class CaptureRunner:
    def __init__(
        self,
        *,
        cancel: bool = False,
        partial: bool = False,
        validation_failure: bool = False,
    ) -> None:
        self.cancel = cancel
        self.partial = partial
        self.validation_failure = validation_failure
        self.invocations: list[ComponentInvocation] = []

    def run(self, invocation: ComponentInvocation) -> dict[str, Any]:
        self.invocations.append(invocation)
        if invocation.arguments[0] == "snapshots":
            if self.cancel:
                raise ComponentCancelledError("capture cancellation requested")
            report = Path(invocation.arguments[invocation.arguments.index("--json-out") + 1])
            html = Path(invocation.arguments[invocation.arguments.index("--html-out") + 1])
            report.write_text("{}\n", encoding="utf-8")
            html.write_text("<html></html>\n", encoding="utf-8")
            return _envelope(
                invocation,
                status="partial" if self.partial else "succeeded",
                result={"summary": {}},
                artifacts=[{"kind": "DiagnosticReport", "path": str(report)}],
            )
        if invocation.arguments[0] == "validate-artifact":
            return _envelope(
                invocation,
                status="failed" if self.validation_failure else "succeeded",
                result={
                    "valid": True,
                    "file_hash": "sha256:report",
                    "summary": {"has_errors": self.partial},
                },
            )
        raise AssertionError(invocation.arguments)


def _prepare_capture(
    tmp_path: Path,
    runner: CaptureRunner,
    monkeypatch: pytest.MonkeyPatch,
    *,
    intent: str = "locks",
) -> tuple[LiveDiagnosticsManager, Path, dict[str, Any]]:
    plan = plan_live_diagnostics(_target(tmp_path), intent, 30, 5)
    manager = LiveDiagnosticsManager(runner=runner)  # type: ignore[arg-type]

    def keep_queued(context: Any, state: dict[str, Any]) -> dict[str, Any]:
        state["worker"] = {
            "pid": os.getpid(),
            "process_start_ticks": process_start_ticks(os.getpid()),
            "mode": "test",
        }
        write_state(context.state_path, state)
        return state

    monkeypatch.setattr(manager, "_spawn_worker", keep_queued)
    state = manager.start(plan, plan["plan_hash"], tmp_path / "captures", "incident-1")
    return manager, tmp_path / "captures" / "incident-1", state


def test_plan_reports_missing_inputs_and_uses_versioned_allowlists(tmp_path: Path) -> None:
    missing = plan_live_diagnostics({}, "performance")
    ready = plan_live_diagnostics(_target(tmp_path), "io", 60, 10)

    assert "target.database.host" in missing["missing_inputs"]
    assert missing["ready"] is False
    assert ready["ready"] is True
    assert ready["capture"]["item_ids"] == list(LIVE_DIAGNOSTIC_PROFILES["io"])
    assert ready["safety"]["mutation"] is False
    assert ready["plan_hash"].startswith("sha256:")

    ready["capture"]["item_ids"].append("overview.unreviewed")
    with pytest.raises(LiveDiagnosticsError, match="hash does not match"):
        validate_live_diagnostics_plan(ready, verify_access_files=False)


def test_plan_rejects_unbounded_or_unknown_capture(tmp_path: Path) -> None:
    plan = plan_live_diagnostics(_target(tmp_path), "everything", 3600, 1)

    assert plan["ready"] is False
    assert any("intent must be one of" in error for error in plan["errors"])
    assert any("duration_seconds" in error for error in plan["errors"])
    assert any("interval_seconds" in error for error in plan["errors"])

    boundary = plan_live_diagnostics(_target(tmp_path), "locks", 600.1, 5)
    assert any("121 snapshots" in error for error in boundary["errors"])


def test_profiles_are_valid_pg_diag_snapshot_plans_for_postgresql_10_to_18() -> None:
    content = load_content(Path(str(files("pg_diag").joinpath("content"))))

    for major in range(10, 19):
        for item_ids in LIVE_DIAGNOSTIC_PROFILES.values():
            plan = build_plan(
                content,
                major * 10000,
                mode=runtime_config.SNAPSHOTS_MODE,
                collection_mode=runtime_config.REMOTE_COLLECTION_MODE,
                item_id=list(item_ids),
            )
            assert {item.item_id for item in plan.items} == set(item_ids)


def test_capture_executes_exact_profile_and_validates_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CaptureRunner(partial=True)
    manager, capture_directory, queued = _prepare_capture(tmp_path, runner, monkeypatch)

    result = manager.execute(capture_directory)

    assert queued["state"] == "queued"
    assert result["state"] == "partial"
    assert result["result"]["summary"] == {"has_errors": True}
    collect, validate = runner.invocations
    assert collect.arguments[0] == "snapshots"
    assert collect.cancel_path == capture_directory / "cancel.request.json"
    assert collect.active_process_path == capture_directory / "active-process.json"
    selected = next(value for value in collect.arguments if value.startswith("--item-id="))
    assert selected == "--item-id=[" + ",".join(LIVE_DIAGNOSTIC_PROFILES["locks"]) + "]"
    assert "--password" not in collect.arguments
    assert validate.arguments == (
        "validate-artifact",
        str(capture_directory / "incident-1.json"),
    )
    events = read_events(capture_directory / "events.jsonl")
    assert [event["type"] for event in events["events"]] == [
        "capture_created",
        "capture_started",
        "capture_completed",
    ]


def test_cancellation_is_durable_and_component_cooperative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CaptureRunner(cancel=True)
    manager, capture_directory, _state = _prepare_capture(tmp_path, runner, monkeypatch)

    requested = manager.cancel(capture_directory, reason="host is under pressure")
    result = manager.execute(capture_directory)

    assert requested["effective_state"] == "cancelling"
    assert read_state(capture_directory / "cancel.request.json")["reason"] == (
        "host is under pressure"
    )
    assert result["state"] == "cancelled"
    assert result["error"]["code"] == "cancelled"


def test_failed_capture_reports_unverified_partial_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, capture_directory, _state = _prepare_capture(
        tmp_path,
        CaptureRunner(validation_failure=True),
        monkeypatch,
    )

    result = manager.execute(capture_directory)

    assert result["state"] == "failed"
    assert result["result"]["unverified_partial_outputs"] == [
        str(capture_directory / "incident-1.json"),
        str(capture_directory / "incident-1.html"),
    ]


def test_status_marks_a_lost_worker_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, capture_directory, _state = _prepare_capture(tmp_path, CaptureRunner(), monkeypatch)
    state = read_state(capture_directory / "state.json")
    state["worker"] = {"pid": 999_999_999, "process_start_ticks": 1}
    (capture_directory / "incident-1.json").write_text("{}\n", encoding="utf-8")
    write_state(capture_directory / "state.json", state)

    cleanup_calls: list[Path] = []
    monkeypatch.setattr(
        "pg_play.live_diagnostics.terminate_recorded_process",
        lambda path: cleanup_calls.append(path) or True,
    )

    result = manager.status(capture_directory)

    assert result["state"] == "interrupted"
    assert result["error"]["code"] == "worker_lost"
    assert result["result"]["unverified_partial_outputs"] == [
        str(capture_directory / "incident-1.json")
    ]
    assert cleanup_calls == [capture_directory / "active-process.json"]
    events = read_events(capture_directory / "events.jsonl")["events"]
    assert events[-1]["data"]["orphan_component_terminated"] is True


def test_start_detaches_worker_with_reviewed_plan_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_live_diagnostics(_target(tmp_path), "cpu", 30, 5)
    launched: dict[str, Any] = {}

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> None:
            return None

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        launched["command"] = command
        launched["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("pg_play.live_diagnostics.subprocess.Popen", fake_popen)
    monkeypatch.setattr("pg_play.live_diagnostics.process_start_ticks", lambda _pid: 99)

    state = LiveDiagnosticsManager().start(
        plan,
        plan["plan_hash"],
        tmp_path / "captures",
        "cpu-incident",
    )

    assert state["state"] == "queued"
    assert state["capture_directory"] == str((tmp_path / "captures" / "cpu-incident").resolve())
    assert state["worker"]["process_start_ticks"] == 99
    assert (tmp_path / "captures" / "cpu-incident").stat().st_mode & 0o777 == 0o700
    assert launched["command"][:3] == [
        os.sys.executable,
        "-m",
        "pg_play.live_diagnostics_worker",
    ]
    assert launched["command"][launched["command"].index("--plan-hash") + 1] == plan["plan_hash"]
    assert launched["kwargs"]["start_new_session"] is True
    assert not (tmp_path / "captures" / "cpu-incident" / "worker.starting").exists()
