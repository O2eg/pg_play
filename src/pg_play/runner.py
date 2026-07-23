"""Subprocess adapter for isolated component execution."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_play.contract import ContractError, validate_envelope
from pg_play.state import read_state, utc_now, write_json

EXECUTABLES = {
    "pg_configurator": "pg-configurator",
    "pg_diag": "pg-diag",
    "pg_perf_bench": "pg-perf-bench",
    "pg_stand": "pg-stand",
    "pg_workload": "pg-workload",
}


class ComponentExecutionError(RuntimeError):
    """A component process could not produce a valid machine response."""


class ComponentCancelledError(ComponentExecutionError):
    """A component process was terminated after a durable cancellation request."""


@dataclass(frozen=True)
class ComponentInvocation:
    component: str
    arguments: tuple[str, ...]
    request_id: str
    cwd: Path | None = None
    input_document: dict[str, Any] | None = None
    environment: dict[str, str] | None = None
    timeout_seconds: float = 600.0
    cancel_path: Path | None = None
    active_process_path: Path | None = None
    cancel_grace_seconds: float = 10.0


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
        if invocation.cancel_path is not None or invocation.active_process_path is not None:
            completed = self._run_cancellable(
                invocation,
                command,
                environment,
                input_text,
            )
        else:
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

    def _run_cancellable(
        self,
        invocation: ComponentInvocation,
        command: list[str],
        environment: dict[str, str],
        input_text: str | None,
    ) -> subprocess.CompletedProcess[str]:
        if invocation.cancel_path is not None and invocation.cancel_path.exists():
            raise ComponentCancelledError(
                f"{invocation.component} was not started because cancellation was requested"
            )
        process = subprocess.Popen(
            command,
            cwd=invocation.cwd,
            env=environment,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if invocation.active_process_path is not None:
            try:
                write_json(
                    invocation.active_process_path,
                    {
                        "schema_version": "pg_play/active-process-v1",
                        "pid": process.pid,
                        "process_start_ticks": process_start_ticks(process.pid),
                        "owner_uid": os.getuid() if hasattr(os, "getuid") else None,
                        "executable": process_executable(process.pid),
                        "launcher": str(Path(command[0]).resolve()),
                        "component": invocation.component,
                        "request_id": invocation.request_id,
                        "started_at": utc_now(),
                    },
                )
            except BaseException:
                self._terminate_process_group(process, invocation.cancel_grace_seconds)
                raise
        started = time.monotonic()
        first_communicate = True
        try:
            while True:
                if invocation.cancel_path is not None and invocation.cancel_path.exists():
                    self._terminate_process_group(process, invocation.cancel_grace_seconds)
                    raise ComponentCancelledError(
                        f"{invocation.component} cancelled by experiment request"
                    )
                elapsed = time.monotonic() - started
                if elapsed >= invocation.timeout_seconds:
                    self._terminate_process_group(process, invocation.cancel_grace_seconds)
                    raise ComponentExecutionError(
                        f"{invocation.component} timed out after {invocation.timeout_seconds}s"
                    )
                try:
                    stdout, stderr = process.communicate(
                        input=input_text if first_communicate else None,
                        timeout=min(0.25, invocation.timeout_seconds - elapsed),
                    )
                    return subprocess.CompletedProcess(
                        command,
                        process.returncode,
                        stdout,
                        stderr,
                    )
                except subprocess.TimeoutExpired:
                    first_communicate = False
        finally:
            if invocation.active_process_path is not None:
                invocation.active_process_path.unlink(missing_ok=True)

    @staticmethod
    def _terminate_process_group(
        process: subprocess.Popen[str],
        grace_seconds: float,
    ) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=max(0.1, grace_seconds))
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=max(0.1, grace_seconds))

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


def process_start_ticks(pid: int) -> int | None:
    """Return the Linux process start-time field used to defend against PID reuse."""
    process = process_stat(pid)
    return process[1] if process is not None else None


def process_stat(pid: int) -> tuple[str, int] | None:
    """Return Linux process state and start time without treating zombies as live."""
    try:
        document = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing_parenthesis = document.rfind(")")
        if closing_parenthesis < 0:
            return None
        # The suffix begins with field 3 (state); starttime is field 22.
        fields = document[closing_parenthesis + 1 :].split()
        return fields[0], int(fields[19])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def process_executable(pid: int) -> str | None:
    """Return the kernel-resolved executable, including script interpreters."""
    try:
        return str(Path(f"/proc/{pid}/exe").resolve(strict=True))
    except (FileNotFoundError, OSError):
        return None


def recorded_process_is_alive(record: dict[str, Any]) -> bool:
    try:
        pid = int(record["pid"])
        expected_start = int(record["process_start_ticks"])
    except (KeyError, TypeError, ValueError):
        return False
    process = process_stat(pid)
    if process is None:
        return False
    state, actual_start = process
    return state not in {"Z", "X", "x"} and actual_start == expected_start


def terminate_recorded_process(path: Path, *, grace_seconds: float = 10.0) -> bool:
    """Terminate only the exact process recorded by pg_play, never a reused PID."""
    record = read_state(path)
    if record.get("state") == "not_found":
        return False
    if record.get("schema_version") != "pg_play/active-process-v1":
        raise ComponentExecutionError(f"invalid active process record: {path}")
    if not recorded_process_is_alive(record):
        path.unlink(missing_ok=True)
        return False
    pid = int(record["pid"])
    expected_uid = record.get("owner_uid")
    if hasattr(os, "getuid") and expected_uid != os.getuid():
        raise ComponentExecutionError("refusing to terminate a process owned by another uid")
    expected_executable = record.get("executable")
    actual_executable = process_executable(pid)
    if (
        not isinstance(expected_executable, str)
        or actual_executable is None
        or Path(actual_executable) != Path(expected_executable)
    ):
        raise ComponentExecutionError("refusing to terminate a process with mismatched identity")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + max(0.1, grace_seconds)
    while recorded_process_is_alive(record) and time.monotonic() < deadline:
        time.sleep(0.05)
    if recorded_process_is_alive(record):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    path.unlink(missing_ok=True)
    return True
