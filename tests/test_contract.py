from __future__ import annotations

import pytest

from pg_play.contract import ContractError, canonical_hash, validate_envelope


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
