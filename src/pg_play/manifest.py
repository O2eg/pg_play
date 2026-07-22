"""Strict PostgreSQL experiment manifest."""

from __future__ import annotations

import math
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
    pgbench_duration_seconds: int | None
    job_interval_seconds: int | None
    resource_guard: ResourceGuardSpec


@dataclass(frozen=True)
class DiagnosticSpec:
    mode: str
    collection_mode: str
    duration_seconds: float
    interval_seconds: float
    report_name: str


@dataclass(frozen=True)
class BenchmarkSpec:
    database: str
    report_name: str
    benchmark_type: str
    workload_profile: str | None
    workload_scale: float
    workload_duration_seconds: int | None
    iteration_axis: str
    iterations: tuple[int, ...]
    init_command: str | None
    workload_command: str | None
    workload_path: Path | None
    pgbench_path: str | None
    psql_path: str | None
    command_timeout: float
    system_metrics_interval: float
    system_metrics_duration: float | None
    drop_os_caches: bool
    collect_pg_logs: bool


@dataclass(frozen=True)
class PhaseSpec:
    benchmark: bool
    workload_diagnostics: bool
    recreate_workload_database: bool


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
    benchmark: BenchmarkSpec | None
    phases: PhaseSpec
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
    if not math.isfinite(result) or result <= 0:
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
        {
            "artifact_root",
            "stand",
            "configurator",
            "workload",
            "diagnostics",
            "benchmark",
            "phases",
        },
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
            "pgbench_duration_seconds",
            "job_interval_seconds",
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
        pgbench_duration_seconds=(
            _integer(
                workload_raw["pgbench_duration_seconds"],
                "spec.workload.pgbench_duration_seconds",
                minimum=1,
            )
            if workload_raw.get("pgbench_duration_seconds") is not None
            else None
        ),
        job_interval_seconds=(
            _integer(
                workload_raw["job_interval_seconds"],
                "spec.workload.job_interval_seconds",
                minimum=1,
            )
            if workload_raw.get("job_interval_seconds") is not None
            else None
        ),
        resource_guard=resource_guard,
    )
    diagnostics_raw = _mapping(
        spec.get("diagnostics", {}),
        "spec.diagnostics",
        {"mode", "collection_mode", "duration_seconds", "interval_seconds", "report_name"},
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
        report_name=_text(
            diagnostics_raw.get("report_name", "report"),
            "spec.diagnostics.report_name",
        ),
    )
    if diagnostics.mode == "snapshots":
        window_error = validate_snapshots_window(
            diagnostics.duration_seconds,
            diagnostics.interval_seconds,
        )
        if window_error is not None:
            raise ManifestError(window_error)
    if not _PROFILE_RE.fullmatch(diagnostics.report_name):
        raise ManifestError(f"spec.diagnostics.report_name must match {_PROFILE_RE.pattern}")

    benchmark: BenchmarkSpec | None = None
    if spec.get("benchmark") is not None:
        benchmark_raw = _mapping(
            spec["benchmark"],
            "spec.benchmark",
            {
                "database",
                "report_name",
                "benchmark_type",
                "clients",
                "times_seconds",
                "init_command",
                "workload_command",
                "workload_path",
                "workload_profile",
                "workload_scale",
                "workload_duration_seconds",
                "pgbench_path",
                "psql_path",
                "command_timeout",
                "system_metrics_interval",
                "system_metrics_duration",
                "drop_os_caches",
                "collect_pg_logs",
            },
        )
        clients = benchmark_raw.get("clients")
        times = benchmark_raw.get("times_seconds")
        if bool(clients) == bool(times):
            raise ManifestError("spec.benchmark requires exactly one of clients or times_seconds")
        raw_iterations = clients or times
        if not isinstance(raw_iterations, list) or not raw_iterations:
            raise ManifestError("spec.benchmark iteration axis must be a non-empty list")
        iterations = tuple(
            _integer(value, "spec.benchmark iteration", minimum=1) for value in raw_iterations
        )
        workload_profile = (
            _text(benchmark_raw.get("workload_profile"), "spec.benchmark.workload_profile")
            if benchmark_raw.get("workload_profile") is not None
            else None
        )
        if workload_profile not in {None, "imdb", "pagila"}:
            raise ManifestError("spec.benchmark.workload_profile must be imdb or pagila")
        if workload_profile is not None and times:
            raise ManifestError("bundled benchmark profiles require spec.benchmark.clients")
        benchmark_type = _text(
            benchmark_raw.get(
                "benchmark_type", "custom" if workload_profile is not None else "default"
            ),
            "spec.benchmark.benchmark_type",
        )
        if benchmark_type not in {"default", "custom"}:
            raise ManifestError("spec.benchmark.benchmark_type must be default or custom")
        if workload_profile is not None and benchmark_type != "custom":
            raise ManifestError("bundled benchmark profiles use benchmark_type custom")
        workload_path = (
            _path(base, benchmark_raw["workload_path"], "spec.benchmark.workload_path")
            if benchmark_raw.get("workload_path") is not None
            else None
        )
        if workload_profile is not None and workload_path is not None:
            raise ManifestError(
                "spec.benchmark.workload_path cannot be combined with workload_profile"
            )
        if benchmark_type == "custom" and workload_path is None and workload_profile is None:
            raise ManifestError(
                "spec.benchmark.workload_path is required for benchmark_type custom"
            )
        if workload_path is not None and not workload_path.exists():
            raise ManifestError(f"benchmark workload path does not exist: {workload_path}")
        benchmark = BenchmarkSpec(
            database=_text(
                benchmark_raw.get("database", "pg_perf_bench_test"),
                "spec.benchmark.database",
            ),
            report_name=_text(
                benchmark_raw.get("report_name", "benchmark"),
                "spec.benchmark.report_name",
            ),
            benchmark_type=benchmark_type,
            workload_profile=workload_profile,
            workload_scale=_positive_float(
                benchmark_raw.get("workload_scale", 1.0),
                "spec.benchmark.workload_scale",
            ),
            workload_duration_seconds=(
                _integer(
                    benchmark_raw["workload_duration_seconds"],
                    "spec.benchmark.workload_duration_seconds",
                    minimum=1,
                )
                if benchmark_raw.get("workload_duration_seconds") is not None
                else None
            ),
            iteration_axis="pgbench_clients" if clients else "pgbench_time",
            iterations=iterations,
            init_command=(
                _text(benchmark_raw.get("init_command"), "spec.benchmark.init_command")
                if benchmark_raw.get("init_command") is not None
                else None
            ),
            workload_command=(
                _text(benchmark_raw.get("workload_command"), "spec.benchmark.workload_command")
                if benchmark_raw.get("workload_command") is not None
                else None
            ),
            workload_path=workload_path,
            pgbench_path=(
                _text(benchmark_raw["pgbench_path"], "spec.benchmark.pgbench_path")
                if benchmark_raw.get("pgbench_path") is not None
                else None
            ),
            psql_path=(
                _text(benchmark_raw["psql_path"], "spec.benchmark.psql_path")
                if benchmark_raw.get("psql_path") is not None
                else None
            ),
            command_timeout=_positive_float(
                benchmark_raw.get("command_timeout", 300),
                "spec.benchmark.command_timeout",
            ),
            system_metrics_interval=_positive_float(
                benchmark_raw.get("system_metrics_interval", 1),
                "spec.benchmark.system_metrics_interval",
            ),
            system_metrics_duration=(
                _positive_float(
                    benchmark_raw["system_metrics_duration"],
                    "spec.benchmark.system_metrics_duration",
                )
                if benchmark_raw.get("system_metrics_duration") is not None
                else None
            ),
            drop_os_caches=_boolean(
                benchmark_raw.get("drop_os_caches", False),
                "spec.benchmark.drop_os_caches",
            ),
            collect_pg_logs=_boolean(
                benchmark_raw.get("collect_pg_logs", False),
                "spec.benchmark.collect_pg_logs",
            ),
        )
        if not _PROFILE_RE.fullmatch(benchmark.report_name):
            raise ManifestError(f"spec.benchmark.report_name must match {_PROFILE_RE.pattern}")
        if workload_profile is None and (
            benchmark.init_command is None or benchmark.workload_command is None
        ):
            raise ManifestError(
                "spec.benchmark requires init_command and workload_command without workload_profile"
            )
    phases_raw = _mapping(
        spec.get("phases", {}),
        "spec.phases",
        {"benchmark", "workload_diagnostics", "recreate_workload_database"},
    )
    phases = PhaseSpec(
        benchmark=_boolean(
            phases_raw.get("benchmark", benchmark is not None),
            "spec.phases.benchmark",
        ),
        workload_diagnostics=_boolean(
            phases_raw.get("workload_diagnostics", True),
            "spec.phases.workload_diagnostics",
        ),
        recreate_workload_database=_boolean(
            phases_raw.get("recreate_workload_database", False),
            "spec.phases.recreate_workload_database",
        ),
    )
    if phases.benchmark and benchmark is None:
        raise ManifestError("spec.phases.benchmark requires spec.benchmark")
    if not phases.benchmark and not phases.workload_diagnostics:
        raise ManifestError("spec.phases must enable benchmark or workload_diagnostics")
    if phases.recreate_workload_database and not phases.workload_diagnostics:
        raise ManifestError("spec.phases.recreate_workload_database requires workload_diagnostics")

    return ExperimentManifest(
        source=source,
        experiment_id=experiment_id,
        artifact_root=artifact_root,
        stand_config=stand_config,
        stand_project=stand_project,
        configurator_inputs=dict(inputs),
        workload=workload,
        diagnostics=diagnostics,
        benchmark=benchmark,
        phases=phases,
        document_hash=canonical_hash(document),
    )
