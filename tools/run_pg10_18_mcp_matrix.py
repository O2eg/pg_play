#!/usr/bin/env python3
"""Run the guarded PostgreSQL 10-18 pg_play MCP acceptance matrix.

The runner is deliberately resumable. Each successful phase is recorded in
``matrix-state.json`` and every MCP request/response is retained next to the
generated reports. Database storage is removed through the typed
``teardown_experiment`` MCP tool after a PostgreSQL major completes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pg_workload import initialize_project

DEV_ROOT = Path(__file__).resolve().parents[2]
PG_PLAY_ROOT = DEV_ROOT / "pg_play"
PG_STAND_ROOT = DEV_ROOT / "pg_stand"
PG_WORKLOAD_ROOT = DEV_ROOT / "pg_workload"
MCP_EXECUTABLE = PG_PLAY_ROOT / ".venv/bin/pg-play-mcp"
PROFILE_NAMES = (
    "emulate_errors",
    "imdb",
    "many_objects",
    "pagila",
    "pss_overflow",
    "simple_stock",
    "simple_stock_spec_symbols",
)
VARIANTS = (
    ("baseline-sb15", 0.15),
    ("tuned-sb20", 0.20),
    ("tuned-sb25", 0.25),
)


class MatrixError(RuntimeError):
    """The acceptance matrix cannot safely continue."""


class HostPressure(MatrixError):
    """The host crossed a critical safety threshold."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class HostGuard:
    output: Path
    cpu_limit: float = 95.0
    cpu_sustain_seconds: int = 8
    min_available_bytes: int = 4 * 1024**3
    min_available_percent: float = 5.0
    disk_used_limit: float = 97.0
    min_disk_free_bytes: int = 8 * 1024**3

    def __post_init__(self) -> None:
        self.critical = asyncio.Event()
        self.reason: str | None = None
        self._stop = asyncio.Event()
        self.samples: list[dict[str, Any]] = []
        self._high_cpu_samples = 0

    async def run(self) -> None:
        psutil.cpu_percent(interval=None)
        while not self._stop.is_set():
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            load1, load5, load15 = os.getloadavg()
            sample = {
                "timestamp": time.time(),
                "cpu_percent": psutil.cpu_percent(interval=None),
                "load_1m": load1,
                "load_5m": load5,
                "load_15m": load15,
                "memory_available_bytes": memory.available,
                "memory_available_percent": memory.available * 100.0 / memory.total,
                "disk_free_bytes": disk.free,
                "disk_used_percent": disk.percent,
            }
            self.samples.append(sample)
            with self.output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, sort_keys=True) + "\n")

            if sample["cpu_percent"] >= self.cpu_limit:
                self._high_cpu_samples += 1
            else:
                self._high_cpu_samples = 0
            reasons = []
            if self._high_cpu_samples >= self.cpu_sustain_seconds:
                reasons.append(f"CPU >= {self.cpu_limit:g}% for {self.cpu_sustain_seconds}s")
            if (
                sample["memory_available_bytes"] < self.min_available_bytes
                or sample["memory_available_percent"] < self.min_available_percent
            ):
                reasons.append("available RAM crossed the critical threshold")
            if (
                sample["disk_used_percent"] >= self.disk_used_limit
                or sample["disk_free_bytes"] < self.min_disk_free_bytes
            ):
                reasons.append("root filesystem crossed the critical threshold")
            if load1 >= (psutil.cpu_count() or 1) * 1.5:
                reasons.append("one-minute load average exceeded 1.5x logical CPUs")
            if reasons and not self.critical.is_set():
                self.reason = "; ".join(reasons)
                self.critical.set()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> dict[str, Any]:
        samples = []
        if self.output.is_file():
            for line in self.output.read_text(encoding="utf-8").splitlines():
                try:
                    samples.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not samples:
            return {"sample_count": 0, "critical_reason": self.reason}
        return {
            "sample_count": len(samples),
            "critical_reason": self.reason,
            "maximum_cpu_percent": max(item["cpu_percent"] for item in samples),
            "maximum_load_1m": max(item["load_1m"] for item in samples),
            "minimum_memory_available_bytes": min(
                item["memory_available_bytes"] for item in samples
            ),
            "minimum_disk_free_bytes": min(item["disk_free_bytes"] for item in samples),
            "maximum_disk_used_percent": max(item["disk_used_percent"] for item in samples),
        }


