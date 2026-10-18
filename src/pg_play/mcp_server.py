"""MCP transport exposing only typed high-level pg_play operations."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from mcp.server.fastmcp import FastMCP

from pg_play.contract import ADVISORY_SEVERITIES, SUPPORTED_CONTRACT_VERSIONS
from pg_play.service import PgPlayService

mcp = FastMCP(
    "pg_play",
    instructions=(
        "Plan and run reproducible PostgreSQL experiments through pg_play. "
        "Always call validate_experiment and plan_experiment before start_experiment. "
        "Use experiment_status and experiment_events to observe a durable run. "
        "For incidents on existing servers, call plan_live_diagnostics before "
        "start_live_diagnostics and observe the detached capture through its status and events. "
        "For pg_converter packets, call plan_converter_run, review its exact targets and "
        "packet hashes, then call start_converter_run with the unchanged plan hash."
        " For PostgreSQL builds, call maker_validate, maker_prepare, maker_resolve and maker_plan, "
        "then maker_start with the reviewed hash. Observe with maker_status/maker_events and "
        "cancel through maker_cancel. DB tests default off; succeeded builds remain unverified."
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
def maker_validate(manifest: str) -> dict:
    """Validate maker configuration without preparing images or building PostgreSQL."""
    return _service().maker_validate(manifest)


@mcp.tool()
def maker_dev_open(
    directory: str,
    lock: str | None = None,
    from_run: str | None = None,
    feature: str | None = None,
    input_store: str | None = None,
    repository: str | None = None,
) -> dict:
    """Open owned Git trees from a lock or stopped build, optionally preserving Git history."""
    return _service().maker_dev_open(
        directory,
        lock=lock,
        from_run=from_run,
        feature=feature,
        input_store=input_store,
        repository=repository,
    )


@mcp.tool()
def maker_dev_status(directory: str) -> dict:
    """Show Git heads, dirty files, conflicts and immutable build iterations."""
    return _service().maker_dev_status(directory)


@mcp.tool()
def maker_dev_patch(
    directory: str,
    action: str,
    scope: str = "postgresql",
    identifier: str | None = None,
    patch: str | None = None,
    count: int = 1,
    message: str | None = None,
    onto_source: str | None = None,
    onto_lock: str | None = None,
    output: str | None = None,
) -> dict:
    """Import/commit/refresh/pop/push/rebase/continue/abort/export/adopt an owned patch series."""
    return _service().maker_dev_patch(
        directory,
        action,
        scope=scope,
        identifier=identifier,
        patch=patch,
        count=count,
        message=message,
        onto_source=onto_source,
        onto_lock=onto_lock,
        output=output,
    )


@mcp.tool()
def maker_dev_build(
    directory: str, goal: str = "build", components: list[str] | None = None
) -> dict:
    """Freeze a new iteration and start its durable worker; DB tests remain disabled."""
    return _service().maker_dev_build(directory, goal=goal, components=components)


@mcp.tool()
def maker_dev_explain(
    directory: str, goal: str = "build", components: list[str] | None = None
) -> dict:
    """Explain changed build inputs and opportunities to reuse previous results."""
    return _service().maker_dev_explain(directory, goal=goal, components=components)


@mcp.tool()
def maker_dev_exec(
    directory: str,
    argv: list[str],
    iteration: str | None = None,
    component: str | None = None,
    target: str | None = None,
    timeout_seconds: int = 3600,
) -> dict:
    """Start a diagnostic command in a private container copy of an iteration's build workspace."""
    return _service().maker_dev_exec(
        directory,
        argv,
        iteration=iteration,
        component=component,
        target=target,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def maker_dev_pin(directory: str, iteration: str, pinned: bool = True) -> dict:
    """Protect an iteration's scratch from development cleanup, or release the pin."""
    return _service().maker_dev_pin(directory, iteration, pinned=pinned)


@mcp.tool()
def maker_dev_prune(
    directory: str, keep_last: int = 2, output: str | None = None, apply: str | None = None
) -> dict:
    """Preview old iteration scratch cleanup, or apply an unchanged saved plan."""
    return _service().maker_dev_prune(directory, keep_last=keep_last, output=output, apply=apply)


@mcp.tool()
def maker_prepare(
    manifest: str,
    output: str,
    operations: str,
    input_store: str,
    operation_id: str,
    offline: bool = False,
    config_dir: str | None = None,
    features: list[str] | None = None,
    build_mode: str | None = None,
    with_doc: bool | None = None,
    doc_languages: list[str] | None = None,
) -> dict:
    """Start durable preparation of maker build/test environments."""
    return _service().maker_prepare(
        manifest,
        output,
        operations,
        input_store,
        operation_id,
        offline,
        config_dir=config_dir,
        features=features,
        build_mode=build_mode,
        with_doc=with_doc,
        doc_languages=doc_languages,
    )


@mcp.tool()
def maker_resolve(
    manifest: str,
    environments: str,
    output: str,
    operations: str,
    input_store: str,
    operation_id: str,
    offline: bool = False,
    config_dir: str | None = None,
    features: list[str] | None = None,
    build_mode: str | None = None,
    with_doc: bool | None = None,
    doc_languages: list[str] | None = None,
) -> dict:
    """Freeze sources and prepared environments in a BuildLock; observe via maker_status."""
    return _service().maker_resolve(
        manifest,
        environments,
        output,
        operations,
        input_store,
        operation_id,
        offline,
        config_dir=config_dir,
        features=features,
        build_mode=build_mode,
        with_doc=with_doc,
        doc_languages=doc_languages,
    )


@mcp.tool()
def maker_plan(
    lock: str, input_store: str | None = None, goal: str = "package", run_db_tests: bool = False
) -> dict:
    """Plan a locked build. DB tests default off; resulting packages remain unverified."""
    return _service().maker_plan(lock, input_store, goal, run_db_tests)


@mcp.tool()
def maker_start(
    plan: dict, plan_hash: str, operations: str, operation_id: str, cache_store: str | None = None
) -> dict:
    """Start an unchanged reviewed plan in maker's detached worker."""
    return _service().maker_start(plan, plan_hash, operations, operation_id, cache_store)


@mcp.tool()
def maker_status(directory: str) -> dict:
    """Read maker state and reconcile a lost worker."""
    return _service().maker_status(directory)


@mcp.tool()
def maker_events(directory: str, after_sequence: int = 0, limit: int = 100) -> dict:
    """Read ordered maker events after a durable cursor."""
    return _service().maker_events(directory, after_sequence, limit)


@mcp.tool()
def maker_cancel(directory: str) -> dict:
    """Cancel the maker worker and its owned resources, including after a client disconnect."""
    return _service().maker_cancel(directory)


@mcp.tool()
def maker_resume(
    directory: str, plan_hash: str | None = None, input_hash: str | None = None
) -> dict:
    """Resume with the original build plan hash or preparation input hash."""
    return _service().maker_resume(directory, plan_hash, input_hash)


@mcp.tool()
def maker_artifacts(directory: str) -> dict:
    """List verified artifact references, reports and qualification without copying files."""
    return _service().maker_artifacts(directory)


@mcp.tool()
def maker_export(directory: str, output: str) -> dict:
    """Export a terminal maker operation to a new directory before scratch cleanup."""
    return _service().maker_export(directory, output)


@mcp.tool()
def maker_runtime_plan(artifacts: str, base_image: str, output: str) -> dict:
    """Plan a Debian 13 stand image from exact package bytes and a pinned prepared base."""
    return _service().maker_runtime_plan(artifacts, base_image, output)


@mcp.tool()
def maker_runtime_start(
    plan: str, plan_hash: str, output: str, operations: str, operation_id: str
) -> dict:
    """Start an offline runtime image build; initialization and DB tests are not run."""
    return _service().maker_runtime_start(plan, plan_hash, output, operations, operation_id)


@mcp.tool()
def maker_changes_workspace(
    directory: str,
    input_store: str,
    source_hash: str,
    lock: str | None = None,
    feature: str | None = None,
    until: str | None = None,
) -> dict:
    """Copy a verified immutable source snapshot to a new private agent workspace."""
    return _service().maker_changes_workspace(
        directory, input_store, source_hash, lock, feature, until
    )


@mcp.tool()
def maker_changes_validate(file: str) -> dict:
    """Validate ChangeSet structure and referenced files; does not run acceptance tests."""
    return _service().maker_changes_validate(file)


@mcp.tool()
def maker_changes_export(directory: str, output: str, identifier: str, postgresql: str) -> dict:
    """Export exact text changes from a private workspace, preserving unknown ABI compatibility."""
    return _service().maker_changes_export(directory, output, identifier, postgresql)


@mcp.tool()
def plan_converter_run(
    project_directory: str,
    config_file: str,
    packet_name: str,
    database_selector: str = "ALL",
    placeholders_file: str | None = None,
    config_overrides_file: str | None = None,
    timeout_seconds: float = 3600,
) -> dict[str, Any]:
    """Plan an exact sequential pg_converter packet run without connecting."""
    return _service().plan_converter_run(
        project_directory,
        config_file,
        packet_name,
        database_selector,
        placeholders_file,
        config_overrides_file,
        timeout_seconds,
    )


@mcp.tool()
def start_converter_run(
    plan: dict[str, Any],
    plan_hash: str,
    output_directory: str,
    run_id: str,
) -> dict[str, Any]:
    """Start a detached pg_converter run from an unchanged reviewed plan."""
    return _service().start_converter_run(
        plan,
        plan_hash,
        output_directory,
        run_id,
    )


@mcp.tool()
def converter_run_status(run_directory: str) -> dict[str, Any]:
    """Read durable converter state and detect a lost worker."""
    return _service().converter_run_status(run_directory)


@mcp.tool()
def converter_run_events(
    run_directory: str,
    after_sequence: int = 0,
    limit: int = 1000,
) -> dict[str, Any]:
    """Read ordered converter-run events after the supplied cursor."""
    return _service().converter_run_events(
        run_directory,
        after_sequence=after_sequence,
        limit=limit,
    )


@mcp.tool()
def cancel_converter_run(
    run_directory: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Request cooperative cancellation of an active pg_converter packet run."""
    return _service().cancel_converter_run(run_directory, reason=reason)


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
    common_fields = [
        "contract_version",
        "component",
        "component_version",
        "command",
        "request_id",
        "status",
        "result",
        "artifacts",
        "error",
    ]
    return json.dumps(
        {
            "contract_versions": list(SUPPORTED_CONTRACT_VERSIONS),
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
            # The versions differ in one field. v1 reports `warnings`, a list of
            # sentences; v2 reports `advisories`, objects with a stable code, a
            # severity of warning/assumption/info, and the setting each is about.
            "required_fields": {
                "pg_play/component/v1": sorted([*common_fields, "warnings"]),
                "pg_play/component/v2": sorted([*common_fields, "advisories"]),
            },
            "advisory_severities": list(ADVISORY_SEVERITIES),
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
