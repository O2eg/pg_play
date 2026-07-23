from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pg_configurator.orchestration import artifact_hash as configurator_artifact_hash
from pg_diag.configuration_facts import configuration_facts_hash

from pg_play.configuration_review import (
    ConfigurationReviewError,
    compare_configuration,
    plan_configuration_review,
)
from pg_play.runner import ComponentInvocation
from pg_play.service import PgPlayService


def _target(tmp_path: Path) -> dict[str, Any]:
    key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("test-key", encoding="utf-8")
    known_hosts.write_text("db.example ssh-ed25519 test", encoding="utf-8")
    return {
        "database": {
            "host": "db.example",
            "port": 5432,
            "database": "postgres",
            "user": "diag",
        },
        "ssh": {
            "host": "db.example",
            "user": "postgres",
            "key_path": str(key),
            "known_hosts_path": str(known_hosts),
        },
    }


def _tuning() -> dict[str, Any]:
    return {
        "db_duty": "mixed",
        "db_disk_type": "NVME",
        "replication_mode": "none",
        "pitr_enabled": True,
    }


def _facts(tmp_path: Path) -> Path:
    facts: dict[str, Any] = {
        "schema_version": "pg_diag/configuration-facts-v1",
        "kind": "PostgreSQLConfigurationFacts",
        "generator": {"name": "pg_diag", "version": "test"},
        "source_artifact": {
            "path": str(tmp_path / "report.json"),
            "schema_version": 4,
            "hash": "sha256:report",
        },
        "collected_at": "2026-07-23T00:00:00Z",
        "postgresql": {
            "version": "PostgreSQL 18.1",
            "version_num": 180001,
            "major": "18",
            "database_size_bytes": 1000,
            "database_sizes": [],
            "settings": {
                "shared_buffers": {
                    "value": "16384",
                    "normalized_value": 134217728,
                    "normalized_unit": "bytes",
                    "quantity": "data_volume",
                    "source_unit": "8kB",
                    "source": "configuration file",
                    "context": "postmaster",
                    "pending_restart": False,
                    "boot_value": "16384",
                    "reset_value": "16384",
                    "is_default": False,
                }
            },
            "installed_extensions": ["pg_stat_statements"],
            "available_extensions": ["pg_stat_statements", "pg_wait_sampling"],
        },
        "host": {
            "cpu_cores": 8,
            "cpu": {"logical_cores": 8},
            "ram_bytes": 17179869184,
            "filesystems": [],
            "mounts": None,
            "disks": [],
        },
        "collection": {
            "item_ids": [],
            "critical_item_ids": [],
            "missing_item_ids": [],
            "failed_item_ids": [],
            "invalid_item_ids": [],
            "usable": True,
        },
    }
    facts["facts_hash"] = configuration_facts_hash(facts)
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path


def _envelope(invocation: ComponentInvocation, result: Any, status: str = "succeeded") -> dict:
    return {
        "contract_version": "pg_play/component/v1",
        "component": invocation.component,
        "component_version": "test",
        "command": " ".join(invocation.arguments),
        "request_id": invocation.request_id,
        "status": status,
        "result": result,
        "artifacts": [],
        "warnings": [],
        "error": None,
    }


class ReviewRunner:
    def __init__(self, facts: dict[str, Any]) -> None:
        self.facts = facts
        self.invocations: list[ComponentInvocation] = []

    def run(self, invocation: ComponentInvocation) -> dict:
        self.invocations.append(invocation)
        if invocation.component == "pg_diag" and invocation.arguments[0] == "configuration-facts":
            return _envelope(invocation, self.facts)
        if invocation.component == "pg_diag":
            return _envelope(invocation, {"summary": {}})
        if invocation.component == "pg_configurator":
            artifact = {
                "schema_version": "pg_configurator/v1",
                "parameters": {
                    "shared_buffers": {
                        "value": "4GB",
                        "raw_value": 4294967296,
                        "source": "base",
                        "rule": "ram * shared_buffers_part",
                        "context": "postmaster",
                        "apply_mode": "restart",
                    }
                },
                "postgresql_conf": {"shared_buffers": "4GB"},
                "warnings": [],
            }
            artifact["artifact_hash"] = configurator_artifact_hash(artifact)
            return _envelope(
                invocation,
                {"artifact": artifact},
            )
        raise AssertionError(invocation.component)


def test_review_plan_reports_only_missing_intent_and_becomes_ready(tmp_path: Path) -> None:
    target = _target(tmp_path)

    incomplete = plan_configuration_review(target, {})
    ready = plan_configuration_review(target, _tuning())

    assert incomplete["missing_inputs"] == [
        "tuning_inputs.db_disk_type",
        "tuning_inputs.db_duty",
        "tuning_inputs.pitr_enabled",
        "tuning_inputs.replication_mode",
    ]
    assert ready["ready"] is True
    assert ready["collection"]["mode"] == "one-shot"
    assert ready["collection"]["collection_mode"] == "remote"
    assert ready["plan_hash"].startswith("sha256:")


