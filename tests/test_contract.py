from __future__ import annotations

import pytest

from pg_play.contract import (
    ContractError,
    canonical_hash,
    validate_capabilities,
    validate_envelope,
)


def _envelope() -> dict[str, object]:
    return {
        "contract_version": "pg_play/component/v1",
        "component": "pg_diag",
        "component_version": "1.0",
        "command": "summarize",
        "request_id": "request-1",
        "status": "succeeded",
        "result": {},
        "artifacts": [],
        "warnings": [],
        "error": None,
    }


def test_envelope_contract_is_exact() -> None:
    payload = _envelope()

    assert validate_envelope(payload, expected_component="pg_diag") is payload
    payload["unexpected"] = True
    with pytest.raises(ContractError, match=r"extra=\['unexpected'\]"):
        validate_envelope(payload)


def test_canonical_hash_does_not_depend_on_mapping_order() -> None:
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_capabilities_require_uniform_command_metadata() -> None:
    payload = {
        "capability_schema_version": "pg_play/capabilities/v1",
        "contract_version": "pg_play/component/v1",
        "component": "pg_perf_bench",
        "component_version": "1.0",
        "machine_interface": {
            "machine_flag": "--machine",
            "request_id_option": "--request-id",
            "capabilities_option": "--component-capabilities",
        },
        "commands": {
            "benchmark": {
                "mutates_target": True,
                "machine_output": True,
                "accepts_plan_hash": True,
            }
        },
        "exit_codes": {
            "success": 0,
            "validation_error": 2,
            "precondition_failed": 3,
            "unsupported": 4,
            "partial": 5,
            "execution_error": 6,
            "cancelled": 7,
            "ownership_error": 8,
        },
        "secret_policy": {},
    }

    assert (
        validate_capabilities(
            payload,
            expected_component="pg_perf_bench",
            required_commands={"benchmark"},
        )
        is payload
    )
    del payload["commands"]["benchmark"]["accepts_plan_hash"]
    with pytest.raises(ContractError, match="accepts_plan_hash"):
        validate_capabilities(payload, expected_component="pg_perf_bench")
