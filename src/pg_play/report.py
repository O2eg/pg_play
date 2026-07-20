"""Deterministic pg_diag report inspection and comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pg_diag.orchestration import load_artifact, summarize_artifact


def inspect_report(path: str | Path) -> dict[str, Any]:
    report_path = Path(path).expanduser().resolve()
    return {"path": str(report_path), "summary": summarize_artifact(load_artifact(report_path))}


def compare_reports(baseline: str | Path, candidate: str | Path) -> dict[str, Any]:
    baseline_result = inspect_report(baseline)
    candidate_result = inspect_report(candidate)
    left = baseline_result["summary"]
    right = candidate_result["summary"]
    collection_keys = sorted(set(left["collection_statuses"]) | set(right["collection_statuses"]))
    severity_keys = sorted(set(left["severity_levels"]) | set(right["severity_levels"]))
    comparability_fields = {
        "artifact_schema_version": (
            left.get("artifact_schema_version"),
            right.get("artifact_schema_version"),
        ),
        "content_checksum": (
            left.get("content", {}).get("checksum"),
            right.get("content", {}).get("checksum"),
        ),
        "report_id": (
            left.get("content", {}).get("report_id"),
            right.get("content", {}).get("report_id"),
        ),
        "server_version_num": (
            (left.get("runtime") or {}).get("server_version_num"),
            (right.get("runtime") or {}).get("server_version_num"),
        ),
        "mode": (
            (left.get("runtime") or {}).get("mode"),
            (right.get("runtime") or {}).get("mode"),
        ),
        "collection_mode": (
            (left.get("runtime") or {}).get("collection_mode"),
            (right.get("runtime") or {}).get("collection_mode"),
        ),
    }
    mismatches = {
        name: {"baseline": values[0], "candidate": values[1]}
        for name, values in comparability_fields.items()
        if values[0] != values[1]
    }
    return {
        "schema_version": "pg_play/comparison-v1",
        "baseline": baseline_result,
        "candidate": candidate_result,
        "comparability": {
            "comparable": not mismatches,
            "mismatches": mismatches,
            "note": (
                "Workload profile, scale, seed, configuration, topology, and host identity "
                "must also be checked from pg_play run state."
            ),
        },
        "delta": {
            "item_count": right["item_count"] - left["item_count"],
            "snapshot_count": right["snapshot_count"] - left["snapshot_count"],
            "diagnostic_count": right["diagnostic_count"] - left["diagnostic_count"],
            "completeness_ratio": round(
                right["completeness"]["ratio"] - left["completeness"]["ratio"],
                6,
            ),
            "collection_statuses": {
                key: right["collection_statuses"].get(key, 0)
                - left["collection_statuses"].get(key, 0)
                for key in collection_keys
            },
            "severity_levels": {
                key: right["severity_levels"].get(key, 0) - left["severity_levels"].get(key, 0)
                for key in severity_keys
            },
        },
    }
