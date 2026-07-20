from __future__ import annotations

import asyncio

from pg_play.mcp_server import mcp


def test_mcp_exposes_only_high_level_typed_operations() -> None:
    tools = asyncio.run(mcp.list_tools())

    assert {tool.name for tool in tools} == {
        "component_capabilities",
        "validate_experiment",
        "plan_experiment",
        "run_experiment",
        "experiment_status",
        "inspect_diagnostic_report",
        "compare_diagnostic_reports",
    }
    assert not {"shell", "sql", "docker", "exec"}.intersection({tool.name for tool in tools})


def test_mcp_publishes_contract_resources() -> None:
    resources = asyncio.run(mcp.list_resources())

    assert {str(resource.uri) for resource in resources} == {
        "pgplay://component-contract",
        "pgplay://experiment-schema",
    }
