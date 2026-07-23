from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from pg_stand.config import load_config

from pg_play.runner import ComponentInvocation, process_start_ticks
from pg_play.service import OrchestrationError, PgPlayService
from pg_play.state import RUN_STATE_SCHEMA_VERSION, read_events, write_state

STAND_CONFIG = Path(str(files("pg_stand").joinpath("configs").joinpath("single.yaml")))


def _envelope(
    invocation: ComponentInvocation,
    *,
    status: str = "succeeded",
    result: Any = None,
) -> dict[str, Any]:
    return {
        "contract_version": "pg_play/component/v1",
        "component": invocation.component,
        "component_version": "test",
        "command": " ".join(invocation.arguments),
        "request_id": invocation.request_id,
        "status": status,
        "result": result,
        "artifacts": [],
        "warnings": [],
        "error": None,
    }


class FakeRunner:
    def __init__(self) -> None:
        self.invocations: list[ComponentInvocation] = []

    def run(self, invocation: ComponentInvocation) -> dict[str, Any]:
        self.invocations.append(invocation)
        arguments = invocation.arguments
        if invocation.component == "pg_configurator" and "--validate-input" in arguments:
            return _envelope(
                invocation,
                result={
                    "valid": True,
                    "normalized_inputs": {
                        "cpu_cores": 1.0,
                        "ram_bytes": 1073741824,
                    },
                },
            )
        if invocation.component == "pg_configurator":
            artifact = {
                "schema_version": "pg_configurator/v1",
                "artifact_hash": "sha256:configuration",
                "inputs": {
                    "cpu_cores": 1.0,
                    "ram_bytes": 1073741824,
                },
                "postgresql_conf": {
                    "max_connections": "200",
                    "shared_buffers": "512MB",
                    "wal_level": "minimal",
                },
            }
            return _envelope(invocation, result={"artifact": artifact})
        if invocation.component == "pg_stand" and "plan" in arguments:
            config = load_config(
                Path(arguments[arguments.index("--config") + 1]),
                project_directory=invocation.cwd,
                postgres_parameters=invocation.input_document,
            )
            return _envelope(
                invocation,
                result={
                    "required_action": "none",
                    "plan_hash": "sha256:stand",
                    "desired_state_hash": config.config_hash,
                },
            )
        if invocation.component == "pg_workload" and "plan" in arguments:
            return _envelope(
                invocation,
                status="planned",
                result={
                    "schema_version": "pg_workload/plan-v1",
                    "operation": next(
                        value.split("=", 1)[1]
                        for value in arguments
                        if value.startswith("--operation=")
                    ),
                    "plan_hash": "sha256:workload",
                },
            )
        if invocation.component == "pg_diag":
            return _envelope(invocation, result={"items": []})
        if invocation.component == "pg_perf_bench" and arguments[0] == "plan":
            return _envelope(
                invocation,
                result={
                    "schema_version": "pg_perf_bench/plan-v1",
                    "plan_hash": "sha256:benchmark",
                    "configuration": {},
                    "inputs": {},
                },
            )
        if invocation.component == "pg_perf_bench" and arguments[0] == "join":
            report_name = arguments[arguments.index("--report-name") + 1]
            return _envelope(
                invocation,
                result={"report_name": report_name, "outputs": []},
            )
        return _envelope(invocation, result={"valid": True})


def _manifest(tmp_path: Path, *, benchmark: bool = False, benchmark_profile: bool = False) -> Path:
    workload = tmp_path / "workload"
    workload.mkdir()
    path = tmp_path / "experiment.yaml"
    benchmark_section = (
        """
  benchmark:
    database: benchmark_db
    workload_profile: imdb
    workload_scale: 0.5
    workload_duration_seconds: 10
    clients: [1, 4]
"""
        if benchmark_profile
        else """
  benchmark:
    database: benchmark_db
    benchmark_type: default
    clients: [1, 4]
    init_command: pgbench -i
    workload_command: pgbench -T 30
"""
        if benchmark
        else ""
    )
    path.write_text(
        f"""api_version: pg_play/v1
kind: PostgreSQLExperiment
metadata:
  id: pg18-baseline
spec:
  artifact_root: artifacts
  stand:
    config: {STAND_CONFIG}
  configurator:
    inputs:
      db_cpu: 1
      db_ram: 1Gi
      pg_version: '18'
      db_duty: mixed
  workload:
    project: workload
    profiles: [simple]
    scale: 2
  diagnostics:
    mode: snapshots
    duration_seconds: 60
    interval_seconds: 10
{benchmark_section}
""",
        encoding="utf-8",
    )
    return path


