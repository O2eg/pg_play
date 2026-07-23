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
        "Always call validate_experiment and plan_experiment before start_experiment. "
        "Use experiment_status and experiment_events to observe a durable run. "
        "For incidents on existing servers, call plan_live_diagnostics before "
        "start_live_diagnostics and observe the detached capture through its status and events."
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
def plan_live_diagnostics(
    target: dict[str, Any],
    intent: str = "performance",
    duration_seconds: float = 60,
    interval_seconds: float = 5,
) -> dict[str, Any]:
    """Plan a bounded read-only pg_diag capture and report missing access inputs."""
    return _service().plan_live_diagnostics(
        target,
        intent,
        duration_seconds,
        interval_seconds,
    )


@mcp.tool()
def start_live_diagnostics(
    plan: dict[str, Any],
    plan_hash: str,
    output_directory: str,
    capture_id: str,
) -> dict[str, Any]:
    """Start a detached live diagnostic capture from an unchanged reviewed plan."""
    return _service().start_live_diagnostics(
        plan,
        plan_hash,
        output_directory,
        capture_id,
    )


@mcp.tool()
def live_diagnostics_status(capture_directory: str) -> dict[str, Any]:
    """Read durable capture state and detect a lost diagnostic worker."""
    return _service().live_diagnostics_status(capture_directory)


@mcp.tool()
def live_diagnostics_events(
    capture_directory: str,
    after_sequence: int = 0,
    limit: int = 1000,
) -> dict[str, Any]:
    """Read ordered live-capture events after the supplied sequence cursor."""
    return _service().live_diagnostics_events(
        capture_directory,
        after_sequence=after_sequence,
        limit=limit,
    )


@mcp.tool()
def cancel_live_diagnostics(
    capture_directory: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Request cooperative cancellation of an active live diagnostic capture."""
    return _service().cancel_live_diagnostics(capture_directory, reason=reason)


@mcp.tool()
def plan_configuration_review(
    target: dict[str, Any],
    tuning_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan a read-only review and report missing access or tuning inputs."""
    return _service().plan_configuration_review(target, tuning_inputs)


@mcp.tool()
def collect_configuration_facts(
    target: dict[str, Any],
    output_directory: str,
    review_id: str,
) -> dict[str, Any]:
    """Collect the minimal pg_diag item set and extract normalized server facts."""
    return _service().collect_configuration_facts(target, output_directory, review_id)


@mcp.tool()
def generate_configuration_candidate(
    facts_path: str,
    tuning_inputs: dict[str, Any],
    output_directory: str,
    review_id: str,
) -> dict[str, Any]:
    """Generate a pg_configurator candidate from reviewed host facts and intent."""
    return _service().generate_configuration_candidate(
        facts_path, tuning_inputs, output_directory, review_id
    )


@mcp.tool()
def compare_configuration_candidate(
    facts_path: str,
    candidate_path: str,
    output_directory: str,
    review_id: str,
) -> dict[str, Any]:
    """Write JSON and Markdown tables for settings that differ from the candidate."""
    return _service().compare_configuration_candidate(
        facts_path, candidate_path, output_directory, review_id
    )


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
    """Execute synchronously for compatibility; prefer start_experiment."""
    return _service().run_experiment(manifest_path, plan_hash=plan_hash, run_id=run_id)


@mcp.tool()
def start_experiment(manifest_path: str, plan_hash: str, run_id: str) -> dict[str, Any]:
    """Start an unchanged plan in a detached durable worker and return immediately."""
    return _service().start_experiment(manifest_path, plan_hash=plan_hash, run_id=run_id)


@mcp.tool()
def resume_experiment(manifest_path: str, plan_hash: str, run_id: str) -> dict[str, Any]:
    """Verify artifacts and safely resume a failed, cancelled, or interrupted run."""
    return _service().resume_experiment(manifest_path, plan_hash=plan_hash, run_id=run_id)


@mcp.tool()
def experiment_status(manifest_path: str, run_id: str) -> dict[str, Any]:
    """Read durable state and mark a run interrupted when its worker was lost."""
    return _service().experiment_status(manifest_path, run_id)


@mcp.tool()
def experiment_events(
    manifest_path: str,
    run_id: str,
    after_sequence: int = 0,
    limit: int = 1000,
) -> dict[str, Any]:
    """Read ordered append-only run events with cursor-based pagination."""
    return _service().experiment_events(
        manifest_path,
        run_id,
        after_sequence=after_sequence,
        limit=limit,
    )


@mcp.tool()
def cancel_experiment(
    manifest_path: str,
    run_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Request cancellation; the worker terminates its owned component and cleans up."""
    return _service().cancel_experiment(manifest_path, run_id, reason=reason)


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


@mcp.resource("pgplay://run-state-schema")
def run_state_schema() -> str:
    """The durable pg_play/run-state-v2 JSON Schema."""
    path = files("pg_play").joinpath("schema/run-state-v2.schema.json")
    return path.read_text(encoding="utf-8")


@mcp.resource("pgplay://run-event-schema")
def run_event_schema() -> str:
    """The append-only pg_play/run-event-v1 JSON Schema."""
    path = files("pg_play").joinpath("schema/run-event-v1.schema.json")
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


@mcp.resource("pgplay://configuration-facts-schema")
def configuration_facts_schema() -> str:
    """The pg_diag/configuration-facts-v1 JSON Schema."""
    path = files("pg_diag").joinpath("schema/configuration-facts-v1.schema.json")
    return path.read_text(encoding="utf-8")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
