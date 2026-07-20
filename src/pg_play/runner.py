"""Subprocess adapter for isolated component execution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_play.contract import ContractError, validate_envelope

EXECUTABLES = {
    "pg_configurator": "pg-configurator",
    "pg_diag": "pg-diag",
    "pg_stand": "pg-stand",
    "pg_workload": "pg-workload",
}


class ComponentExecutionError(RuntimeError):
    """A component process could not produce a valid machine response."""


@dataclass(frozen=True)
class ComponentInvocation:
    component: str
    arguments: tuple[str, ...]
    request_id: str
    cwd: Path | None = None
    input_document: dict[str, Any] | None = None
    environment: dict[str, str] | None = None
    timeout_seconds: float = 600.0


class ComponentRunner:
    def run(self, invocation: ComponentInvocation) -> dict[str, Any]:
        executable_name = EXECUTABLES.get(invocation.component)
        if executable_name is None:
            raise ComponentExecutionError(f"unsupported component: {invocation.component}")
        self._validate_arguments(invocation.arguments)
        executable = shutil.which(executable_name)
        if executable is None:
            sibling = Path(sys.executable).absolute().with_name(executable_name)
            executable = str(sibling) if sibling.is_file() else None
        if executable is None:
            raise ComponentExecutionError(
                f"component executable is not installed: {executable_name}"
            )
        command = [
            executable,
            "--machine",
            "--request-id",
            invocation.request_id,
            *invocation.arguments,
        ]
        input_text = (
            json.dumps(invocation.input_document, sort_keys=True)
            if invocation.input_document is not None
            else None
        )
        environment = os.environ.copy()
        environment.update(invocation.environment or {})
        try:
            completed = subprocess.run(
                command,
                cwd=invocation.cwd,
                env=environment,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=invocation.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ComponentExecutionError(
                f"{invocation.component} timed out after {invocation.timeout_seconds}s"
            ) from exc
        try:
            payload = json.loads(completed.stdout)
            return validate_envelope(payload, expected_component=invocation.component)
        except (json.JSONDecodeError, ContractError) as exc:
            detail = self._safe_stderr(
                completed.stderr,
                secrets=tuple((invocation.environment or {}).values()),
            )
            raise ComponentExecutionError(
                f"{invocation.component} returned invalid machine output"
                + (f": {detail}" if detail else "")
            ) from exc

    @staticmethod
    def _validate_arguments(arguments: tuple[str, ...]) -> None:
        forbidden = {"--password", "--show-password"}
        for argument in arguments:
            option = argument.partition("=")[0]
            if option in forbidden:
                raise ComponentExecutionError(f"secret-bearing argument is forbidden: {option}")

    @staticmethod
    def _safe_stderr(value: str, *, secrets: tuple[str, ...] = ()) -> str:
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<redacted>")
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return " | ".join(lines[-3:])[:600]