def test_service_collects_minimal_items_and_generates_candidate(tmp_path: Path) -> None:
    facts_path = _facts(tmp_path)
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    runner = ReviewRunner(facts)
    service = PgPlayService(runner=runner)  # type: ignore[arg-type]

    collection = service.collect_configuration_facts(_target(tmp_path), tmp_path, "server-x")
    candidate = service.generate_configuration_candidate(
        facts_path, _tuning(), tmp_path, "server-x"
    )

    collect_arguments = runner.invocations[0].arguments
    assert collect_arguments[0] == "one-shot"
    item_argument = next(value for value in collect_arguments if value.startswith("--item-id="))
    assert "overview.pg_settings" in item_argument
    assert "snapshot" not in item_argument
    assert collection["facts"]["facts_hash"] == facts["facts_hash"]
    assert candidate["inputs"]["db_cpu"] == 8
    assert candidate["inputs"]["db_ram"] == "17179869184B"
    assert candidate["inputs"]["pg_version"] == "18"
    assert candidate["inputs"]["available_extensions"] == "pg_stat_statements,pg_wait_sampling"
    assert Path(candidate["candidate_path"]).is_file()


def test_comparison_contains_only_changed_parameters_and_writes_tables(tmp_path: Path) -> None:
    facts_path = _facts(tmp_path)
    candidate_path = tmp_path / "candidate.json"
    candidate = {
        "schema_version": "pg_configurator/v1",
        "parameters": {
            "shared_buffers": {
                "raw_value": 268435456,
                "source": "base",
                "rule": "ram * shared_buffers_part",
                "context": "postmaster",
                "apply_mode": "restart",
            },
            "max_connections": {
                "raw_value": 200,
                "source": "base",
                "rule": "connections",
                "context": "postmaster",
                "apply_mode": "restart",
            },
        },
        "postgresql_conf": {"shared_buffers": "256MB", "max_connections": "200"},
        "warnings": [],
    }
    candidate["artifact_hash"] = configurator_artifact_hash(candidate)
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    comparison = compare_configuration(facts_path, candidate_path)
    service_result = PgPlayService.compare_configuration_candidate(
        facts_path, candidate_path, tmp_path, "server-x"
    )

    assert [row["parameter"] for row in comparison["rows"]] == [
        "max_connections",
        "shared_buffers",
    ]
    assert comparison["rows"][0]["status"] == "not_observed"
    assert comparison["rows"][1]["difference"]["absolute"] == 134217728.0
    assert all(Path(path).is_file() for path in service_result["outputs"])
    markdown = Path(service_result["outputs"][1]).read_text(encoding="utf-8")
    assert (
        "| shared_buffers | 16384 | 256MB | configuration file | postmaster | restart |" in markdown
    )


def test_comparison_normalizes_postgresql_time_units(tmp_path: Path) -> None:
    facts_path = _facts(tmp_path)
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts["postgresql"]["settings"] = {
        "vacuum_cost_delay": {
            "value": "2",
            "normalized_value": 0.002,
            "normalized_unit": "seconds",
            "quantity": "seconds",
            "source_unit": "ms",
            "source": "default",
            "context": "user",
            "pending_restart": False,
            "boot_value": "0",
            "reset_value": "2",
            "is_default": True,
        },
        "statement_timeout": {
            "value": "1000",
            "normalized_value": 1,
            "normalized_unit": "seconds",
            "quantity": "seconds",
            "source_unit": "ms",
            "source": "configuration file",
            "context": "user",
            "pending_restart": False,
            "boot_value": "0",
            "reset_value": "1000",
            "is_default": False,
        },
    }
    facts["facts_hash"] = configuration_facts_hash(facts)
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    candidate = {
        "schema_version": "pg_configurator/v1",
        "parameters": {
            "vacuum_cost_delay": {"raw_value": 2, "apply_mode": "reload"},
            "statement_timeout": {"raw_value": 2000, "apply_mode": "reload"},
        },
        "postgresql_conf": {
            "vacuum_cost_delay": "2ms",
            "statement_timeout": "2s",
        },
        "warnings": [],
    }
    candidate["artifact_hash"] = configurator_artifact_hash(candidate)
    candidate_path = tmp_path / "time-candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    comparison = compare_configuration(facts_path, candidate_path)

    assert comparison["summary"]["unchanged_parameter_count"] == 1
    assert [row["parameter"] for row in comparison["rows"]] == ["statement_timeout"]
    assert comparison["rows"][0]["difference"] == {
        "absolute": 1.0,
        "percent": 100.0,
        "unit": "seconds",
    }


def test_comparison_rejects_tampered_candidate_hash(tmp_path: Path) -> None:
    candidate = {
        "schema_version": "pg_configurator/v1",
        "parameters": {"shared_buffers": {"raw_value": 268435456}},
        "postgresql_conf": {"shared_buffers": "256MB"},
        "warnings": [],
    }
    candidate["artifact_hash"] = configurator_artifact_hash(candidate)
    candidate["postgresql_conf"]["shared_buffers"] = "8GB"
    candidate_path = tmp_path / "tampered-candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(ConfigurationReviewError, match="hash does not match"):
        compare_configuration(_facts(tmp_path), candidate_path)