def test_plan_is_deterministic_and_passes_configuration_as_stdin(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = PgPlayService(runner=runner)  # type: ignore[arg-type]
    manifest = _manifest(tmp_path)

    first = service.plan_experiment(manifest)
    second = service.plan_experiment(manifest)

    assert first == second
    assert first["plan_hash"].startswith("sha256:")
    assert first["configuration"]["stand_managed_parameters"] == {"wal_level": "minimal"}
    assert first["components"]["pg_perf_bench"] == "test"
    assert first["benchmark"] is None
    stand_calls = [
        call
        for call in runner.invocations
        if call.component == "pg_stand" and "plan" in call.arguments
    ]
    assert stand_calls[0].input_document == {
        "max_connections": "200",
        "shared_buffers": "512MB",
    }
    assert "--parameters-json=-" in stand_calls[0].arguments
    assert stand_calls[0].cwd == tmp_path.resolve()
    config_call = next(call for call in runner.invocations if call.component == "pg_configurator")
    assert config_call.input_document["inputs"]["replication_mode"] == "none"


def test_validation_uses_only_non_mutating_component_commands(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = PgPlayService(runner=runner).validate_experiment(_manifest(tmp_path))  # type: ignore[arg-type]

    assert result["valid"] is True
    commands = {(call.component, call.arguments[0]) for call in runner.invocations}
    assert commands == {
        ("pg_configurator", "--input-json=-"),
        ("pg_stand", "--config"),
        ("pg_workload", "validate"),
        ("pg_diag", "validate"),
        ("pg_diag", "explain-plan"),
        ("pg_perf_bench", "validate"),
    }


def test_plan_includes_reviewed_benchmark_for_pg_stand(tmp_path: Path) -> None:
    runner = FakeRunner()
    plan = PgPlayService(runner=runner).plan_experiment(  # type: ignore[arg-type]
        _manifest(tmp_path, benchmark=True)
    )

    assert plan["benchmark"]["plan_hash"] == "sha256:benchmark"
    call = next(
        invocation
        for invocation in runner.invocations
        if invocation.component == "pg_perf_bench" and invocation.arguments[0] == "plan"
    )
    assert call.arguments[1:3] == ("benchmark", "--connection-type=docker")
    assert "--allow-database-reset" in call.arguments
    assert "--pgbench-clients" in call.arguments
    assert call.arguments[call.arguments.index("--pgbench-clients") + 1] == "1,4"
    assert "--pgbench-path" not in call.arguments
    assert "--psql-path" not in call.arguments
    assert call.arguments[call.arguments.index("--system-metrics-interval") + 1] == "1.0"


def test_plan_passes_bundled_benchmark_profile_without_manual_commands(tmp_path: Path) -> None:
    runner = FakeRunner()
    PgPlayService(runner=runner).plan_experiment(  # type: ignore[arg-type]
        _manifest(tmp_path, benchmark_profile=True)
    )

    call = next(
        invocation
        for invocation in runner.invocations
        if invocation.component == "pg_perf_bench" and invocation.arguments[0] == "plan"
    )
    assert call.arguments[call.arguments.index("--workload-profile") + 1] == "imdb"
    assert call.arguments[call.arguments.index("--workload-scale") + 1] == "0.5"
    assert call.arguments[call.arguments.index("--workload-duration-seconds") + 1] == "10"
    assert "--init-command" not in call.arguments
    assert "--workload-command" not in call.arguments


def test_plan_includes_separate_phases_database_recreation_and_workload_cadence(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, benchmark_profile=True)
    text = manifest.read_text(encoding="utf-8").replace(
        "    scale: 2",
        "    scale: 2\n    pgbench_duration_seconds: 30\n    job_interval_seconds: 5",
    )
    text += """  phases:
    benchmark: true
    workload_diagnostics: true
    recreate_workload_database: true
"""
    manifest.write_text(text, encoding="utf-8")
    runner = FakeRunner()

    plan = PgPlayService(runner=runner).plan_experiment(manifest)  # type: ignore[arg-type]

    assert plan["phases"] == {
        "benchmark": True,
        "workload_diagnostics": True,
        "recreate_workload_database": True,
    }
    workload_plans = [
        call
        for call in runner.invocations
        if call.component == "pg_workload" and "plan" in call.arguments
    ]
    prepare = next(call for call in workload_plans if "--operation=prepare-db" in call.arguments)
    scheduler = next(call for call in workload_plans if "--operation=scheduler" in call.arguments)
    assert "--recreate" in prepare.arguments
    assert prepare.arguments[prepare.arguments.index("--pgbench-duration") + 1] == "30"
    assert scheduler.arguments[scheduler.arguments.index("--job-interval-seconds") + 1] == "5"


def test_join_benchmark_reports_passes_only_the_exact_selected_files(tmp_path: Path) -> None:
    baseline = tmp_path / "pg18-baseline.json"
    tuned = tmp_path / "pg18-tuned.json"
    baseline.write_text("{}", encoding="utf-8")
    tuned.write_text("{}", encoding="utf-8")
    runner = FakeRunner()

    result = PgPlayService(runner=runner).join_benchmark_reports(  # type: ignore[arg-type]
        [str(baseline), str(tuned)],
        "optimize-db-config",
        str(tmp_path / "joined"),
        "pg18-three-configs",
    )

    call = runner.invocations[-1]
    assert call.component == "pg_perf_bench"
    assert call.arguments.count("--report") == 2
    assert str(baseline.resolve()) in call.arguments
    assert str(tuned.resolve()) in call.arguments
    assert result["report_name"] == "pg18-three-configs"


def test_teardown_uses_owned_manifest_targets_and_explicit_clear_data(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    runner = FakeRunner()

    result = PgPlayService(runner=runner).teardown_experiment(  # type: ignore[arg-type]
        manifest,
        clear_stand_data=True,
    )

    workload, stand = runner.invocations[-2:]
    assert workload.component == "pg_workload"
    assert workload.arguments == ("stop", "--root", str((tmp_path / "workload").resolve()))
    assert stand.component == "pg_stand"
    assert stand.arguments[-2:] == ("down", "--clear-data")
    assert stand.cwd == tmp_path.resolve()
    assert result["clear_stand_data"] is True


def test_validation_rejects_managed_tls_before_invoking_components(tmp_path: Path) -> None:
    config = tmp_path / "stand-tls.yaml"
    config.write_text(
        STAND_CONFIG.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(str(STAND_CONFIG), str(config)),
        encoding="utf-8",
    )
    runner = FakeRunner()

    with pytest.raises(OrchestrationError, match="does not yet orchestrate pg_stand TLS"):
        PgPlayService(runner=runner).validate_experiment(manifest)  # type: ignore[arg-type]

    assert runner.invocations == []


def test_configurator_literals_are_normalized_for_pg_stand() -> None:
    assert PgPlayService._semantic_postgresql_parameters(
        {
            "log_statement": "'ddl'",
            "synchronous_standby_names": "''",
            "log_line_prefix": "'%m user=%u O''Reilly '",
            "work_mem": "16MB",
        }
    ) == {
        "log_statement": "ddl",
        "synchronous_standby_names": "",
        "log_line_prefix": "%m user=%u O'Reilly ",
        "work_mem": "16MB",
    }


def _create_durable_run(
    tmp_path: Path,
    *,
    run_id: str = "durable-run",
) -> tuple[PgPlayService, Path, Any, dict[str, Any], dict[str, Any]]:
    runner = FakeRunner()
    service = PgPlayService(runner=runner)  # type: ignore[arg-type]
    manifest_path = _manifest(tmp_path)
    plan = service.plan_experiment(manifest_path)
    manifest, stored_plan, state = service._create_run(
        manifest_path,
        plan_hash=plan["plan_hash"],
        run_id=run_id,
        allow_existing_success=False,
    )
    return service, manifest_path, manifest, stored_plan, state


def test_create_run_persists_recovery_inputs_and_initial_event(tmp_path: Path) -> None:
    service, _manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])

    assert state["schema_version"] == RUN_STATE_SCHEMA_VERSION
    assert state["state"] == "queued"
    assert state["attempt"] == 1
    assert context.state_path.is_file()
    assert (context.run_directory / "experiment.yaml").is_file()
    assert (context.run_directory / "plan.json").is_file()
    assert (context.run_directory / "postgresql-parameters.json").is_file()
    assert plan["plan_hash"] == state["plan_hash"]
    events = read_events(context.events_path)["events"]
    assert [(event["sequence"], event["type"]) for event in events] == [(1, "run_created")]


def test_status_marks_a_run_interrupted_when_its_worker_was_lost(tmp_path: Path) -> None:
    service, manifest_path, manifest, _plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "running"
    state["worker"] = {
        "pid": 2_147_483_647,
        "process_start_ticks": 1,
        "started_at": state["created_at"],
        "mode": "background",
    }
    write_state(context.state_path, state)

    result = service.experiment_status(manifest_path, state["run_id"])

    assert result["state"] == "interrupted"
    assert result["error"]["code"] == "worker_lost"
    assert read_events(context.events_path)["events"][-1]["type"] == "worker_lost"


def test_start_returns_after_launching_a_detached_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    service = PgPlayService(runner=runner)  # type: ignore[arg-type]
    manifest_path = _manifest(tmp_path)
    plan = service.plan_experiment(manifest_path)
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = os.getpid()

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("pg_play.service.subprocess.Popen", fake_popen)

    state = service.start_experiment(
        manifest_path,
        plan_hash=plan["plan_hash"],
        run_id="async-run",
    )

    assert state["state"] == "queued"
    assert state["worker"]["mode"] == "background"
    assert captured["command"][1:3] == ["-m", "pg_play.worker"]
    assert "--resume" not in captured["command"]
    assert captured["kwargs"]["start_new_session"] is True
    gate = Path(captured["command"][captured["command"].index("--start-gate") + 1])
    assert not gate.exists()


def test_worker_is_stopped_if_its_durable_start_record_cannot_be_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _manifest_path, manifest, _plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])

    class FakeProcess:
        pid = os.getpid()
        terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            return 0

        def kill(self) -> None:
            self.terminated = True

    process = FakeProcess()
    monkeypatch.setattr("pg_play.service.subprocess.Popen", lambda *_args, **_kwargs: process)
    original_event = service._event

    def fail_worker_started(context: Any, event_type: str, **kwargs: Any) -> dict[str, Any]:
        if event_type == "worker_started":
            raise OSError("event journal unavailable")
        return original_event(context, event_type, **kwargs)

    monkeypatch.setattr(service, "_event", fail_worker_started)

    with pytest.raises(OrchestrationError, match="cannot start experiment worker"):
        service._spawn_worker(manifest, state, resume=False)

    assert process.terminated is True
    assert not (context.run_directory / "worker.starting").exists()
    assert service.experiment_status(manifest.source, state["run_id"])["state"] == "failed"


