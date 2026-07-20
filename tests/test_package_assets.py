from __future__ import annotations

from importlib.resources import files


def test_schema_and_agent_skills_are_packaged() -> None:
    package = files("pg_play")

    assert package.joinpath("schema/pg_play-v1.schema.json").is_file()
    assert package.joinpath("skills/run-postgres-experiment/SKILL.md").is_file()
    assert package.joinpath("skills/analyze-postgres-experiment/SKILL.md").is_file()
