"""Shared pg_play component envelope validation and hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTRACT_VERSION = "pg_play/component/v1"
COMPONENTS = {"pg_configurator", "pg_diag", "pg_stand", "pg_workload"}
STATUSES = {
    "planned",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "skipped",
    "blocked",
}


class ContractError(RuntimeError):
    """A component returned output outside the versioned contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_envelope(value: Any, *, expected_component: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("component response must be a JSON object")
    required = {
        "contract_version",
        "component",
        "component_version",
        "command",
        "request_id",
        "status",
        "result",
        "artifacts",
        "warnings",
        "error",
    }
    if set(value) != required:
        missing = sorted(required.difference(value))
        extra = sorted(set(value).difference(required))
        raise ContractError(f"invalid component envelope fields: missing={missing}, extra={extra}")
    if value["contract_version"] != CONTRACT_VERSION:
        raise ContractError(f"unsupported component contract: {value['contract_version']!r}")
    component = value["component"]
    if component not in COMPONENTS:
        raise ContractError(f"unknown component in envelope: {component!r}")
    if expected_component is not None and component != expected_component:
        raise ContractError(f"expected {expected_component}, received {component}")
    if not isinstance(value["component_version"], str) or not value["component_version"]:
        raise ContractError("component_version must be a non-empty string")
    if not isinstance(value["command"], str) or not value["command"]:
        raise ContractError("command must be a non-empty string")
    if value["request_id"] is not None and not isinstance(value["request_id"], str):
        raise ContractError("request_id must be a string or null")
    if value["status"] not in STATUSES:
        raise ContractError(f"unsupported component status: {value['status']!r}")
    if not isinstance(value["artifacts"], list) or not all(
        isinstance(artifact, dict) for artifact in value["artifacts"]
    ):
        raise ContractError("component artifacts must be a list of objects")
    if not isinstance(value["warnings"], list) or not all(
        isinstance(warning, str) for warning in value["warnings"]
    ):
        raise ContractError("component warnings must be a list of strings")
    if value["error"] is not None and not isinstance(value["error"], dict):
        raise ContractError("component error must be an object or null")
    if isinstance(value["error"], dict) and not {
        "code",
        "message",
    }.issubset(value["error"]):
        raise ContractError("component error must contain code and message")
    return value
