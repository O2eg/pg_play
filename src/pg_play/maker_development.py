"""Development calls use pg_maker's Git state and worker, without a second scheduler."""

from pathlib import Path


def invoke(client, operation: str, directory: str, **options):
    args = ["dev", operation, str(Path(directory).absolute())]
    if operation == "patch":
        args.append(options.pop("action"))
    if operation == "pin":
        args.append(options.pop("iteration"))
        if not options.pop("pinned", True):
            args.append("--unpin")
    argv = options.pop("argv", None)
    aliases = {"identifier": "id", "output": "out", "components": "component"}
    paths = {
        "lock",
        "from_run",
        "input_store",
        "repository",
        "patch",
        "onto_lock",
        "output",
        "apply",
    }
    for name, value in options.items():
        if value is None:
            continue
        flag = "--" + aliases.get(name, name).replace("_", "-")
        for item in value if isinstance(value, list) else [value]:
            args += [flag, str(Path(item).absolute()) if name in paths else str(item)]
    if operation in {"build", "exec"}:
        args.append("--detach")
    if argv is not None:
        args += ["--", *argv]
    return client.call(tuple(args))


def add_commands(commands):
    command = commands.add_parser("dev-open", help="open an owned Git development session")
    command.add_argument("directory")
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--lock")
    source.add_argument("--from-run")
    command.add_argument("--feature")
    command.add_argument("--input-store")
    command.add_argument("--repository")
    commands.add_parser("dev-status").add_argument("directory")
    command = commands.add_parser("dev-patch")
    command.add_argument("directory")
    command.add_argument(
        "action",
        choices=[
            "import",
            "commit",
            "refresh",
            "pop",
            "push",
            "rebase",
            "continue",
            "abort",
            "export",
            "adopt",
        ],
    )
    command.add_argument("--scope", default="postgresql")
    command.add_argument("--id", dest="identifier")
    command.add_argument("--patch")
    command.add_argument("--count", type=int, default=1)
    command.add_argument("--message")
    command.add_argument("--onto-source")
    command.add_argument("--onto-lock")
    command.add_argument("--out", dest="output")
    for name in ("dev-build", "dev-explain"):
        command = commands.add_parser(name)
        command.add_argument("directory")
        command.add_argument(
            "--goal", choices=["source", "build", "package", "docs"], default="build"
        )
        command.add_argument("--component", dest="components", action="append")
    command = commands.add_parser("dev-exec")
    command.add_argument("directory")
    command.add_argument("--iteration")
    command.add_argument("--component")
    command.add_argument("--target")
    command.add_argument("--timeout-seconds", type=int, default=3600)
    command.add_argument("argv", nargs="+")
    command = commands.add_parser("dev-pin")
    command.add_argument("directory")
    command.add_argument("iteration")
    command.add_argument("--unpin", dest="pinned", action="store_false", default=True)
    command = commands.add_parser("dev-prune")
    command.add_argument("directory")
    command.add_argument("--keep-last", type=int, default=2)
    command.add_argument("--out", dest="output")
    command.add_argument("--apply")
