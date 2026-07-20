"""Atomic experiment state with explicit step transitions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


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
    write_json(path, state)


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "not_found", "path": str(path)}
    if not isinstance(value, dict):
        raise ValueError(f"experiment state must be a JSON object: {path}")
    return value
