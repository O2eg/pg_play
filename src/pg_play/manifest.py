"""Strict PostgreSQL experiment manifest."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pg_diag.runtime_config import validate_snapshots_window

from pg_play.contract import canonical_hash

API_VERSION = "pg_play/v1"
KIND = "PostgreSQLExperiment"
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ManifestError(ValueError):
    """The experiment manifest is incomplete, unsafe, or inconsistent."""


@dataclass(frozen=True)
class ResourceGuardSpec:
    disk_max_used_pct: int
    mem_min_available_pct: int
    mem_min_available_mb: int
    cpu_max_pct: int
    cpu_window_seconds: int
    check_interval: int


@dataclass(frozen=True)
class WorkloadSpec:
    project: Path
    profiles: tuple[str, ...]
    scale: float
    database: str
    user: str
    install: bool
    stop_after_report: bool
    resource_guard: ResourceGuardSpec


@dataclass(frozen=True)
class DiagnosticSpec:
    mode: str
    collection_mode: str
    duration_seconds: float
    interval_seconds: float


@dataclass(frozen=True)
class ExperimentManifest:
    source: Path
    experiment_id: str
    artifact_root: Path
    stand_config: Path
    stand_project: Path
    configurator_inputs: dict[str, Any]
    workload: WorkloadSpec
    diagnostics: DiagnosticSpec
    document_hash: str


def _mapping(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a mapping")
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ManifestError(f"unknown {label} field(s): {', '.join(unknown)}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{label} must be boolean")
    return value


def _positive_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ManifestError(f"{label} must be a positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{label} must be a positive number") from exc
    if result <= 0:
        raise ManifestError(f"{label} must be a positive number")
    return result


def _integer(value: Any, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        limit = f" between {minimum} and {maximum}" if maximum is not None else f" >= {minimum}"
        raise ManifestError(f"{label} must be{limit}")
    return value


def _path(base: Path, value: Any, label: str) -> Path:
    text = _text(value, label)
    return (base / text).resolve() if not Path(text).is_absolute() else Path(text).resolve()


def load_manifest(path: str | Path) -> ExperimentManifest:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ManifestError(f"experiment manifest does not exist: {source}")
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read experiment manifest: {exc}") from exc
    root = _mapping(document, "document", {"api_version", "kind", "metadata", "spec"})
    if root.get("api_version") != API_VERSION:
        raise ManifestError(f"api_version must be {API_VERSION}")
    if root.get("kind") != KIND:
        raise ManifestError(f"kind must be {KIND}")
    metadata = _mapping(root.get("metadata"), "metadata", {"id"})
    experiment_id = _text(metadata.get("id"), "metadata.id")
    if not _ID_RE.fullmatch(experiment_id):
        raise ManifestError(f"metadata.id must match {_ID_RE.pattern}")
    spec = _mapping(
        root.get("spec"),
        "spec",
        {"artifact_root", "stand", "configurator", "workload", "diagnostics"},
    )
    base = source.parent
    artifact_root = _path(
        base,
        spec.get("artifact_root", f".pg_play/experiments/{experiment_id}"),
        "spec.artifact_root",
    )
    stand = _mapping(spec.get("stand"), "spec.stand", {"config", "project"})
    stand_config = _path(base, stand.get("config"), "spec.stand.config")
    if not stand_config.is_file():
        raise ManifestError(f"stand configuration does not exist: {stand_config}")
    stand_project = _path(base, stand.get("project", "."), "spec.stand.project")
    if not stand_project.is_dir():
        raise ManifestError(f"stand project directory does not exist: {stand_project}")
    configurator = _mapping(
        spec.get("configurator", {}),
        "spec.configurator",
        {"inputs"},
    )
    inputs = configurator.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ManifestError("spec.configurator.inputs must be a mapping")
    forbidden_inputs = sorted(
        key for key in inputs if any(token in str(key).lower() for token in ("password", "secret"))
    )
    if forbidden_inputs:
        raise ManifestError("secrets are forbidden in a manifest: " + ", ".join(forbidden_inputs))

    workload_raw = _mapping(
        spec.get("workload"),
        "spec.workload",
        {
            "project",
            "profiles",
            "scale",
            "database",
            "user",
            "install",
            "stop_after_report",
            "resource_guard",
        },
    )
    profiles_raw = workload_raw.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise ManifestError("spec.workload.profiles must be a non-empty list")
    profiles = tuple(_text(value, "spec.workload.profiles[]") for value in profiles_raw)
    if len(profiles) != len(set(profiles)):
        raise ManifestError("spec.workload.profiles must not contain duplicates")
    invalid_profiles = [profile for profile in profiles if not _PROFILE_RE.fullmatch(profile)]
    if invalid_profiles:
        raise ManifestError(
            "spec.workload.profiles contains invalid name(s): " + ", ".join(invalid_profiles)
        )
    workload_project = _path(base, workload_raw.get("project"), "spec.workload.project")
    if not workload_project.is_dir():
        raise ManifestError(f"workload project does not exist: {workload_project}")
    resource_guard_raw = _mapping(
        workload_raw.get("resource_guard", {}),
        "spec.workload.resource_guard",
        {
            "disk_max_used_pct",
            "mem_min_available_pct",
            "mem_min_available_mb",
            "cpu_max_pct",
            "cpu_window_seconds",
            "check_interval",
        },
    )
    resource_guard = ResourceGuardSpec(
        disk_max_used_pct=_integer(
            resource_guard_raw.get("disk_max_used_pct", 90),
            "spec.workload.resource_guard.disk_max_used_pct",
            minimum=0,
            maximum=100,
        ),
        mem_min_available_pct=_integer(
            resource_guard_raw.get("mem_min_available_pct", 10),
            "spec.workload.resource_guard.mem_min_available_pct",
            minimum=0,
            maximum=100,
        ),
        mem_min_available_mb=_integer(
            resource_guard_raw.get("mem_min_available_mb", 2048),
            "spec.workload.resource_guard.mem_min_available_mb",
            minimum=0,
        ),
        cpu_max_pct=_integer(
            resource_guard_raw.get("cpu_max_pct", 90),
            "spec.workload.resource_guard.cpu_max_pct",
            minimum=0,
            maximum=100,
        ),
        cpu_window_seconds=_integer(
            resource_guard_raw.get("cpu_window_seconds", 60),
            "spec.workload.resource_guard.cpu_window_seconds",
            minimum=1,
        ),
        check_interval=_integer(
            resource_guard_raw.get("check_interval", 5),
            "spec.workload.resource_guard.check_interval",
            minimum=1,
        ),
    )
    workload = WorkloadSpec(
        project=workload_project,
        profiles=profiles,
        scale=_positive_float(workload_raw.get("scale", 1.0), "spec.workload.scale"),
        database=_text(workload_raw.get("database", "workload_db"), "spec.workload.database"),
        user=_text(workload_raw.get("user", "workload_user"), "spec.workload.user"),
        install=_boolean(workload_raw.get("install", True), "spec.workload.install"),
        stop_after_report=_boolean(
            workload_raw.get("stop_after_report", True),
            "spec.workload.stop_after_report",
        ),
        resource_guard=resource_guard,
    )
    diagnostics_raw = _mapping(
        spec.get("diagnostics", {}),
        "spec.diagnostics",
        {"mode", "collection_mode", "duration_seconds", "interval_seconds"},
    )
    mode = _text(diagnostics_raw.get("mode", "snapshots"), "spec.diagnostics.mode")
    if mode not in {"one-shot", "snapshots"}:
        raise ManifestError("spec.diagnostics.mode must be one-shot or snapshots")
    if mode == "one-shot" and {
        "duration_seconds",
        "interval_seconds",
    }.intersection(diagnostics_raw):
        raise ManifestError(
            "spec.diagnostics duration_seconds and interval_seconds are only valid for snapshots"
        )
    collection_mode = _text(
        diagnostics_raw.get("collection_mode", "remote-db-only"),
        "spec.diagnostics.collection_mode",
    )
    if collection_mode not in {"remote-db-only", "local", "remote"}:
        raise ManifestError("spec.diagnostics.collection_mode is unsupported")
    diagnostics = DiagnosticSpec(
        mode=mode,
        collection_mode=collection_mode,
        duration_seconds=_positive_float(
            diagnostics_raw.get("duration_seconds", 30),
            "spec.diagnostics.duration_seconds",
        ),
        interval_seconds=_positive_float(
            diagnostics_raw.get("interval_seconds", 15),
            "spec.diagnostics.interval_seconds",
        ),
    )
    if diagnostics.mode == "snapshots":
        window_error = validate_snapshots_window(
            diagnostics.duration_seconds,
            diagnostics.interval_seconds,
        )
        if window_error is not None:
            raise ManifestError(window_error)
    return ExperimentManifest(
        source=source,
        experiment_id=experiment_id,
        artifact_root=artifact_root,
        stand_config=stand_config,
        stand_project=stand_project,
        configurator_inputs=dict(inputs),
        workload=workload,
        diagnostics=diagnostics,
        document_hash=canonical_hash(document),
    )
