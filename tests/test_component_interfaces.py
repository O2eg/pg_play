from __future__ import annotations

import json
from pathlib import Path

from pg_play.service import PgPlayService


def test_installed_components_share_the_pg_play_capability_contract() -> None:
    capabilities = PgPlayService().component_capabilities()

    assert set(capabilities) == {
        "pg_configurator",
        "pg_converter",
        "pg_diag",
        "pg_perf_bench",
        "pg_stand",
        "pg_workload",
    }
    for component, document in capabilities.items():
        assert document["component"] == component
        assert document["capability_schema_version"] == "pg_play/capabilities/v1"
        assert document["contract_version"] == "pg_play/component/v1"
        assert document["machine_interface"] == {
            "machine_flag": "--machine",
            "request_id_option": "--request-id",
            "capabilities_option": "--component-capabilities",
        }
        assert all(
            {"mutates_target", "machine_output", "accepts_plan_hash"}.issubset(metadata)
            for metadata in document["commands"].values()
        )


def test_real_pg_converter_builds_a_secret_free_reviewed_plan(tmp_path: Path) -> None:
    project = tmp_path / "converter-project"
    packet = project / "packets" / "release_test"
    packet.mkdir(parents=True)
    (packet / "meta_data.json").write_text('{"type":"default"}\n', encoding="utf-8")
    (packet / "01_release.sql").write_text("select 1;\n", encoding="utf-8")
    config = project / "conf" / "pg_converter.conf"
    config.parent.mkdir()
    config.write_text(
        "[databases]\n"
        "db_a = postgresql://deployer:do-not-leak@db.example:5432/app\n"
        "\n"
        "[main]\n"
        "schema_location = pgc\n",
        encoding="utf-8",
    )

    plan = PgPlayService().plan_converter_run(
        project,
        str(config),
        "release_test",
        "db_a",
        timeout_seconds=120,
    )

    assert plan["schema_version"] == "pg_play/converter-run-plan-v1"
    assert plan["component_plan"]["target"]["database_aliases"] == ["db_a"]
    assert plan["component_plan"]["target"]["database_connections"]["db_a"]["host"] == (
        "db.example"
    )
    assert plan["component_plan"]["packet"]["step_names"] == ["01_release.sql"]
    assert plan["component_plan"]["safety"]["machine_sql_logging"] is False
    assert "do-not-leak" not in json.dumps(plan)
