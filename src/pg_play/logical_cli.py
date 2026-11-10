"""CLI options for typed logical replication operations."""

from pg_play.logical import INSPECTIONS, OPERATIONS, load_plan


def add_commands(commands):
    logical = commands.add_parser("logical", help="Plan and observe logical replication workflows")
    actions = logical.add_subparsers(dest="logical_command", required=True)
    inspect = actions.add_parser("inspect")
    inspect.add_argument("config")
    inspect.add_argument("--operation", choices=sorted(INSPECTIONS), default="status")
    inspect.add_argument("--after", type=int, default=0)
    inspect.add_argument("--limit", type=int, default=100)
    inspect.add_argument("--level", choices=["counts", "checksums", "full"], default="checksums")
    plan = actions.add_parser("plan")
    plan.add_argument("config")
    plan.add_argument(
        "--operation", choices=sorted(OPERATIONS | {"controller", "experiment"}), required=True
    )
    plan.add_argument("--scenario")
    plan.add_argument("--duration-seconds", type=float, default=60)
    plan.add_argument("--poll-interval-seconds", type=float, default=5)
    plan.add_argument("--timeout-seconds", type=float, default=600)
    for option in ("reason", "action", "lsn", "table", "sql-file", "evidence", "write-fence"):
        plan.add_argument("--" + option)
    for option in ("max-duration", "wal-rate", "job-id"):
        plan.add_argument("--" + option, type=int)
    plan.add_argument("--source-unavailable", action="store_true", default=None)
    start = actions.add_parser("start")
    start.add_argument("plan")
    start.add_argument("--plan-hash", required=True)
    start.add_argument("--output-directory", required=True)
    start.add_argument("--run-id", required=True)
    for name in ("status", "events", "stop-controller", "resume-controller", "report"):
        cmd = actions.add_parser(name)
        cmd.add_argument("directory")
        if name == "events":
            cmd.add_argument("--after-sequence", type=int, default=0)
            cmd.add_argument("--limit", type=int, default=100)
        elif name == "stop-controller":
            cmd.add_argument("--reason", required=True)
        elif name == "resume-controller":
            cmd.add_argument("--plan-hash", required=True)


def dispatch(manager, args):
    values = vars(args).copy()
    values.pop("command")
    name = values.pop("logical_command")
    if name == "plan":
        keys = (
            "reason",
            "action",
            "lsn",
            "table",
            "sql_file",
            "evidence",
            "write_fence",
            "max_duration",
            "wal_rate",
            "job_id",
            "source_unavailable",
        )
        options = {key: values.pop(key) for key in keys}
        values["options"] = {key: value for key, value in options.items() if value is not None}
    if name == "start":
        values["plan"] = load_plan(values["plan"])
    name = {"stop-controller": "stop", "resume-controller": "resume"}.get(name, name)
    return getattr(manager, name)(**values)
