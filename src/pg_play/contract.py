"""Shared pg_play component envelope validation and hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Two component contracts are live. They differ in one field: v1 reports
# `warnings`, a list of sentences, and v2 reports `advisories`, a list of
# objects carrying a stable code, a severity and the setting each one is about.
# Components migrate one at a time, so both are accepted and `envelope_advisories`
# below gives a caller one shape to work with either way.
CONTRACT_VERSION = "pg_play/component/v1"
CONTRACT_VERSION_V2 = "pg_play/component/v2"
SUPPORTED_CONTRACT_VERSIONS = (CONTRACT_VERSION, CONTRACT_VERSION_V2)
ADVISORY_SEVERITIES = ("warning", "assumption", "info")
CAPABILITY_SCHEMA_VERSION = "pg_play/capabilities/v1"
MACHINE_INTERFACE = {
    "machine_flag": "--machine",
    "request_id_option": "--request-id",
    "capabilities_option": "--component-capabilities",
}
EXIT_CODES = {
    "success": 0,
    "validation_error": 2,
    "precondition_failed": 3,
    "unsupported": 4,
    "partial": 5,
    "execution_error": 6,
    "cancelled": 7,
    "ownership_error": 8,
}
COMPONENTS = {
    "pg_configurator",
    "pg_converter",
    "pg_diag",
    "pg_perf_bench",
    "pg_stand",
    "pg_workload",
}
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
    contract_version = value.get("contract_version")
    if contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise ContractError(f"unsupported component contract: {contract_version!r}")
    findings_field = "advisories" if contract_version == CONTRACT_VERSION_V2 else "warnings"
    required = {
        "contract_version",
        "component",
        "component_version",
        "command",
        "request_id",
        "status",
        "result",
        "artifacts",
        findings_field,
        "error",
    }
    if set(value) != required:
        missing = sorted(required.difference(value))
        extra = sorted(set(value).difference(required))
        raise ContractError(f"invalid component envelope fields: missing={missing}, extra={extra}")
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
    if findings_field == "warnings":
        if not isinstance(value["warnings"], list) or not all(
            isinstance(warning, str) for warning in value["warnings"]
        ):
            raise ContractError("component warnings must be a list of strings")
    else:
        _validate_advisories(value["advisories"])
    if value["error"] is not None and not isinstance(value["error"], dict):
        raise ContractError("component error must be an object or null")
    if isinstance(value["error"], dict) and not {
        "code",
        "message",
    }.issubset(value["error"]):
        raise ContractError("component error must contain code and message")
    return value


def _validate_advisories(advisories: Any) -> None:
    if not isinstance(advisories, list):
        raise ContractError("component advisories must be a list of objects")
    for advisory in advisories:
        if not isinstance(advisory, dict):
            raise ContractError("component advisories must be a list of objects")
        missing = sorted({"code", "severity", "setting", "actual", "message"}.difference(advisory))
        if missing:
            raise ContractError(f"component advisory missing fields: {missing}")
        if advisory["severity"] not in ADVISORY_SEVERITIES:
            raise ContractError(f"unknown advisory severity: {advisory['severity']!r}")
        for field in ("code", "message"):
            if not isinstance(advisory[field], str) or not advisory[field]:
                raise ContractError(f"component advisory {field} must be a non-empty string")
        for field in ("setting", "actual"):
            if advisory[field] is not None and not isinstance(advisory[field], str):
                raise ContractError(f"component advisory {field} must be a string or null")


def envelope_advisories(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings in an envelope, in the v2 shape whichever version sent it.

    A v1 component reports sentences with no severity and no stable identifier,
    so each one is lifted to `severity: "warning"`: that is the only thing v1
    can express, and pretending to know more would be an invention. `code` stays
    None, which is how a caller tells a lifted line from a real advisory.
    """
    if "advisories" in envelope:
        return [dict(advisory) for advisory in envelope["advisories"]]
    return [
        {
            "code": None,
            "severity": "warning",
            "setting": None,
            "actual": None,
            "message": message,
        }
        for message in envelope.get("warnings") or []
    ]


def envelope_messages(envelope: dict[str, Any]) -> list[str]:
    """Just the sentences, for a report that has no room for the structure."""
    return [advisory["message"] for advisory in envelope_advisories(envelope)]


def validate_capabilities(
    value: Any,
    *,
    expected_component: str,
    required_commands: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("component capabilities must be a JSON object")
    required_fields = {
        "capability_schema_version",
        "contract_version",
        "component",
        "component_version",
        "commands",
        "exit_codes",
        "machine_interface",
        "secret_policy",
    }
    missing = sorted(required_fields.difference(value))
    if missing:
        raise ContractError(f"component capabilities missing fields: {missing}")
    if value["capability_schema_version"] != CAPABILITY_SCHEMA_VERSION:
        raise ContractError(
            f"unsupported capability schema: {value['capability_schema_version']!r}"
        )
    if value["contract_version"] not in SUPPORTED_CONTRACT_VERSIONS:
        raise ContractError(f"unsupported component contract: {value['contract_version']!r}")
    if value["component"] != expected_component:
        raise ContractError(
            f"expected capabilities for {expected_component}, received {value['component']}"
        )
    if value["machine_interface"] != MACHINE_INTERFACE:
        raise ContractError("component machine_interface must match the pg_play contract")
    if not isinstance(value["component_version"], str) or not value["component_version"]:
        raise ContractError("component capability version must be a non-empty string")
    commands = value["commands"]
    if not isinstance(commands, dict):
        raise ContractError("component capability commands must be an object")
    missing_commands = sorted((required_commands or set()).difference(commands))
    if missing_commands:
        raise ContractError(f"component capabilities missing commands: {missing_commands}")
    for command, metadata in commands.items():
        if not isinstance(command, str) or not isinstance(metadata, dict):
            raise ContractError("component command capabilities must map names to objects")
        for field in ("mutates_target", "machine_output", "accepts_plan_hash"):
            if not isinstance(metadata.get(field), bool):
                raise ContractError(f"component command {command!r} must declare boolean {field}")
        if metadata["mutates_target"] and not metadata["machine_output"]:
            raise ContractError(f"mutating component command {command!r} lacks machine output")
    if value["exit_codes"] != EXIT_CODES:
        raise ContractError("component exit_codes must match the pg_play capability contract")
    if not isinstance(value["secret_policy"], dict):
        raise ContractError("component secret_policy must be an object")
    return value
