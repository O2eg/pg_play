from __future__ import annotations

import asyncio

from pg_play.mcp_server import mcp
from pg_play.version import __version__


def test_mcp_handshake_advertises_the_pg_play_version() -> None:
    options = mcp._mcp_server.create_initialization_options()

    assert (options.server_name, options.server_version) == ("pg_play", __version__)


def test_mcp_exposes_only_high_level_typed_operations() -> None:
    tools = asyncio.run(mcp.list_tools())

    assert {tool.name for tool in tools} == {
        "maker_validate",
        "maker_init",
        "maker_config_show",
        "maker_package_names",
        "maker_extension_init",
        "maker_extension_prepare",
        "maker_extension_plan",
        "maker_extension_build",
        "maker_publish_plan",
        "maker_publish_run",
        "maker_runtime_check",
        "maker_dev_open",
        "maker_dev_status",
        "maker_dev_patch",
        "maker_dev_explain",
        "maker_dev_build",
        "maker_dev_exec",
        "maker_dev_pin",
        "maker_dev_prune",
        "maker_prepare",
        "maker_resolve",
        "maker_plan",
        "maker_start",
        "maker_status",
        "maker_events",
        "maker_cancel",
        "maker_resume",
        "maker_artifacts",
        "maker_export",
        "maker_changes_workspace",
        "maker_changes_validate",
        "maker_changes_export",
        "maker_runtime_plan",
        "maker_runtime_start",
        "component_capabilities",
        "logical_inspect",
        "logical_plan",
        "logical_start",
        "logical_status",
        "logical_events",
        "logical_stop_controller",
        "logical_resume_controller",
        "logical_report",
        "plan_converter_run",
        "start_converter_run",
        "converter_run_status",
        "converter_run_events",
        "cancel_converter_run",
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