def test_cancel_finishes_an_interrupted_run_without_signalling_unknown_pids(
    tmp_path: Path,
) -> None:
    service, manifest_path, manifest, _plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "interrupted"
    state["worker"] = None
    write_state(context.state_path, state)

    result = service.cancel_experiment(
        manifest_path,
        state["run_id"],
        reason="operator stopped the experiment",
    )

    assert result["state"] == "cancelled"
    assert result["cancellation"]["reason"] == "operator stopped the experiment"
    assert [event["type"] for event in read_events(context.events_path)["events"][-2:]] == [
        "cancellation_requested",
        "run_cancelled",
    ]


def test_cancel_after_worker_loss_stops_a_started_workload(tmp_path: Path) -> None:
    service, manifest_path, manifest, _plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "interrupted"
    state["worker"] = None
    state["steps"] = [
        {
            "name": "start-workload",
            "status": "succeeded",
            "attempt": 1,
            "resume_policy": "desired_state",
            "artifacts": [],
        }
    ]
    write_state(context.state_path, state)
    service.runner.invocations.clear()

    result = service.cancel_experiment(manifest_path, state["run_id"], reason="operator stop")

    assert result["state"] == "cancelled"
    stop_calls = [
        invocation
        for invocation in service.runner.invocations
        if invocation.component == "pg_workload" and invocation.arguments[0] == "stop"
    ]
    assert len(stop_calls) == 1
    assert service._latest_step_status(result, "stop-workload") == "succeeded"


