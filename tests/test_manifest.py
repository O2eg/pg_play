from __future__ import annotations

from pathlib import Path

import pytest

from pg_play.manifest import ManifestError, load_manifest

STAND_CONFIG = Path("/home/oleg/Desktop/dev/pg_stand/src/pg_stand/configs/single.yaml")


def _write_manifest(tmp_path: Path, diagnostics: str = "") -> Path:
    workload = tmp_path / "workload"
    workload.mkdir()
    manifest = tmp_path / "experiment.yaml"
    manifest.write_text(
        f"""api_version: pg_play/v1
kind: PostgreSQLExperiment
metadata:
  id: pg18-baseline
spec:
  stand:
    config: {STAND_CONFIG}
  configurator:
    inputs:
      db_cpu: 4
      db_ram: 8Gi
      pg_version: '18'
  workload:
    project: workload
    profiles: [simple]
{diagnostics}
""",
        encoding="utf-8",
    )
    return manifest


def test_manifest_resolves_paths_and_has_stable_hash(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """  diagnostics:
    mode: snapshots
    duration_seconds: 60
    interval_seconds: 10
""",
    )

    first = load_manifest(path)
    second = load_manifest(path)

    assert first.workload.project == (tmp_path / "workload").resolve()
    assert first.stand_project == tmp_path.resolve()
    assert first.workload.resource_guard.disk_max_used_pct == 90
    assert first.document_hash == second.document_hash


def test_manifest_rejects_invalid_snapshot_window(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """  diagnostics:
    mode: snapshots
    duration_seconds: 20
    interval_seconds: 10
""",
    )

    with pytest.raises(ManifestError, match="duration-seconds between 30 and 86400"):
        load_manifest(path)


def test_manifest_rejects_snapshot_options_for_one_shot(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """  diagnostics:
    mode: one-shot
    duration_seconds: 60
""",
    )

    with pytest.raises(ManifestError, match="only valid for snapshots"):
        load_manifest(path)


def test_manifest_rejects_secret_fields(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    text = path.read_text(encoding="utf-8").replace("      pg_version: '18'", "      password: bad")
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ManifestError, match="secrets are forbidden"):
        load_manifest(path)


def test_manifest_rejects_invalid_resource_guard_threshold(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    profiles: [simple]",
            "    profiles: [simple]\n    resource_guard:\n      disk_max_used_pct: 101",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="disk_max_used_pct must be between 0 and 100"):
        load_manifest(path)


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_manifest_rejects_non_finite_positive_numbers(tmp_path: Path, value: str) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    profiles: [simple]",
            f"    profiles: [simple]\n    scale: {value}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="must be a positive number"):
        load_manifest(path)


def test_manifest_parses_optional_benchmark(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """  benchmark:
    database: disposable_benchmark
    clients: [1, 8]
    init_command: pgbench -i
    workload_command: pgbench -T 60
""",
        encoding="utf-8",
    )

    benchmark = load_manifest(path).benchmark

    assert benchmark is not None
    assert benchmark.database == "disposable_benchmark"
    assert benchmark.iteration_axis == "pgbench_clients"
    assert benchmark.iterations == (1, 8)


def test_manifest_rejects_ambiguous_benchmark_axis(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """  benchmark:
    clients: [1]
    times_seconds: [30]
    init_command: pgbench -i
    workload_command: pgbench -T 30
""",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="exactly one"):
        load_manifest(path)


def test_manifest_parses_bundled_benchmark_profile_without_command_templates(
    tmp_path: Path,
) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """  benchmark:
    database: disposable_benchmark
    workload_profile: imdb
    workload_scale: 0.5
    clients: [1, 8]
    system_metrics_interval: 0.5
    system_metrics_duration: 10
    workload_duration_seconds: 10
""",
        encoding="utf-8",
    )

    benchmark = load_manifest(path).benchmark

    assert benchmark is not None
    assert benchmark.benchmark_type == "custom"
    assert benchmark.workload_profile == "imdb"
    assert benchmark.workload_scale == 0.5
    assert benchmark.pgbench_path is None
    assert benchmark.psql_path is None
    assert benchmark.system_metrics_interval == 0.5
    assert benchmark.system_metrics_duration == 10
    assert benchmark.workload_duration_seconds == 10
    assert benchmark.init_command is None
    assert benchmark.workload_command is None


def test_manifest_rejects_time_axis_for_bundled_benchmark_profile(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """  benchmark:
    workload_profile: pagila
    times_seconds: [30]
""",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="require spec.benchmark.clients"):
        load_manifest(path)


def test_manifest_parses_benchmark_only_and_reviewed_workload_cadence(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "    profiles: [simple]",
        "    profiles: [simple]\n    pgbench_duration_seconds: 30\n    job_interval_seconds: 5",
    )
    text += """  benchmark:
    workload_profile: imdb
    clients: [2]
  phases:
    benchmark: true
    workload_diagnostics: false
"""
    path.write_text(text, encoding="utf-8")

    manifest = load_manifest(path)

    assert manifest.phases.benchmark is True
    assert manifest.phases.workload_diagnostics is False
    assert manifest.workload.pgbench_duration_seconds == 30
    assert manifest.workload.job_interval_seconds == 5


def test_manifest_rejects_database_recreation_without_workload_phase(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """  benchmark:
    workload_profile: imdb
    clients: [2]
  phases:
    benchmark: true
    workload_diagnostics: false
    recreate_workload_database: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="requires workload_diagnostics"):
        load_manifest(path)
