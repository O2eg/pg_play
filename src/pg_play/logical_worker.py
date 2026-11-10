"""Detached entry point with cooperative signal handling and a parent start gate."""

from __future__ import annotations

import argparse
import signal
import time
from pathlib import Path

from pg_play.logical_runs import LogicalRunManager
from pg_play.state import write_json


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("plan_hash")
    parser.add_argument("gate", type=Path)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + 30
    while args.gate.exists():
        if time.monotonic() >= deadline:
            return 1
        time.sleep(0.05)

    def stop(signum, _frame):
        write_json(args.directory / "stop.request.json", {"signal": signum})

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    manager = LogicalRunManager()
    _, state, _ = manager._load(args.directory)
    if state["plan_hash"] != args.plan_hash:
        return 1
    result = manager.execute(args.directory)
    return 0 if result["state"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
