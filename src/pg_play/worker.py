"""Detached durable experiment worker used by the CLI and MCP service."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from pg_play.manifest import load_manifest
from pg_play.service import PgPlayService
from pg_play.state import read_state, utc_now, write_json, write_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pg_play.worker")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start-gate", required=True)
    parser.add_argument("--resume", action="store_true")
    return parser


def _wait_for_parent(gate: Path, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while gate.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gate = Path(args.start_gate)
    _wait_for_parent(gate)
    service = PgPlayService()
    manifest = load_manifest(args.manifest)
    service._validate_run_id(args.run_id)
    context = service._run_context(manifest, args.run_id)

    def request_cancel(signum: int, _frame: object) -> None:
        if not context.cancel_path.exists():
            write_json(
                context.cancel_path,
                {
                    "schema_version": "pg_play/cancel-request-v1",
                    "run_id": args.run_id,
                    "requested_at": utc_now(),
                    "reason": f"worker received signal {signum}",
                },
            )

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    os.environ["PG_PLAY_WORKER"] = "1"
    try:
        state = service._reconcile_worker(context)
        if state.get("plan_hash") != args.plan_hash:
            raise RuntimeError("worker plan hash does not match durable run state")
        if state.get("manifest_hash") != manifest.document_hash:
            raise RuntimeError("worker manifest does not match durable run state")
        plan = service._load_stored_plan(context, args.plan_hash)
        service._validate_core_artifacts(
            manifest,
            plan,
            state,
            expected_run_id=context.run_id,
        )
        service._validate_component_versions(plan)
        if args.resume:
            service._validate_resumable_steps(state, context.run_directory)
            state = service._reconcile_worker(context)
        service._execute_experiment(manifest, plan, state, resume=args.resume)
        return 0
    except BaseException as exc:
        state = read_state(context.state_path)
        if state.get("state") in {"queued", "running", "cancelling"}:
            state["state"] = "interrupted"
            state["worker"] = None
            state["error"] = {
                "code": "worker_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            write_state(context.state_path, state)
            service._event(
                context,
                "worker_failed",
                state="interrupted",
                data={"message": str(exc)},
            )
        print(f"pg-play worker: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