def test_execution_rejects_changed_stand_desired_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    original_load_config = load_config

    class ChangedConfig:
        def __init__(self, wrapped: Any) -> None:
            self._wrapped = wrapped
            self.config_hash = "sha256:changed-stand"

        def __getattr__(self, name: str) -> Any:
            return getattr(self._wrapped, name)

    def changed_load_config(*args: Any, **kwargs: Any) -> ChangedConfig:
        return ChangedConfig(original_load_config(*args, **kwargs))

    monkeypatch.setattr("pg_play.service.load_config", changed_load_config)

    with pytest.raises(OrchestrationError, match="stand desired configuration changed"):
        service._execute_experiment(manifest, plan, state, resume=False)

    persisted = service.experiment_status(manifest.source, state["run_id"])
    assert persisted["state"] == "failed"


def test_resume_rejects_changed_stand_desired_state_before_spawning_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "failed"
    write_state(context.state_path, state)
    original_load_config = load_config

    class ChangedConfig:
        def __init__(self, wrapped: Any) -> None:
            self._wrapped = wrapped
            self.config_hash = "sha256:changed-stand"

        def __getattr__(self, name: str) -> Any:
            return getattr(self._wrapped, name)

    monkeypatch.setattr(
        "pg_play.service.load_config",
        lambda *args, **kwargs: ChangedConfig(original_load_config(*args, **kwargs)),
    )

    with pytest.raises(OrchestrationError, match="stand desired configuration changed"):
        service.resume_experiment(
            manifest_path,
            plan_hash=plan["plan_hash"],
            run_id=state["run_id"],
        )


