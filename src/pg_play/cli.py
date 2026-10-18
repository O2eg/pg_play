"""Human-oriented pg_play command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pg_play.service import PgPlayService
from pg_play.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-play",
        description="Orchestrate reproducible PostgreSQL experiments.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    maker = commands.add_parser("maker", help="Manage durable pg_maker operations")
    maker_commands = maker.add_subparsers(dest="maker_command", required=True)
    from pg_play.maker_development import add_commands

    add_commands(maker_commands)
    maker_commands.add_parser("validate").add_argument("manifest")
    for name in ("prepare", "resolve"):
        command = maker_commands.add_parser(name)
        command.add_argument("--config-dir")
        command.add_argument("--feature", action="append", dest="features")
        command.add_argument("--build-mode", choices=["release", "debug"])
        docs = command.add_mutually_exclusive_group()
        docs.add_argument("--with-doc", dest="with_doc", action="store_true", default=None)
        docs.add_argument("--without-doc", dest="with_doc", action="store_false")
        command.add_argument("--doc-language", action="append", dest="doc_languages")
        command.add_argument("manifest")
        command.add_argument("--output", required=True)
        command.add_argument("--operations", required=True)
        command.add_argument("--input-store", required=True)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--offline", action="store_true")
        if name == "resolve":
            command.add_argument("--environments", required=True)
    command = maker_commands.add_parser("plan")
    command.add_argument("lock")
    command.add_argument("--input-store")
    command.add_argument("--goal", default="package")
    command.add_argument("--run-db-tests", action="store_true")
    command = maker_commands.add_parser("start")
    command.add_argument("plan")
    command.add_argument("--plan-hash", required=True)
    command.add_argument("--operations", required=True)
    command.add_argument("--operation-id", required=True)
    command.add_argument("--cache-store")
    for name in ("status", "events", "cancel", "resume", "artifacts", "export"):
        command = maker_commands.add_parser(name)
        command.add_argument("directory")
        if name == "events":
            command.add_argument("--after-sequence", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
        if name == "resume":
            command.add_argument("--plan-hash")
            command.add_argument("--input-hash")
        if name == "export":
            command.add_argument("--output", required=True)
    command = maker_commands.add_parser("runtime-plan")
    command.add_argument("artifacts")
    command.add_argument("--base-image", required=True)
    command.add_argument("--output", required=True)
    command = maker_commands.add_parser("changes-workspace")
    command.add_argument("directory")
    command.add_argument("--input-store", required=True)
    command.add_argument("--source-hash", required=True)
    command.add_argument("--lock")
    command.add_argument("--feature")
    command.add_argument("--until")
    command = maker_commands.add_parser("changes-validate")
    command.add_argument("file")
    command = maker_commands.add_parser("changes-export")
    command.add_argument("directory")
    command.add_argument("--output", required=True)
    command.add_argument("--id", dest="identifier", required=True)
    command.add_argument("--postgresql", required=True)
    command = maker_commands.add_parser("runtime-start")
    command.add_argument("plan")
    command.add_argument("--plan-hash", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--operations", required=True)
    command.add_argument("--operation-id", required=True)

    capabilities = commands.add_parser("capabilities", help="Show installed component capabilities")
    capabilities.add_argument(
        "--component",
        choices=(
            "pg_maker",
            "pg_configurator",
            "pg_converter",
            "pg_stand",
            "pg_workload",
            "pg_diag",
            "pg_perf_bench",
        ),
    )

    converter_plan = commands.add_parser(
        "plan-converter-run",
        help="Plan an exact sequential pg_converter packet run",
    )
    converter_plan.add_argument("--project", required=True)
    converter_plan.add_argument("--config", required=True)
    converter_plan.add_argument("--packet", required=True)
    converter_plan.add_argument("--database-selector", default="ALL")
    converter_plan.add_argument("--placeholders-file")
    converter_plan.add_argument("--config-overrides-file")
    converter_plan.add_argument("--timeout-seconds", type=float, default=3600)

    converter_start = commands.add_parser(
        "start-converter-run",
        help="Start a reviewed converter plan in a durable worker",
    )
    converter_start.add_argument("plan")
    converter_start.add_argument("--plan-hash", required=True)
    converter_start.add_argument("--out", required=True)
    converter_start.add_argument("--run-id", required=True)

    converter_status = commands.add_parser(
        "converter-run-status",
        help="Read and reconcile durable converter state",
    )
    converter_status.add_argument("run_directory")

    converter_events = commands.add_parser(
        "converter-run-events",
        help="Read ordered durable converter events",
    )
    converter_events.add_argument("run_directory")
    converter_events.add_argument("--after-sequence", type=int, default=0)
    converter_events.add_argument("--limit", type=int, default=1000)

    converter_cancel = commands.add_parser(
        "cancel-converter-run",
        help="Request cooperative converter cancellation",
    )
    converter_cancel.add_argument("run_directory")
    converter_cancel.add_argument("--reason")

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
        if args.command == "maker":
            options = vars(args).copy()
            options.pop("command")
            name = options.pop("maker_command")
            if name == "start":
                options["plan"] = json.loads(Path(options["plan"]).read_text(encoding="utf-8"))
            result = getattr(service, "maker_" + name.replace("-", "_"))(**options)
        elif args.command == "capabilities":
            result = service.component_capabilities(args.component)
        elif args.command == "plan-converter-run":
            result = service.plan_converter_run(
                args.project,
                args.config,
                args.packet,
                args.database_selector,
                args.placeholders_file,
                args.config_overrides_file,
                args.timeout_seconds,
            )
        elif args.command == "start-converter-run":
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            result = service.start_converter_run(
                plan,
                args.plan_hash,
                args.out,
                args.run_id,
            )
        elif args.command == "converter-run-status":
            result = service.converter_run_status(args.run_directory)
        elif args.command == "converter-run-events":
            result = service.converter_run_events(
                args.run_directory,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
        elif args.command == "cancel-converter-run":
            result = service.cancel_converter_run(
                args.run_directory,
                reason=args.reason,
            )
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
