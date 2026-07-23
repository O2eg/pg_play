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
        choices=("pg_configurator", "pg_stand", "pg_workload", "pg_diag", "pg_perf_bench"),
    )

    validate = commands.add_parser("validate", help="Validate an experiment manifest and inputs")
    validate.add_argument("manifest")

    plan = commands.add_parser("plan", help="Build a read-only experiment plan")
    plan.add_argument("manifest")

    run = commands.add_parser("run", help="Run an experiment from an unchanged plan")
    run.add_argument("manifest")
    run.add_argument("--plan-hash", required=True)
    run.add_argument("--run-id", required=True)

    start = commands.add_parser("start", help="Start a durable experiment worker and return")
    start.add_argument("manifest")
    start.add_argument("--plan-hash", required=True)
    start.add_argument("--run-id", required=True)

    resume = commands.add_parser(
        "resume",
        help="Resume a verified failed, cancelled, or interrupted experiment",
    )
    resume.add_argument("manifest")
    resume.add_argument("--plan-hash", required=True)
    resume.add_argument("--run-id", required=True)

    status = commands.add_parser("status", help="Read and reconcile durable experiment state")
    status.add_argument("manifest")
    status.add_argument("--run-id", required=True)

    events = commands.add_parser("events", help="Read ordered durable experiment events")
    events.add_argument("manifest")
    events.add_argument("--run-id", required=True)
    events.add_argument("--after-sequence", type=int, default=0)
    events.add_argument("--limit", type=int, default=1000)

    cancel = commands.add_parser("cancel", help="Request cooperative experiment cancellation")
    cancel.add_argument("manifest")
    cancel.add_argument("--run-id", required=True)
    cancel.add_argument("--reason")

    inspect = commands.add_parser("inspect-report", help="Validate and summarize pg_diag JSON")
    inspect.add_argument("report")

    compare = commands.add_parser("compare-reports", help="Compare two pg_diag JSON artifacts")
    compare.add_argument("baseline")
    compare.add_argument("candidate")

    inspect_benchmark = commands.add_parser(
        "inspect-benchmark-report",
        help="Validate and summarize pg_perf_bench JSON",
    )
    inspect_benchmark.add_argument("report")

    compare_benchmarks = commands.add_parser(
        "compare-benchmark-reports",
        help="Compare two pg_perf_bench JSON artifacts",
    )
    compare_benchmarks.add_argument("baseline")
    compare_benchmarks.add_argument("candidate")
    commands.add_parser("benchmark-profiles", help="List pg_perf_bench workload profiles")
    commands.add_parser("benchmark-join-tasks", help="List pg_perf_bench JOIN scenarios")
    join_benchmarks = commands.add_parser(
        "join-benchmark-reports",
        help="Join an explicit set of pg_perf_bench JSON reports",
    )
    join_benchmarks.add_argument("--report", action="append", required=True)
    join_benchmarks.add_argument("--join-task", required=True)
    join_benchmarks.add_argument("--out", required=True)
    join_benchmarks.add_argument("--report-name", required=True)

    teardown = commands.add_parser(
        "teardown",
        help="Stop workload processes and remove an experiment stand",
    )
    teardown.add_argument("manifest")
    teardown.add_argument("--clear-stand-data", action="store_true")
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
        elif args.command == "start":
            result = service.start_experiment(
                args.manifest,
                plan_hash=args.plan_hash,
                run_id=args.run_id,
            )
        elif args.command == "resume":
            result = service.resume_experiment(
                args.manifest,
                plan_hash=args.plan_hash,
                run_id=args.run_id,
            )
        elif args.command == "status":
            result = service.experiment_status(args.manifest, args.run_id)
        elif args.command == "events":
            result = service.experiment_events(
                args.manifest,
                args.run_id,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
        elif args.command == "cancel":
            result = service.cancel_experiment(
                args.manifest,
                args.run_id,
                reason=args.reason,
            )
        elif args.command == "inspect-report":
            result = service.inspect_report(args.report)
        elif args.command == "compare-reports":
            result = service.compare_reports(args.baseline, args.candidate)
        elif args.command == "inspect-benchmark-report":
            result = service.inspect_benchmark_report(args.report)
        elif args.command == "compare-benchmark-reports":
            result = service.compare_benchmark_reports(args.baseline, args.candidate)
        elif args.command == "benchmark-profiles":
            result = service.benchmark_profiles()
        elif args.command == "benchmark-join-tasks":
            result = service.benchmark_join_tasks()
        elif args.command == "join-benchmark-reports":
            result = service.join_benchmark_reports(
                args.report,
                args.join_task,
                args.out,
                args.report_name,
            )
        elif args.command == "teardown":
            result = service.teardown_experiment(
                args.manifest,
                clear_stand_data=args.clear_stand_data,
            )
        else:  # pragma: no cover
            raise AssertionError(f"unhandled command: {args.command}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"pg-play: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
