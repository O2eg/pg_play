from __future__ import annotations

import asyncio

from pg_play.mcp_server import mcp


def test_mcp_exposes_only_high_level_typed_operations() -> None:
    tools = asyncio.run(mcp.list_tools())

    assert {tool.name for tool in tools} == {
        "component_capabilities",
        "plan_live_diagnostics",
        "start_live_diagnostics",
        "live_diagnostics_status",
        "live_diagnostics_events",
        "cancel_live_diagnostics",
        "plan_configuration_review",
        "collect_configuration_facts",
        "generate_configuration_candidate",
        "compare_configuration_candidate",
        "validate_experiment",
        "plan_experiment",
        "run_experiment",
        "start_experiment",
        "resume_experiment",
        "experiment_status",
        "experiment_events",
        "cancel_experiment",
        "teardown_experiment",
        "inspect_diagnostic_report",
        "compare_diagnostic_reports",
        "inspect_benchmark_report",
        "compare_benchmark_reports",
        "join_benchmark_reports",
        "benchmark_profiles",
        "benchmark_join_tasks",
    }
    assert not {"shell", "sql", "docker", "exec"}.intersection({tool.name for tool in tools})


def test_mcp_publishes_contract_resources() -> None:
    resources = asyncio.run(mcp.list_resources())

    assert {str(resource.uri) for resource in resources} == {
        "pgplay://component-contract",
        "pgplay://configuration-facts-schema",
        "pgplay://experiment-schema",
        "pgplay://run-state-schema",
        "pgplay://run-event-schema",
    }