def test_resume_invalidates_missing_artifacts_and_retries_only_allowlisted_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "failed"
    state["steps"] = [
        {
            "name": "diagnostics",
            "status": "succeeded",
            "attempt": 1,
            "resume_policy": "read_only",
            "artifacts": [{"path": "missing-report.json"}],
        }
    ]
    write_state(context.state_path, state)
    spawned: dict[str, Any] = {}

    def fake_spawn(
        _manifest: Any,
        queued_state: dict[str, Any],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        spawned["resume"] = resume
        return queued_state

    monkeypatch.setattr(service, "_spawn_worker", fake_spawn)

    result = service.resume_experiment(
        manifest_path,
        plan_hash=plan["plan_hash"],
        run_id=state["run_id"],
    )

    assert spawned == {"resume": True}
    assert result["state"] == "queued"
    assert result["attempt"] == 2
    assert result["steps"][0]["resume_validation"]["valid"] is False
    assert [event["type"] for event in read_events(context.events_path)["events"][-2:]] == [
        "step_artifacts_invalidated",
        "resume_requested",
    ]


def test_resume_rejects_a_step_without_an_exact_safe_policy(tmp_path: Path) -> None:
    service, manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "failed"
    state["steps"] = [
        {
            "name": "arbitrary-shell",
            "status": "failed",
            "attempt": 1,
            "resume_policy": "retry",
            "artifacts": [],
        }
    ]
    write_state(context.state_path, state)

    with pytest.raises(OrchestrationError, match="no safe resume policy"):
        service.resume_experiment(
            manifest_path,
            plan_hash=plan["plan_hash"],
            run_id=state["run_id"],
        )


def test_resume_rejects_a_live_worker_even_for_a_failed_state(tmp_path: Path) -> None:
    service, manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "failed"
    state["worker"] = {
        "pid": os.getpid(),
        "process_start_ticks": process_start_ticks(os.getpid()),
        "started_at": state["created_at"],
        "mode": "background",
    }
    write_state(context.state_path, state)

    with pytest.raises(OrchestrationError, match="still has a live worker"):
        service.resume_experiment(
            manifest_path,
            plan_hash=plan["plan_hash"],
            run_id=state["run_id"],
        )


def test_resume_rejects_a_tampered_stored_plan(tmp_path: Path) -> None:
    service, manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "failed"
    write_state(context.state_path, state)
    stored_plan = context.run_directory / "plan.json"
    document = json.loads(stored_plan.read_text(encoding="utf-8"))
    document["warnings"].append("tampered")
    stored_plan.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OrchestrationError, match="content failed hash verification"):
        service.resume_experiment(
            manifest_path,
            plan_hash=plan["plan_hash"],
            run_id=state["run_id"],
        )


def test_resume_rejects_changed_component_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, manifest_path, manifest, plan, state = _create_durable_run(tmp_path)
    context = service._run_context(manifest, state["run_id"])
    state["state"] = "failed"
    write_state(context.state_path, state)
    original_invoke = service._invoke

    def changed_version(
        component: str,
        arguments: tuple[str, ...],
        **kwargs: Any,
    ) -> dict[str, Any]:
        envelope = original_invoke(component, arguments, **kwargs)
        if component == "pg_diag" and arguments == ("--component-capabilities",):
            envelope["component_version"] = "changed"
        return envelope

    monkeypatch.setattr(service, "_invoke", changed_version)

    with pytest.raises(OrchestrationError, match="component version changed for pg_diag"):
        service.resume_experiment(
            manifest_path,
            plan_hash=plan["plan_hash"],
            run_id=state["run_id"],
        )
