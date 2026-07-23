"""Typed helpers for read-only reviews of existing PostgreSQL servers."""

from __future__ import annotations

import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pg_configurator.orchestration import artifact_hash as configurator_artifact_hash
from pg_diag.configuration_facts import (
    CONFIGURATION_ITEM_IDS,
    load_configuration_facts,
)

from pg_play.contract import canonical_hash
from pg_play.state import write_json, write_text

REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
REQUIRED_TUNING_INPUTS = ("db_duty", "db_disk_type", "replication_mode", "pitr_enabled")
TUNING_ENUMS = {
    "db_duty": {"statistic", "mixed", "oltp", "financial"},
    "db_disk_type": {"SATA", "SAS", "SSD", "NVME", "NETWORK"},
    "replication_mode": {"none", "physical", "logical"},
}


class ConfigurationReviewError(ValueError):
    """A configuration review request or artifact is unsafe or incomplete."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationReviewError(f"{field} must be an object")
    return dict(value)


def _forbid_secrets(value: Any, path: str = "request") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"password", "secret", "private_key", "key_content"}:
                raise ConfigurationReviewError(
                    f"secret-bearing field {path}.{key} is forbidden; "
                    "use a file or environment reference"
                )
            _forbid_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _forbid_secrets(child, f"{path}[{index}]")


def _port(value: Any, default: int, field: str, errors: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        errors.append(f"{field} must be an integer from 1 to 65535")
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer from 1 to 65535")
        return default
    if not 1 <= port <= 65535:
        errors.append(f"{field} must be an integer from 1 to 65535")
        return default
    return port


def _path_status(value: Any, field: str, missing: list[str], errors: list[str]) -> str | None:
    if value is None or not str(value).strip():
        missing.append(field)
        return None
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        errors.append(f"{field} is not a readable file: {path}")
    return str(path)


def normalize_review_target(
    target: dict[str, Any],
    *,
    require_files: bool = True,
) -> tuple[dict[str, Any], list[str], list[str]]:
    _forbid_secrets(target, "target")
    database = _mapping(target.get("database"), "target.database")
    ssh = _mapping(target.get("ssh"), "target.ssh")
    unknown = sorted(set(target).difference({"database", "ssh"}))
    errors = [f"unknown target field: {field}" for field in unknown]
    errors.extend(
        f"unknown target.database field: {field}"
        for field in sorted(
            set(database).difference({"host", "port", "database", "user", "passfile"})
        )
    )
    errors.extend(
        f"unknown target.ssh field: {field}"
        for field in sorted(
            set(ssh).difference(
                {
                    "host",
                    "port",
                    "user",
                    "key_path",
                    "known_hosts_path",
                    "connect_timeout",
                    "key_passphrase_env",
                }
            )
        )
    )
    missing: list[str] = []

    def required_text(mapping: dict[str, Any], key: str, prefix: str) -> str | None:
        value = mapping.get(key)
        if value is None or not str(value).strip():
            missing.append(f"{prefix}.{key}")
            return None
        return str(value).strip()

    normalized_database = {
        "host": required_text(database, "host", "target.database"),
        "port": _port(database.get("port"), 5432, "target.database.port", errors),
        "database": required_text(database, "database", "target.database"),
        "user": required_text(database, "user", "target.database"),
    }
    passfile = database.get("passfile")
    if passfile is not None:
        normalized_database["passfile"] = str(Path(str(passfile)).expanduser().resolve())
        if require_files and not Path(normalized_database["passfile"]).is_file():
            errors.append(
                "target.database.passfile is not a readable file: "
                f"{normalized_database['passfile']}"
            )

    key_path = (
        _path_status(ssh.get("key_path"), "target.ssh.key_path", missing, errors)
        if require_files
        else str(ssh.get("key_path") or "").strip() or None
    )
    known_hosts_value = ssh.get("known_hosts_path", "~/.ssh/known_hosts")
    known_hosts_path = str(Path(str(known_hosts_value)).expanduser().resolve())
    if require_files and not Path(known_hosts_path).is_file():
        errors.append(f"target.ssh.known_hosts_path is not a readable file: {known_hosts_path}")
    normalized_ssh: dict[str, Any] = {
        "host": required_text(ssh, "host", "target.ssh"),
        "port": _port(ssh.get("port"), 22, "target.ssh.port", errors),
        "user": required_text(ssh, "user", "target.ssh"),
        "key_path": key_path,
        "known_hosts_path": known_hosts_path,
    }
    if ssh.get("connect_timeout") is not None:
        try:
            timeout = float(ssh["connect_timeout"])
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
            normalized_ssh["connect_timeout"] = timeout
        except (TypeError, ValueError):
            errors.append("target.ssh.connect_timeout must be a positive number")
    if ssh.get("key_passphrase_env") is not None:
        environment_name = str(ssh["key_passphrase_env"])
        normalized_ssh["key_passphrase_env"] = environment_name
        if require_files and environment_name not in os.environ:
            errors.append(
                f"target.ssh.key_passphrase_env references an unset variable: {environment_name}"
            )

    return {"database": normalized_database, "ssh": normalized_ssh}, missing, errors


def plan_configuration_review(
    target: dict[str, Any],
    tuning_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tuning = _mapping(tuning_inputs, "tuning_inputs")
    _forbid_secrets(tuning, "tuning_inputs")
    normalized_target, missing, errors = normalize_review_target(target)
    missing.extend(
        f"tuning_inputs.{name}"
        for name in REQUIRED_TUNING_INPUTS
        if name not in tuning or tuning[name] is None
    )
    for name, allowed in TUNING_ENUMS.items():
        if name in tuning and str(tuning[name]) not in allowed:
            errors.append(f"tuning_inputs.{name} must be one of: {', '.join(sorted(allowed))}")
    if "pitr_enabled" in tuning and not isinstance(tuning["pitr_enabled"], bool):
        errors.append("tuning_inputs.pitr_enabled must be boolean")
    plan = {
        "schema_version": "pg_play/configuration-review-plan-v1",
        "ready": not missing and not errors,
        "missing_inputs": sorted(set(missing)),
        "errors": errors,
        "collection": {
            "component": "pg_diag",
            "mode": "one-shot",
            "collection_mode": "remote",
            "item_ids": list(CONFIGURATION_ITEM_IDS),
        },
        "target": normalized_target,
        "tuning_inputs": tuning,
        "mutation": False,
    }
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def build_configurator_inputs(
    facts_path: str | Path,
    tuning_inputs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    facts = load_configuration_facts(facts_path)
    if not facts["collection"]["usable"]:
        unavailable = sorted(
            set(facts["collection"]["missing_item_ids"])
            | set(facts["collection"]["failed_item_ids"])
            | set(facts["collection"].get("invalid_item_ids") or [])
        )
        raise ConfigurationReviewError(
            "configuration facts are not usable; required diagnostic items unavailable: "
            + ", ".join(unavailable)
        )
    tuning = _mapping(tuning_inputs, "tuning_inputs")
    _forbid_secrets(tuning, "tuning_inputs")
    missing = [name for name in REQUIRED_TUNING_INPUTS if name not in tuning]
    if missing:
        raise ConfigurationReviewError("missing tuning inputs: " + ", ".join(missing))

    cpu_cores = facts["host"].get("cpu_cores")
    ram_bytes = facts["host"].get("ram_bytes")
    pg_major = facts["postgresql"].get("major")
    unavailable = [
        name
        for name, value in (
            ("cpu_cores", cpu_cores),
            ("ram_bytes", ram_bytes),
            ("pg_version", pg_major),
        )
        if value is None
    ]
    if unavailable:
        raise ConfigurationReviewError(
            "configuration facts lack required values: " + ", ".join(unavailable)
        )
    derived: dict[str, Any] = {
        "db_cpu": cpu_cores,
        "db_ram": f"{ram_bytes}B",
        "pg_version": pg_major,
        "platform": "LINUX",
    }
    database_size = facts["postgresql"].get("database_size_bytes")
    if database_size is not None:
        derived["db_size"] = f"{database_size}B"
    extensions = facts["postgresql"].get("available_extensions") or []
    if extensions:
        derived["available_extensions"] = ",".join(extensions)

    overrides = {
        name: {"derived": derived[name], "requested": value}
        for name, value in tuning.items()
        if name in derived and value != derived[name]
    }
    inputs = {**derived, **tuning}
    return inputs, {"facts": facts, "derived_inputs": derived, "resource_overrides": overrides}


def _normalized_text(value: Any) -> str:
    return str(value).strip().strip("'").lower()


_NUMBER_WITH_UNIT_RE = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z0-9]+)?$"
)
_BYTE_FACTORS = {
    "b": Decimal(1),
    "kb": Decimal(1024),
    "8kb": Decimal(8192),
    "mb": Decimal(1024**2),
    "gb": Decimal(1024**3),
    "tb": Decimal(1024**4),
}
_SECOND_FACTORS = {
    "us": Decimal("0.000001"),
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "min": Decimal(60),
    "h": Decimal(3600),
    "d": Decimal(86400),
}


def _candidate_normalized_value(current: dict[str, Any], candidate: Any) -> float | None:
    text = str(candidate).strip().strip("'")
    match = _NUMBER_WITH_UNIT_RE.fullmatch(text)
    if match is None:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    normalized_unit = str(current.get("normalized_unit") or "").lower()
    candidate_unit = (match.group(2) or current.get("source_unit") or "").lower()
    if normalized_unit == "bytes":
        factor = _BYTE_FACTORS.get(candidate_unit)
    elif normalized_unit == "seconds":
        factor = _SECOND_FACTORS.get(candidate_unit)
    elif normalized_unit in {"", "none"} and not candidate_unit:
        factor = Decimal(1)
    else:
        factor = None
    return float(number * factor) if factor is not None else None


def _values_equal(current: dict[str, Any], candidate: Any) -> bool:
    normalized = current.get("normalized_value")
    candidate_normalized = _candidate_normalized_value(current, candidate)
    if isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
        if candidate_normalized is not None:
            return math.isclose(float(normalized), candidate_normalized, rel_tol=1e-9, abs_tol=1e-9)
    return _normalized_text(current.get("value")) == _normalized_text(candidate)


def validate_configuration_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict) or candidate.get("schema_version") != "pg_configurator/v1":
        raise ConfigurationReviewError("candidate must be a pg_configurator/v1 artifact")
    claimed_hash = candidate.get("artifact_hash")
    if not isinstance(claimed_hash, str) or claimed_hash != configurator_artifact_hash(candidate):
        raise ConfigurationReviewError("pg_configurator candidate hash does not match its content")
    desired = candidate.get("postgresql_conf")
    details = candidate.get("parameters") or {}
    if not isinstance(desired, dict) or not isinstance(details, dict):
        raise ConfigurationReviewError("candidate artifact has invalid parameter mappings")
    return candidate


def compare_configuration(
    facts_path: str | Path,
    candidate_path: str | Path,
) -> dict[str, Any]:
    facts = load_configuration_facts(facts_path)
    candidate_source = Path(candidate_path).expanduser().resolve()
    try:
        candidate = json.loads(candidate_source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationReviewError(f"cannot read pg_configurator artifact: {exc}") from exc
    candidate = validate_configuration_candidate(candidate)
    desired = candidate.get("postgresql_conf")
    details = candidate.get("parameters") or {}
    assert isinstance(desired, dict)
    assert isinstance(details, dict)

    current_settings = facts["postgresql"]["settings"]
    rows: list[dict[str, Any]] = []
    unchanged = 0
    for name in sorted(desired):
        current = current_settings.get(name)
        detail = details.get(name) if isinstance(details.get(name), dict) else {}
        candidate_value = desired[name]
        if current is not None and _values_equal(current, candidate_value):
            unchanged += 1
            continue
        difference = None
        if current is not None:
            current_number = current.get("normalized_value")
            candidate_number = _candidate_normalized_value(current, candidate_value)
            if all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (current_number, candidate_number)
            ):
                absolute = float(candidate_number) - float(current_number)
                difference = {
                    "absolute": absolute,
                    "percent": (
                        round(absolute / float(current_number) * 100, 6)
                        if float(current_number) != 0
                        else None
                    ),
                    "unit": current.get("normalized_unit"),
                }
        rows.append(
            {
                "parameter": name,
                "current_value": current.get("value") if current else None,
                "recommended_value": candidate_value,
                "difference": difference,
                "current_source": current.get("source") if current else None,
                "context": detail.get("context") or (current or {}).get("context"),
                "apply_mode": detail.get("apply_mode", "manual"),
                "pending_restart": bool((current or {}).get("pending_restart", False)),
                "rule": detail.get("rule"),
                "reason": f"pg_configurator source: {detail.get('source', 'unknown')}",
                "status": "change" if current else "not_observed",
            }
        )

    action_counts: dict[str, int] = {}
    for row in rows:
        action = str(row["apply_mode"])
        action_counts[action] = action_counts.get(action, 0) + 1
    result: dict[str, Any] = {
        "schema_version": "pg_play/configuration-comparison-v1",
        "facts_hash": facts["facts_hash"],
        "candidate_hash": candidate["artifact_hash"],
        "rows": rows,
        "summary": {
            "changed_parameter_count": len(rows),
            "unchanged_parameter_count": unchanged,
            "apply_mode_counts": dict(sorted(action_counts.items())),
        },
        "warnings": list(candidate.get("warnings") or []),
    }
    result["comparison_hash"] = canonical_hash(result)
    return result


def _markdown_cell(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_comparison_artifacts(
    comparison: dict[str, Any],
    output_directory: str | Path,
    review_id: str,
) -> tuple[Path, Path]:
    if not REVIEW_ID_RE.fullmatch(review_id):
        raise ConfigurationReviewError("review_id contains unsupported characters")
    directory = Path(output_directory).expanduser().resolve()
    json_path = directory / f"{review_id}-configuration-comparison.json"
    markdown_path = directory / f"{review_id}-configuration-comparison.md"
    write_json(json_path, comparison)
    lines = [
        "# PostgreSQL configuration review",
        "",
        (
            "| Parameter | Current | Recommended | Current source | Context | Apply | "
            "Pending restart | Rule | Reason |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in comparison["rows"]:
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    row["parameter"],
                    row["current_value"],
                    row["recommended_value"],
                    row["current_source"],
                    row["context"],
                    row["apply_mode"],
                    row["pending_restart"],
                    row["rule"],
                    row["reason"],
                )
            )
            + " |"
        )
    write_text(markdown_path, "\n".join(lines) + "\n")
    return json_path, markdown_path
