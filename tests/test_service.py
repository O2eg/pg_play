from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pg_play.runner import ComponentInvocation
from pg_play.service import OrchestrationError, PgPlayService

STAND_CONFIG = Path("/home/oleg/Desktop/dev/pg_stand/src/pg_stand/configs/single.yaml")


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
        return _envelope(invocation, result={"valid": True})


def _manifest(tmp_path: Path) -> Path:
    workload = tmp_path / "workload"
    workload.mkdir()
    path = tmp_path / "experiment.yaml"
    path.write_text(
        f"""api_version: pg_play/v1
kind: PostgreSQLExperiment
metadata:
  id: pg17-baseline
spec:
  artifact_root: artifacts
  stand:
    config: {STAND_CONFIG}
  configurator:
    inputs:
      db_cpu: 1
      db_ram: 1Gi
      pg_version: '17'
      db_duty: mixed
  workload:
    project: workload
    profiles: [simple]
    scale: 2
  diagnostics:
    mode: snapshots
    duration_seconds: 60
    interval_seconds: 10
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
    }


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
