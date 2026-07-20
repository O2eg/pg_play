"""Human-oriented pg_play command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

from pg_play.service import PgPlayService
from pg_play.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-play",
        description="Orchestrate reproducible PostgreSQL experiments.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    capabilities = commands.add_parser("capabilities", help="Show installed component capabilities")
    capabilities.add_argument(
        "--component",
        choices=("pg_configurator", "pg_stand", "pg_workload", "pg_diag"),
    )

    validate = commands.add_parser("validate", help="Validate an experiment manifest and inputs")
    validate.add_argument("manifest")

    plan = commands.add_parser("plan", help="Build a read-only experiment plan")
    plan.add_argument("manifest")

    run = commands.add_parser("run", help="Run an experiment from an unchanged plan")
    run.add_argument("manifest")
    run.add_argument("--plan-hash", required=True)
    run.add_argument("--run-id", required=True)

    status = commands.add_parser("status", help="Read one immutable experiment run state")
    status.add_argument("manifest")
    status.add_argument("--run-id", required=True)

    inspect = commands.add_parser("inspect-report", help="Validate and summarize pg_diag JSON")
    inspect.add_argument("report")

    compare = commands.add_parser("compare-reports", help="Compare two pg_diag JSON artifacts")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    service = PgPlayService()
    try:
        if args.command == "capabilities":
            result = service.component_capabilities(args.component)
        elif args.command == "validate":
            result = service.validate_experiment(args.manifest)
        elif args.command == "plan":
            result = service.plan_experiment(args.manifest)
        elif args.command == "run":
            result = service.run_experiment(
                args.manifest,
                plan_hash=args.plan_hash,
                run_id=args.run_id,
            )
        elif args.command == "status":
            result = service.experiment_status(args.manifest, args.run_id)
        elif args.command == "inspect-report":
            result = service.inspect_report(args.report)
        elif args.command == "compare-reports":
            result = service.compare_reports(args.baseline, args.candidate)
        else:  # pragma: no cover
            raise AssertionError(f"unhandled command: {args.command}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"pg-play: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
