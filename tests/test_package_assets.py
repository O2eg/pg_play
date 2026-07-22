from __future__ import annotations

import re
from importlib.metadata import requires
from importlib.resources import files


def test_schema_and_agent_skills_are_packaged() -> None:
    package = files("pg_play")

    assert package.joinpath("schema/pg_play-v1.schema.json").is_file()
    assert package.joinpath("skills/run-postgres-experiment/SKILL.md").is_file()
    assert package.joinpath("skills/analyze-postgres-experiment/SKILL.md").is_file()


def test_distribution_metadata_requires_every_orchestrated_component() -> None:
    requirement_lines = requires("pg-play") or []
    package_names = {
        re.split(r"[<>=!~ ;\[]", requirement, maxsplit=1)[0].lower()
        for requirement in requirement_lines
    }

    assert {
        "pg-configurator",
        "pg-diag",
        "pg-perf-bench",
        "pg-stand",
        "pg-workload",
    } <= package_names
    minimum_versions = {
        "pg-configurator": ">=0.9.1",
        "pg-diag": ">=0.10.3",
        "pg-perf-bench": ">=0.2",
        "pg-stand": ">=0.2.1",
        "pg-workload": ">=0.3.0",
    }
    for package_name, minimum in minimum_versions.items():
        requirement = next(
            line for line in requirement_lines if line.lower().startswith(package_name)
        )
        assert minimum in requirement
