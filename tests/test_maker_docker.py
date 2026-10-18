"""Opt-in cross-process worker checks, with no PostgreSQL startup."""

import os
import time
from pathlib import Path

import pytest

from pg_play.maker import MakerClient
from pg_play.runner import ComponentRunner


def test_observe_and_cancel_real_detached_maker_worker(tmp_path):
    image = os.environ.get("PG_MAKER_DOCKER_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_DOCKER_IMAGE for the cross-process maker check")
    api = pytest.importorskip("pg_maker.api")
    from pg_maker.models import CommandSpec, EnvironmentSpec, OperationRequest
    from pg_maker.runners.docker import current_user

    service = api.MakerService(tmp_path / "operations")
    state = service.start_operation(
        OperationRequest(
            EnvironmentSpec(image, current_user()), (CommandSpec("sleep", ("sleep", "30")),)
        )
    )
    directory = service.directory / state["operation_id"]
    client = MakerClient(ComponentRunner())
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            observed = client.status(directory)
            if observed["status"] == "running":
                break
            time.sleep(0.1)
        assert observed["status"] == "running"
        events = client.events(directory)
        assert events["events"]
        client.cancel(directory)
        assert service.wait(directory, timeout_seconds=20)["status"] == "cancelled"
        assert client.status(directory)["resources"] == []
        refs = client.artifacts(directory)["artifacts"]
        assert {r["role"] for r in refs} >= {"operation_state", "operation_events"}
        exported = client.export(directory, tmp_path / "export")
        assert Path(exported["directory"]).joinpath("export-manifest.json").is_file()
    finally:
        service.cancel(directory)
