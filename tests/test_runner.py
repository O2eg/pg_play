from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from pg_play.runner import (
    ComponentCancelledError,
    ComponentExecutionError,
    ComponentInvocation,
    ComponentRunner,
    process_executable,
    process_start_ticks,
    recorded_process_is_alive,
    terminate_recorded_process,
)
from pg_play.state import write_json


def test_runner_rejects_secret_bearing_arguments() -> None:
    with pytest.raises(ComponentExecutionError, match="secret-bearing"):
        ComponentRunner().run(
            ComponentInvocation(
                component="pg_stand",
                arguments=("dsn", "--show-password"),
                request_id="unsafe",
            )
        )


def test_runner_redacts_environment_secrets_from_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-leak-this"
    monkeypatch.setattr("pg_play.runner.shutil.which", lambda _name: "/bin/component")
    monkeypatch.setattr(
        "pg_play.runner.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=6,
            stdout="not-json",
            stderr=f"connection failed with {secret}",
        ),
    )

    with pytest.raises(ComponentExecutionError) as exc_info:
        ComponentRunner().run(
            ComponentInvocation(
                component="pg_workload",
                arguments=("status",),
                request_id="redaction",
                environment={"WORKLOAD_PASSWORD": secret},
            )
        )

    assert secret not in str(exc_info.value)
    assert "<redacted>" in str(exc_info.value)


def test_runner_cancels_owned_component_and_removes_active_record(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "slow-component"
    executable.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    cancel_path = tmp_path / "cancel.request.json"
    active_path = tmp_path / "active-process.json"
    monkeypatch.setattr("pg_play.runner.shutil.which", lambda _name: str(executable))
    result: list[BaseException] = []

    def run_component() -> None:
        try:
            ComponentRunner().run(
                ComponentInvocation(
                    component="pg_diag",
                    arguments=("one-shot",),
                    request_id="cancel-test",
                    cancel_path=cancel_path,
                    active_process_path=active_path,
                    cancel_grace_seconds=0.1,
                )
            )
        except BaseException as exc:  # test captures the worker-thread exception
            result.append(exc)

    thread = threading.Thread(target=run_component)
    thread.start()
    deadline = time.monotonic() + 5
    while not active_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert active_path.exists()
    write_json(cancel_path, {"requested": True})
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], ComponentCancelledError)
    assert not active_path.exists()


def test_orphan_cleanup_rejects_a_mismatched_executable(tmp_path) -> None:
    process = subprocess.Popen(
        ["/bin/sleep", "30"],
        start_new_session=True,
    )
    record = tmp_path / "active-process.json"
    try:
        write_json(
            record,
            {
                "schema_version": "pg_play/active-process-v1",
                "pid": process.pid,
                "process_start_ticks": process_start_ticks(process.pid),
                "owner_uid": os.getuid(),
                "executable": "/bin/false",
            },
        )
        with pytest.raises(ComponentExecutionError, match="mismatched identity"):
            terminate_recorded_process(record, grace_seconds=0.1)
        assert process.poll() is None
    finally:
        os.killpg(process.pid, 9)
        process.wait(timeout=5)


def test_orphan_cleanup_terminates_only_a_fully_verified_process(tmp_path) -> None:
    process = subprocess.Popen(
        ["/bin/sleep", "30"],
        start_new_session=True,
    )
    record = tmp_path / "active-process.json"
    write_json(
        record,
        {
            "schema_version": "pg_play/active-process-v1",
            "pid": process.pid,
            "process_start_ticks": process_start_ticks(process.pid),
            "owner_uid": os.getuid(),
            "executable": process_executable(process.pid),
        },
    )

    assert terminate_recorded_process(record, grace_seconds=0.1) is True
    process.wait(timeout=5)
    assert process.returncode in {-15, -9}
    assert not record.exists()


def test_zombie_process_is_not_reported_as_alive() -> None:
    process = subprocess.Popen(["/bin/sleep", "0.05"])
    record = {
        "pid": process.pid,
        "process_start_ticks": process_start_ticks(process.pid),
    }
    try:
        time.sleep(0.2)

        assert recorded_process_is_alive(record) is False
    finally:
        process.wait(timeout=5)
