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
