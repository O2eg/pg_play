from __future__ import annotations

import subprocess

import pytest

from pg_play.runner import ComponentExecutionError, ComponentInvocation, ComponentRunner


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
