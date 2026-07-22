from __future__ import annotations

from pg_play.service import PgPlayService


def test_installed_components_share_the_pg_play_capability_contract() -> None:
    capabilities = PgPlayService().component_capabilities()

    assert set(capabilities) == {
        "pg_configurator",
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
