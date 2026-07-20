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
  id: pg17-baseline
spec:
  stand:
    config: {STAND_CONFIG}
  configurator:
    inputs:
      db_cpu: 4
      db_ram: 8Gi
      pg_version: '17'
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
    text = path.read_text(encoding="utf-8").replace("      pg_version: '17'", "      password: bad")
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
