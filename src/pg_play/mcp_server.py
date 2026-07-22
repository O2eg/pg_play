"""MCP transport exposing only typed high-level pg_play operations."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from mcp.server.fastmcp import FastMCP

from pg_play.service import PgPlayService

mcp = FastMCP(
    "pg_play",
    instructions=(
        "Plan and run reproducible PostgreSQL experiments through pg_play. "
        "Always call validate_experiment and plan_experiment before run_experiment."
    ),
    json_response=True,
)


def _service() -> PgPlayService:
    return PgPlayService()


@mcp.tool()
def component_capabilities(component: str | None = None) -> dict[str, Any]:
    """Return installed component contracts; optionally select one component."""
    return _service().component_capabilities(component)


@mcp.tool()
def validate_experiment(manifest_path: str) -> dict[str, Any]:
    """Validate an experiment manifest and all non-mutating component inputs."""
    return _service().validate_experiment(manifest_path)


@mcp.tool()
def plan_experiment(manifest_path: str) -> dict[str, Any]:
    """Build a read-only plan and return the plan hash required for execution."""
    return _service().plan_experiment(manifest_path)


@mcp.tool()
def run_experiment(manifest_path: str, plan_hash: str, run_id: str) -> dict[str, Any]:
    """Execute an unchanged plan under an explicit immutable run identifier."""
    return _service().run_experiment(manifest_path, plan_hash=plan_hash, run_id=run_id)


@mcp.tool()
def experiment_status(manifest_path: str, run_id: str) -> dict[str, Any]:
    """Read the durable state of one experiment run without changing it."""
    return _service().experiment_status(manifest_path, run_id)


@mcp.tool()
def teardown_experiment(
    manifest_path: str,
    clear_stand_data: bool = False,
) -> dict[str, Any]:
    """Stop owned workload processes and remove the experiment stand and optional data."""
    return _service().teardown_experiment(
        manifest_path,
        clear_stand_data=clear_stand_data,
    )


@mcp.tool()
def inspect_diagnostic_report(report_path: str) -> dict[str, Any]:
    """Validate and summarize one pg_diag JSON artifact."""
    return _service().inspect_report(report_path)


@mcp.tool()
def compare_diagnostic_reports(
    baseline_path: str,
    candidate_path: str,
) -> dict[str, Any]:
    """Compare two valid pg_diag artifacts using deterministic summary deltas."""
    return _service().compare_reports(baseline_path, candidate_path)


@mcp.tool()
def inspect_benchmark_report(report_path: str) -> dict[str, Any]:
    """Validate and summarize one pg_perf_bench JSON artifact."""
    return _service().inspect_benchmark_report(report_path)


@mcp.tool()
def compare_benchmark_reports(
    baseline_path: str,
    candidate_path: str,
) -> dict[str, Any]:
    """Compare two compatible pg_perf_bench artifacts and TPS deltas."""
    return _service().compare_benchmark_reports(baseline_path, candidate_path)


@mcp.tool()
def join_benchmark_reports(
    report_paths: list[str],
    join_task: str,
    output_directory: str,
    report_name: str,
) -> dict[str, Any]:
    """Join an exact report set after validating controlled experiment dimensions."""
    return _service().join_benchmark_reports(
        report_paths,
        join_task,
        output_directory,
        report_name,
    )


@mcp.tool()
def benchmark_profiles() -> dict[str, Any]:
    """List installed pg_perf_bench maximum-TPS workload profiles."""
    return _service().benchmark_profiles()


@mcp.tool()
def benchmark_join_tasks() -> dict[str, Any]:
    """List installed pg_perf_bench JOIN scenarios and controlled dimensions."""
    return _service().benchmark_join_tasks()


@mcp.resource("pgplay://experiment-schema")
def experiment_schema() -> str:
    """The pg_play/v1 experiment manifest JSON Schema."""
    path = files("pg_play").joinpath("schema/pg_play-v1.schema.json")
    return path.read_text(encoding="utf-8")


@mcp.resource("pgplay://component-contract")
def component_contract() -> str:
    """Machine envelope fields expected from every component."""
    return json.dumps(
        {
            "contract_version": "pg_play/component/v1",
            "capability_schema_version": "pg_play/capabilities/v1",
            "capability_command_fields": [
                "mutates_target",
                "machine_output",
                "accepts_plan_hash",
            ],
            "machine_interface": {
                "machine_flag": "--machine",
                "request_id_option": "--request-id",
                "capabilities_option": "--component-capabilities",
            },
            "required_fields": [
                "contract_version",
                "component",
                "component_version",
                "command",
                "request_id",
                "status",
                "result",
                "artifacts",
                "warnings",
                "error",
            ],
        },
        indent=2,
        sort_keys=True,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
