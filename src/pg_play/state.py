"""Durable experiment state, append-only events, and artifact verification."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised on POSIX, retained for import portability
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported runtime
    fcntl = None  # type: ignore[assignment]


RUN_STATE_SCHEMA_VERSION = "pg_play/run-state-v2"
EVENT_SCHEMA_VERSION = "pg_play/run-event-v1"
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
RESUMABLE_STATES = frozenset({"failed", "cancelled", "interrupted"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(path, state)


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "not_found", "path": str(path)}
    if not isinstance(value, dict):
        raise ValueError(f"experiment state must be a JSON object: {path}")
    return value


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize control-plane mutations for one durable run."""
    if fcntl is None:  # pragma: no cover - Linux is the supported runtime
        raise RuntimeError("durable run locking requires POSIX flock support")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def append_event(
    path: Path,
    *,
    run_id: str,
    event_type: str,
    state: str | None = None,
    step: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one durable ordered event and return its public representation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            sequence = 0
            offset = 0
            truncated_torn_tail = False
            lines = stream.readlines()
            for index, line in enumerate(lines):
                if not line.strip():
                    offset += len(line)
                    continue
                try:
                    previous = json.loads(line)
                    previous_sequence = int(previous["sequence"])
                    if previous.get("schema_version") != EVENT_SCHEMA_VERSION:
                        raise ValueError("wrong schema version")
                    if previous_sequence != sequence + 1:
                        raise ValueError("non-contiguous sequence")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    is_torn_tail = index == len(lines) - 1 and not line.endswith(b"\n")
                    if is_torn_tail:
                        stream.seek(offset)
                        stream.truncate()
                        truncated_torn_tail = True
                        break
                    raise ValueError(f"experiment event log is corrupt: {path}") from exc
                sequence = previous_sequence
                offset += len(line)
            if lines and offset and not truncated_torn_tail and not lines[-1].endswith(b"\n"):
                stream.seek(0, os.SEEK_END)
                stream.write(b"\n")
            event = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "sequence": sequence + 1,
                "timestamp": utc_now(),
                "run_id": run_id,
                "type": event_type,
                "state": state,
                "step": step,
                "data": data or {},
            }
            stream.seek(0, os.SEEK_END)
            stream.write(
                (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
            return event
    except BaseException:
        # os.fdopen owns and closes descriptor after it is entered. Close it only
        # when opening the stream itself failed.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def read_events(
    path: Path,
    *,
    after_sequence: int = 0,
    limit: int = 1000,
) -> dict[str, Any]:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    events: list[dict[str, Any]] = []
    try:
        stream = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return {"events": [], "last_sequence": after_sequence, "has_more": False}
    with stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        lines = stream.readlines()
        expected_sequence = 1
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if line_number == len(lines) and not line.endswith("\n"):
                    break
                raise ValueError(
                    f"experiment event log contains invalid JSON at line {line_number}: {path}"
                ) from exc
            if not isinstance(event, dict) or event.get("schema_version") != EVENT_SCHEMA_VERSION:
                raise ValueError(f"experiment event log has an invalid event at line {line_number}")
            try:
                sequence = int(event.get("sequence", -1))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"experiment event log has an invalid sequence at line {line_number}"
                ) from exc
            if sequence != expected_sequence:
                raise ValueError(
                    f"experiment event log has a non-contiguous sequence at line {line_number}"
                )
            expected_sequence += 1
            if sequence > after_sequence:
                events.append(event)
                if len(events) > limit:
                    break
    has_more = len(events) > limit
    selected = events[:limit]
    last_sequence = int(selected[-1]["sequence"]) if selected else after_sequence
    return {
        "events": selected,
        "last_sequence": last_sequence,
        "has_more": has_more,
    }
