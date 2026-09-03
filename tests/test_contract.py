from __future__ import annotations

import pytest

from pg_play.contract import (
    ContractError,
    canonical_hash,
    envelope_advisories,
    envelope_messages,
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


def _advisory(**overrides: object) -> dict[str, object]:
    advisory = {
        "code": "wal_retention_capped",
        "severity": "warning",
        "setting": "wal_keep_segments",
        "actual": "2",
        "message": "Requested WAL retention did not fit the budget.",
    }
    advisory.update(overrides)
    return advisory


def _envelope_v2() -> dict[str, object]:
    payload = _envelope()
    payload["contract_version"] = "pg_play/component/v2"
    payload["component"] = "pg_configurator"
    del payload["warnings"]
    payload["advisories"] = [_advisory()]
    return payload


def test_both_component_contracts_are_accepted() -> None:
    # Components migrate one at a time. Until the last one has, pg_play has to
    # read both, and each version's own field is the only one it may carry.
    assert validate_envelope(_envelope(), expected_component="pg_diag")
    assert validate_envelope(_envelope_v2(), expected_component="pg_configurator")


def test_a_version_may_not_borrow_the_other_findings_field() -> None:
    v1_with_advisories = _envelope()
    del v1_with_advisories["warnings"]
    v1_with_advisories["advisories"] = [_advisory()]
    with pytest.raises(ContractError, match=r"missing=\['warnings'\]"):
        validate_envelope(v1_with_advisories)

    v2_with_warnings = _envelope_v2()
    del v2_with_warnings["advisories"]
    v2_with_warnings["warnings"] = ["something"]
    with pytest.raises(ContractError, match=r"missing=\['advisories'\]"):
        validate_envelope(v2_with_warnings)


def test_an_unknown_contract_version_is_refused() -> None:
    payload = _envelope()
    payload["contract_version"] = "pg_play/component/v3"
    with pytest.raises(ContractError, match="unsupported component contract"):
        validate_envelope(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"severity": "critical"}, "unknown advisory severity"),
        ({"code": ""}, "advisory code must be a non-empty string"),
        ({"message": 7}, "advisory message must be a non-empty string"),
        ({"setting": 7}, "advisory setting must be a string or null"),
    ],
)
def test_a_malformed_advisory_is_refused(overrides: dict, message: str) -> None:
    payload = _envelope_v2()
    payload["advisories"] = [_advisory(**overrides)]
    with pytest.raises(ContractError, match=message):
        validate_envelope(payload)


def test_a_missing_advisory_field_is_refused() -> None:
    payload = _envelope_v2()
    advisory = _advisory()
    del advisory["actual"]
    payload["advisories"] = [advisory]
    with pytest.raises(ContractError, match=r"missing fields: \['actual'\]"):
        validate_envelope(payload)


def test_v1_findings_are_read_as_advisories_without_inventing_detail() -> None:
    payload = _envelope()
    payload["warnings"] = ["the disk is small"]

    assert envelope_advisories(payload) == [
        {
            "code": None,
            "severity": "warning",
            "setting": None,
            "actual": None,
            # A v1 component says only this much, and nothing more is assumed.
            "message": "the disk is small",
        }
    ]
    assert envelope_messages(payload) == ["the disk is small"]


def test_v2_findings_are_read_as_they_were_sent() -> None:
    payload = _envelope_v2()

    assert envelope_advisories(payload) == [_advisory()]
    assert envelope_messages(payload) == ["Requested WAL retention did not fit the budget."]
    # A copy: a caller that edits what it was handed must not reach the envelope.
    envelope_advisories(payload)[0]["severity"] = "info"
    assert payload["advisories"][0]["severity"] == "warning"


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
