"""Detached worker for one immutable reviewed pg_converter packet run."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from pg_play.converter_runs import (
    STATE_SCHEMA_VERSION,
    ConverterRunManager,
    _context,
    _event,
)
from pg_play.state import read_state, utc_now, write_json, write_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pg_play.converter_run_worker")
    parser.add_argument("--run-directory", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--start-gate", required=True)
    return parser


def _wait_for_parent(gate: Path, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while gate.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _wait_for_parent(Path(args.start_gate))
    context = _context(args.run_directory)

    def request_cancel(signum: int, _frame: object) -> None:
        if not context.cancel_path.exists():
            write_json(
                context.cancel_path,
                {
                    "schema_version": "pg_play/cancel-request-v1",
                    "run_id": context.run_id,
                    "requested_at": utc_now(),
                    "reason": f"worker received signal {signum}",
                },
            )

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    try:
        state = read_state(context.state_path)
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise RuntimeError("converter run state uses an unsupported schema")
        if state.get("plan_hash") != args.plan_hash:
            raise RuntimeError("worker plan hash does not match durable converter state")
        result = ConverterRunManager().execute(context.directory)
        return 0 if result.get("state") in {"succeeded", "partial", "cancelled"} else 1
    except BaseException as exc:
        state = read_state(context.state_path)
        if state.get("state") in {"queued", "running"}:
            state["state"] = "interrupted"
            state["worker"] = None
            state["error"] = {
                "code": "worker_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            write_state(context.state_path, state)
            _event(
                context,
                "worker_failed",
                state="interrupted",
                data={"message": str(exc)},
            )
        print(
            f"pg-play converter worker: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
