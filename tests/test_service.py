from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from pg_play.runner import ComponentInvocation
from pg_play.service import OrchestrationError, PgPlayService

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
            return _envelope(
                invocation,
                result={
                    "required_action": "none",
                    "plan_hash": "sha256:stand",
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