class McpMatrixRunner:
    def __init__(
        self,
        session: ClientSession,
        output_root: Path,
        state: dict[str, Any],
        guard: HostGuard,
    ) -> None:
        self.session = session
        self.output_root = output_root
        self.state = state
        self.state_path = output_root / "matrix-state.json"
        self.guard = guard
        self.call_index = int(state.get("call_index", 0))

    def persist(self) -> None:
        self.state["call_index"] = self.call_index
        self.state["host_guard"] = self.guard.summary()
        write_json(self.state_path, self.state)

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        label: str,
        expect_error: bool = False,
    ) -> dict[str, Any]:
        if self.guard.critical.is_set():
            raise HostPressure(self.guard.reason or "critical host pressure")
        self.call_index += 1
        record_root = self.output_root / "mcp-calls"
        request_path = record_root / f"{self.call_index:04d}-{label}-request.json"
        response_path = record_root / f"{self.call_index:04d}-{label}-response.json"
        write_json(request_path, {"tool": tool, "arguments": arguments})
        tool_task = asyncio.create_task(self.session.call_tool(tool, arguments))
        guard_task = asyncio.create_task(self.guard.critical.wait())
        done, _pending = await asyncio.wait(
            {tool_task, guard_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if guard_task in done and self.guard.critical.is_set() and not tool_task.done():
            tool_task.cancel()
            await asyncio.gather(tool_task, return_exceptions=True)
            raise HostPressure(self.guard.reason or "critical host pressure")
        guard_task.cancel()
        await asyncio.gather(guard_task, return_exceptions=True)
        result = await tool_task
        payload = result.structuredContent
        record = {
            "is_error": bool(result.isError),
            "structured_content": payload,
            "content": [block.model_dump(mode="json") for block in result.content],
        }
        write_json(response_path, record)
        self.persist()
        if result.isError and not expect_error:
            raise MatrixError(f"MCP {tool} failed: {record['content']}")
        if expect_error:
            if not result.isError:
                raise MatrixError(f"MCP {tool} unexpectedly succeeded")
            return record
        if not isinstance(payload, dict):
            raise MatrixError(f"MCP {tool} returned no structured object")
        return payload

    async def validated_run(self, manifest: Path, run_id: str, label: str) -> dict[str, Any]:
        await self.call(
            "validate_experiment",
            {"manifest_path": str(manifest)},
            label=f"{label}-validate",
        )
        plan = await self.call(
            "plan_experiment",
            {"manifest_path": str(manifest)},
            label=f"{label}-plan",
        )
        return await self.call(
            "run_experiment",
            {
                "manifest_path": str(manifest),
                "plan_hash": plan["plan_hash"],
                "run_id": run_id,
            },
            label=f"{label}-run",
        )


def stand_document(major: int, stand_project: Path) -> dict[str, Any]:
    name = f"pg-play-matrix-pg{major}"
    return {
        "api_version": "pg_stand/v1",
        "kind": "PostgreSQLStand",
        "metadata": {"name": name},
        "spec": {
            "postgres": {
                "version": major,
                "superuser": "postgres",
                "initdb_args": "--data-checksums --auth-host=scram-sha-256",
                "tls": {"enabled": False},
                "parameters": {},
            },
            "topology": {"mode": "single"},
            "docker": {
                "pull_policy": "missing",
                "network_name": f"{name}-network-pg-stand-managed",
                "labels": {"io.pg-stand.profile": "pg-play-mcp-matrix"},
            },
            "storage": {"root_directory": f".pg_stand/{name}"},
            "nodes": {
                "primary": {
                    "container_name": f"{name}-primary-pg-stand-managed",
                    "bind_address": "127.0.0.1",
                    "published_port": 55000 + major,
                    "ssh_published_port": 56000 + major,
                    "pgbouncer_session_published_port": 57000 + major,
                    "pgbouncer_transaction_published_port": 58000 + major,
                    "shm_size": "512m",
                    "cpu_limit": 1.0,
                    "memory_limit": "1g",
                }
            },
        },
    }


def experiment_document(
    *,
    major: int,
    experiment_id: str,
    artifact_root: Path,
    stand_config: Path,
    stand_project: Path,
    workload_project: Path,
    shared_buffers_part: float,
    benchmark_variant: str | None,
    benchmark_scale: float = 0.05,
) -> dict[str, Any]:
    workload_phase = benchmark_variant is None
    document: dict[str, Any] = {
        "api_version": "pg_play/v1",
        "kind": "PostgreSQLExperiment",
        "metadata": {"id": experiment_id},
        "spec": {
            "artifact_root": str(artifact_root),
            "stand": {
                "config": str(stand_config),
                "project": str(stand_project),
            },
            "configurator": {
                "inputs": {
                    "db_cpu": 1,
                    "db_ram": "1Gi",
                    "pg_version": str(major),
                    "db_duty": "mixed",
                    "shared_buffers_part": shared_buffers_part,
                    "replication_mode": "none",
                }
            },
            "workload": {
                "project": str(workload_project),
                "profiles": list(PROFILE_NAMES),
                "scale": 0.05,
                "database": f"workload_pg{major}",
                "user": f"workload_pg{major}",
                "install": True,
                "stop_after_report": True,
                "pgbench_duration_seconds": 30,
                "job_interval_seconds": 5,
                "resource_guard": {
                    "disk_max_used_pct": 97,
                    "mem_min_available_pct": 5,
                    "mem_min_available_mb": 4096,
                    "cpu_max_pct": 90,
                    "cpu_window_seconds": 10,
                    "check_interval": 2,
                },
            },
            "diagnostics": {
                "mode": "snapshots" if workload_phase else "one-shot",
                "collection_mode": "remote-db-only",
                "report_name": f"pg{major}-all-profiles-diag-30s",
            },
            "phases": {
                "benchmark": not workload_phase,
                "workload_diagnostics": workload_phase,
                "recreate_workload_database": workload_phase,
            },
        },
    }
    if workload_phase:
        document["spec"]["diagnostics"].update({"duration_seconds": 30, "interval_seconds": 5})
    else:
        report_name = f"pg{major}-{benchmark_variant}-imdb-s005-c1-2-t10"
        document["spec"]["benchmark"] = {
            "database": f"pgperf_imdb_pg{major}",
            "report_name": report_name,
            "workload_profile": "imdb",
            "workload_scale": benchmark_scale,
            "workload_duration_seconds": 10,
            "clients": [1, 2],
            "command_timeout": 180,
            "system_metrics_interval": 1,
            "drop_os_caches": False,
            "collect_pg_logs": True,
        }
    return document


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def report_artifact(state: dict[str, Any], kind: str) -> Path:
    matches = [
        Path(item["path"])
        for item in state.get("artifacts", [])
        if item.get("kind") == kind and str(item.get("path", "")).endswith(".json")
    ]
    if len(matches) != 1:
        raise MatrixError(f"expected one {kind} JSON artifact, found {matches}")
    if not matches[0].is_file():
        raise MatrixError(f"reported artifact does not exist: {matches[0]}")
    return matches[0]


def validate_benchmark_report(path: Path, major: int) -> dict[str, Any]:
    report = read_json(path, {})
    if (report.get("collection_summary") or {}).get("status") != "succeeded":
        raise MatrixError(f"benchmark report is partial: {path}")
    runs = report.get("benchmark_runs") or []
    if len(runs) != 2:
        raise MatrixError(f"benchmark report must contain two client iterations: {path}")
    sampler_errors = [
        error for run in runs for error in (run.get("system_metrics") or {}).get("errors") or []
    ]
    if sampler_errors:
        raise MatrixError(f"benchmark OS sampler errors in {path}: {sampler_errors}")
    compatibility = report.get("postgresql_compatibility") or {}
    server_major = int((compatibility.get("server") or {}).get("major", 0))
    if server_major != major:
        raise MatrixError(f"expected PostgreSQL {major}, report has {server_major}")
    evidence = report.get("workload_evidence") or {}
    roles = {item.get("role") for item in evidence.get("files") or []}
    if not {"schema", "generator", "query", "manifest"}.issubset(roles):
        raise MatrixError(f"incomplete workload source evidence in {path}: {roles}")
    return report


def maximum_tps(report: dict[str, Any]) -> float:
    values = [float(run["metrics"]["tps"]) for run in report.get("benchmark_runs") or []]
    if not values:
        raise MatrixError("benchmark report contains no TPS")
    return max(values)


def report_uses_current_environment_identity(path: Path) -> bool:
    if not path.is_file():
        return False
    report = read_json(path, {})
    dimensions = (report.get("environment_evidence") or {}).get("dimensions") or {}
    return ((dimensions.get("cpu") or {}).get("items") == ["cpu_info"]) and (
        (dimensions.get("network_hardware") or {}).get("items") == ["lshw_network"]
    )


def version_has_current_benchmarks(version_state: dict[str, Any]) -> bool:
    for variant, _ in VARIANTS:
        step = (version_state.get("benchmarks") or {}).get(variant) or {}
        report_path = Path(step.get("report", "")) if step.get("report") else Path()
        if step.get("status") != "succeeded" or not report_uses_current_environment_identity(
            report_path
        ):
            return False
    return True


def version_is_current(
    version_state: dict[str, Any],
    *,
    require_negative_scale_check: bool,
) -> bool:
    if version_state.get("status") != "succeeded" or not version_has_current_benchmarks(
        version_state
    ):
        return False

    joined = version_state.get("join") or {}
    joined_path = Path(joined.get("report", "")) if joined.get("report") else None
    if joined.get("status") != "succeeded" or not joined_path or not joined_path.is_file():
        return False

    diagnostic = version_state.get("workload_diagnostics") or {}
    diagnostic_path = Path(diagnostic.get("report", "")) if diagnostic.get("report") else None
    inspection_summary = (diagnostic.get("inspection") or {}).get("summary") or {}
    logs = [Path(path) for path in diagnostic.get("non_empty_logs") or []]
    if (
        diagnostic.get("status") != "succeeded"
        or not diagnostic_path
        or not diagnostic_path.is_file()
        or inspection_summary.get("has_errors") is not False
        or not logs
        or any(not path.is_file() or path.stat().st_size == 0 for path in logs)
    ):
        return False

    if not require_negative_scale_check:
        return True
    negative = version_state.get("changed_dataset_rejected") or {}
    changed_report = (
        Path(negative.get("changed_report", "")) if negative.get("changed_report") else None
    )
    return bool(
        negative.get("status") == "succeeded"
        and changed_report
        and changed_report.is_file()
        and (negative.get("mcp_error") or {}).get("is_error") is True
    )


def archive_obsolete_benchmark_state(version_state: dict[str, Any]) -> None:
    """Keep previous attempts as history without treating them as current runs."""
    current = {variant for variant, _shared_buffers_part in VARIANTS}
    benchmarks = version_state.get("benchmarks") or {}
    history = version_state.setdefault("historical_benchmarks", {})
    for variant in sorted(set(benchmarks) - current):
        archived = dict(benchmarks.pop(variant))
        if archived.get("status") != "succeeded":
            archived["original_status"] = archived.get("status")
            archived["status"] = "abandoned"
            archived["reason"] = "variant is not part of the safe 15/20/25 percent matrix"
        history[variant] = archived


async def run_major(
    runner: McpMatrixRunner,
    major: int,
    *,
    negative_scale_check: bool,
) -> None:
    key = str(major)
    version_state = runner.state.setdefault("versions", {}).setdefault(key, {})
    version_root = runner.output_root / f"postgresql-{major}"
    control_root = version_root / "control"
    stand_project = control_root / "stand"
    workload_project = control_root / "workload"
    artifact_root = version_root / "artifacts"
    stand_config = control_root / f"pg{major}-single.yaml"
    stand_project.mkdir(parents=True, exist_ok=True)
    write_yaml(stand_config, stand_document(major, stand_project))
    initialize_project(workload_project, force=True)

    successful_reports: list[Path] = []
    execution_hashes: list[str] = []
    benchmark_reran = False
    for variant, shared_buffers_part in VARIANTS:
        step = version_state.setdefault("benchmarks", {}).setdefault(variant, {})
        manifest = control_root / f"pg{major}-{variant}.yaml"
        experiment_id = f"pg{major}-{variant.replace('.', '-')}"
        write_yaml(
            manifest,
            experiment_document(
                major=major,
                experiment_id=experiment_id,
                artifact_root=artifact_root,
                stand_config=stand_config,
                stand_project=stand_project,
                workload_project=workload_project,
                shared_buffers_part=shared_buffers_part,
                benchmark_variant=variant,
            ),
        )
        existing = Path(step.get("report", "")) if step.get("report") else None
        reusable = bool(step.get("status") == "succeeded" and existing and existing.is_file())
        if reusable:
            reusable = report_uses_current_environment_identity(existing)
        if reusable:
            report_path = existing
            report = validate_benchmark_report(report_path, major)
        else:
            benchmark_reran = True
            attempt = int(step.get("attempt", 0)) + 1
            run_id = f"pg{major}-{variant}-imdb-s005-c1-2-t10-a{attempt}"
            step.update({"status": "running", "attempt": attempt, "run_id": run_id})
            runner.persist()
            state = await runner.validated_run(
                manifest,
                run_id,
                f"pg{major}-{variant}-a{attempt}",
            )
            if state.get("state") != "succeeded":
                raise MatrixError(f"benchmark run did not succeed: {state}")
            report_path = report_artifact(state, "BenchmarkReport")
            report = validate_benchmark_report(report_path, major)
            inspected = await runner.call(
                "inspect_benchmark_report",
                {"report_path": str(report_path)},
                label=f"pg{major}-{variant}-inspect",
            )
            step.update(
                {
                    "status": "succeeded",
                    "report": str(report_path),
                    "maximum_tps": maximum_tps(report),
                    "inspection": inspected,
                }
            )
            runner.persist()
        successful_reports.append(report_path)
        execution_hashes.append(report["workload_evidence"]["execution_hash"])
    if len(set(execution_hashes)) != 1:
        raise MatrixError(f"controlled benchmark workload hashes differ for PostgreSQL {major}")

    joined = version_state.setdefault("join", {})
    joined_path = Path(joined.get("report", "")) if joined.get("report") else None
    if benchmark_reran or not (
        joined.get("status") == "succeeded" and joined_path and joined_path.is_file()
    ):
        previous_join_exists = bool(joined_path and joined_path.is_file())
        join_attempt = int(joined.get("attempt", 1 if previous_join_exists else 0)) + 1
        join_name = f"pg{major}-imdb-s005-c1-2-t10-three-configs"
        if join_attempt > 1:
            join_name += f"-a{join_attempt}"
        joined_result = await runner.call(
            "join_benchmark_reports",
            {
                "report_paths": [str(path) for path in successful_reports],
                "join_task": "optimize-db-config",
                "output_directory": str(version_root / "joined"),
                "report_name": join_name,
            },
            label=f"pg{major}-join-three-configs",
        )
        artifacts = [
            Path(item["path"])
            for item in joined_result.get("artifacts", [])
            if item.get("kind") == "BenchmarkReport"
        ]
        if len(artifacts) != 1:
            raise MatrixError(f"JOIN returned unexpected artifacts: {joined_result}")
        joined_path = artifacts[0]
        joined_report = read_json(joined_path, {})
        sources = (joined_report.get("join_metadata") or {}).get("source_reports") or []
        if len(sources) != 3:
            raise MatrixError(f"JOIN did not retain exactly three reports: {sources}")
        await runner.call(
            "inspect_benchmark_report",
            {"report_path": str(joined_path)},
            label=f"pg{major}-join-inspect",
        )
        joined.update({"status": "succeeded", "report": str(joined_path), "attempt": join_attempt})
        runner.persist()

    if negative_scale_check and not version_state.get("changed_dataset_rejected"):
        variant = "changed-data-s006"
        manifest = control_root / f"pg{major}-{variant}.yaml"
        write_yaml(
            manifest,
            experiment_document(
                major=major,
                experiment_id=f"pg{major}-changed-data-s006",
                artifact_root=artifact_root,
                stand_config=stand_config,
                stand_project=stand_project,
                workload_project=workload_project,
                shared_buffers_part=0.25,
                benchmark_variant=variant,
                benchmark_scale=0.06,
            ),
        )
        changed_state = await runner.validated_run(
            manifest,
            f"pg{major}-changed-data-s006-c1-2-t10-a1",
            f"pg{major}-changed-data",
        )
        changed_report = report_artifact(changed_state, "BenchmarkReport")
        validate_benchmark_report(changed_report, major)
        rejection = await runner.call(
            "join_benchmark_reports",
            {
                "report_paths": [
                    str(successful_reports[0]),
                    str(successful_reports[1]),
                    str(changed_report),
                ],
                "join_task": "optimize-db-config",
                "output_directory": str(version_root / "negative-join"),
                "report_name": f"pg{major}-must-reject-changed-dataset",
            },
            label=f"pg{major}-reject-changed-dataset",
            expect_error=True,
        )
        version_state["changed_dataset_rejected"] = {
            "status": "succeeded",
            "changed_report": str(changed_report),
            "mcp_error": rejection,
        }
        runner.persist()

    workload_step = version_state.setdefault("workload_diagnostics", {})
    diagnostic_path = Path(workload_step.get("report", "")) if workload_step.get("report") else None
    workload_manifest = control_root / f"pg{major}-all-profiles-diag.yaml"
    write_yaml(
        workload_manifest,
        experiment_document(
            major=major,
            experiment_id=f"pg{major}-all-profiles-diag",
            artifact_root=artifact_root,
            stand_config=stand_config,
            stand_project=stand_project,
            workload_project=workload_project,
            shared_buffers_part=0.25,
            benchmark_variant=None,
            benchmark_scale=0.01,
        ),
    )
    previous_workload_manifest = (
        artifact_root / str(workload_step.get("run_id", "")) / "experiment.yaml"
    )
    previous_workload_document = (
        yaml.safe_load(previous_workload_manifest.read_text(encoding="utf-8"))
        if previous_workload_manifest.is_file()
        else {}
    )
    previous_workload_scale = (
        ((previous_workload_document or {}).get("spec") or {}).get("workload") or {}
    ).get("scale")
    if not (
        workload_step.get("status") == "succeeded"
        and diagnostic_path
        and diagnostic_path.is_file()
        and previous_workload_scale == 0.01
    ):
        attempt = int(workload_step.get("attempt", 0)) + 1
        run_id = f"pg{major}-all-profiles-diag-30s-a{attempt}"
        workload_step.update({"status": "running", "attempt": attempt, "run_id": run_id})
        runner.persist()
        state = await runner.validated_run(
            workload_manifest,
            run_id,
            f"pg{major}-all-profiles-diag-a{attempt}",
        )
        if state.get("state") != "succeeded":
            raise MatrixError(f"workload/diagnostic run did not succeed: {state}")
        diagnostic_path = report_artifact(state, "DiagnosticReport")
        inspection = await runner.call(
            "inspect_diagnostic_report",
            {"report_path": str(diagnostic_path)},
            label=f"pg{major}-diag-inspect",
        )
        summary = inspection.get("summary") or inspection
        if summary.get("has_errors"):
            raise MatrixError(f"pg_diag report contains collection errors: {diagnostic_path}")
        logs = sorted(
            str(path)
            for path in workload_project.rglob("*.log")
            if path.is_file() and path.stat().st_size > 0
        )
        if not logs:
            raise MatrixError("pg_workload produced no non-empty profile logs")
        workload_step.update(
            {
                "status": "succeeded",
                "report": str(diagnostic_path),
                "inspection": inspection,
                "non_empty_logs": logs,
            }
        )
        runner.persist()

    await runner.call(
        "teardown_experiment",
        {"manifest_path": str(workload_manifest), "clear_stand_data": True},
        label=f"pg{major}-teardown",
    )
    version_state["status"] = "succeeded"
    runner.persist()


async def async_main(args: argparse.Namespace) -> int:
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state = read_json(
        output_root / "matrix-state.json",
        {
            "schema_version": "pg_play/mcp-acceptance-matrix-v1",
            "started_at": time.time(),
            "versions": {},
        },
    )
    guard = HostGuard(output_root / "host-monitor.jsonl")
    guard_task = asyncio.create_task(guard.run())
    parameters = StdioServerParameters(command=str(MCP_EXECUTABLE))
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                runner = McpMatrixRunner(session, output_root, state, guard)
                capabilities = await runner.call(
                    "component_capabilities",
                    {},
                    label="component-capabilities",
                )
                write_json(output_root / "component-capabilities.json", capabilities)
                profiles = await runner.call(
                    "benchmark_profiles",
                    {},
                    label="benchmark-profiles",
                )
                write_json(output_root / "benchmark-profiles.json", profiles)
                tasks = await runner.call(
                    "benchmark_join_tasks",
                    {},
                    label="benchmark-join-tasks",
                )
                write_json(output_root / "benchmark-join-tasks.json", tasks)
                for major in args.versions:
                    version_state = state.get("versions", {}).get(str(major), {})
                    archive_obsolete_benchmark_state(version_state)
                    already_current = version_is_current(
                        version_state,
                        require_negative_scale_check=major == args.negative_scale_version,
                    )
                    if already_current:
                        continue
                    await run_major(
                        runner,
                        major,
                        negative_scale_check=major == args.negative_scale_version,
                    )
                state["status"] = "succeeded"
                state["finished_at"] = time.time()
                runner.persist()
    finally:
        guard.stop()
        await guard_task
        state["host_guard"] = guard.summary()
        write_json(output_root / "matrix-state.json", state)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PG_PLAY_ROOT / ".pg_play/mcp-pg10-18-acceptance-20260722",
    )
    parser.add_argument(
        "--versions",
        type=int,
        nargs="+",
        default=list(range(10, 19)),
        choices=range(10, 19),
    )
    parser.add_argument(
        "--negative-scale-version",
        type=int,
        default=18,
        choices=range(10, 19),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not MCP_EXECUTABLE.is_file():
        raise MatrixError(f"pg_play MCP executable is missing: {MCP_EXECUTABLE}")
    if shutil.which("docker") is None:
        raise MatrixError("docker is required")
    return asyncio.run(async_main(parse_args(argv)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MatrixError, HostPressure) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
