from __future__ import annotations

from importlib.metadata import metadata, requires
from importlib.resources import files

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def test_schema_and_agent_skills_are_packaged() -> None:
    package = files("pg_play")

    assert package.joinpath("assets/maker-entrypoint.py").is_file()
    assert package.joinpath("schema/pg_play-v1.schema.json").is_file()
    assert package.joinpath("schema/run-state-v2.schema.json").is_file()
    assert package.joinpath("schema/run-event-v1.schema.json").is_file()
    assert package.joinpath("skills/run-postgres-experiment/SKILL.md").is_file()
    assert package.joinpath("skills/recover-postgres-experiment/SKILL.md").is_file()
    assert package.joinpath("skills/diagnose-live-postgres/SKILL.md").is_file()
    assert package.joinpath("skills/run-pg-converter-packet/SKILL.md").is_file()
    assert package.joinpath("skills/review-postgres-configuration/SKILL.md").is_file()
    assert package.joinpath("skills/analyze-postgres-experiment/SKILL.md").is_file()
    assert package.joinpath(
        "skills/analyze-postgres-experiment/references/benchmark-joins.md"
    ).is_file()


def test_distribution_metadata_requires_every_orchestrated_component() -> None:
    requirement_lines = requires("pg-play") or []
    requirements = [Requirement(line) for line in requirement_lines]
    package_names = {item.name for item in requirements if item.marker is None}

    assert {
        "pg-configurator",
        "pg-converter",
        "pg-diag",
        "pg-perf-bench",
        "pg-stand",
        "pg-workload",
        "pg-maker",
        "pg-logical-repl",
    } <= package_names
    minimum_versions = {
        "pg-configurator": ">=0.11.1",
        "pg-converter": ">=2.0b2",
        "pg-diag": ">=0.19.1",
        "pg-logical-repl": ">=0.2.0",
        "pg-maker": ">=0.6.0",
        "pg-perf-bench": ">=0.7.4",
        "pg-stand": ">=0.3.0",
        "pg-workload": ">=0.6.1",
    }
    for package_name, minimum in minimum_versions.items():
        requirement = next(
            line for line in requirement_lines if line.lower().startswith(package_name)
        )
        assert minimum in requirement


def test_distribution_declares_a_compatible_python_minimum() -> None:
    supported = SpecifierSet(metadata("pg-play")["Requires-Python"])
    assert "3.10" not in supported
    assert "3.11" in supported
    assert "3.12" in supported


def test_distribution_has_no_maker_or_logical_extras() -> None:
    extras = set(metadata("pg-play").get_all("Provides-Extra") or [])
    assert not extras.intersection({"maker", "logical"})
