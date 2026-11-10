"""Native maker extension and publication workflows; workers stay owned by maker."""

from pathlib import Path

from pg_play.maker import MakerClient


def _path(value: str) -> str:
    return str(Path(value).absolute())


def _launch(client: MakerClient, args, operations, operation_id, kind):
    result = client.call(
        (*args, "--operations", _path(operations), "--operation-id", operation_id, "--detach"),
        request_id=f"pg-play-{kind}-{operation_id}",
    )
    return client._reference(result, operations)


def extension_init(
    client: MakerClient,
    directory: str,
    extensions: list[str],
    *,
    major: int = 18,
    profile: str | None = None,
    packages: str | None = None,
    platform: str = "debian-13",
    architecture: str = "amd64",
    split_bundled: bool = False,
) -> dict:
    args = (
        "extension",
        "init",
        _path(directory),
        "--major",
        str(major),
        "--platform",
        platform,
        "--architecture",
        architecture,
    )
    for name in extensions:
        args += ("--extension", name)
    if profile is not None:
        args += ("--profile", profile)
    if packages is not None:
        args += ("--packages", _path(packages))
    if split_bundled:
        args += ("--split-bundled",)
    return client.call(args)


def extension_prepare(
    client: MakerClient,
    manifest: str,
    output: str,
    operations: str,
    input_store: str,
    operation_id: str,
    *,
    offline: bool = False,
) -> dict:
    args = (
        "extension",
        "prepare",
        _path(manifest),
        "--out",
        _path(output),
        "--input-store",
        _path(input_store),
    )
    if offline:
        args += ("--offline",)
    return _launch(client, args, operations, operation_id, "extension-prepare")


def extension_plan(client: MakerClient, lock: str) -> dict:
    return client.call(("extension", "plan", _path(lock)))


def extension_build(
    client: MakerClient,
    lock: str,
    operations: str,
    operation_id: str,
    *,
    input_store: str | None = None,
    publish_config: str | None = None,
) -> dict:
    args = ("extension", "build", _path(lock))
    if input_store is not None:
        args += ("--input-store", _path(input_store))
    if publish_config is not None:
        client.require("publish run")
        args += ("--publish-config", _path(publish_config))
    return {
        **_launch(client, args, operations, operation_id, "extension-build"),
        "source_operation_directory": str(Path(operations).absolute() / operation_id),
    }


def publish_plan(client: MakerClient, source: str, config: str) -> dict:
    return client.call(("publish", "plan", _path(source), "--config", _path(config)))


def publish_run(
    client: MakerClient,
    source: str,
    config: str,
    operations: str,
    operation_id: str,
    *,
    wait_for_source: bool = False,
) -> dict:
    args = ("publish", "run", _path(source), "--config", _path(config))
    if wait_for_source:
        args += ("--wait-for-source",)
    return _launch(client, args, operations, operation_id, "publish")


def add_commands(commands):
    command = commands.add_parser("init", help="Initialize a project from a maker profile")
    command.add_argument("directory")
    command.add_argument("--profile")
    command = commands.add_parser("config-show")
    command.add_argument("manifest")
    command = commands.add_parser("package-names")
    command.add_argument("manifest")
    command = commands.add_parser("extension-init")
    command.add_argument("directory")
    command.add_argument("--extension", action="append", dest="extensions", required=True)
    command.add_argument("--major", type=int, default=18)
    command.add_argument("--profile")
    command.add_argument("--packages")
    command.add_argument("--platform", default="debian-13")
    command.add_argument("--architecture", choices=["amd64", "arm64"], default="amd64")
    command.add_argument("--split-bundled", action="store_true")
    command = commands.add_parser("extension-prepare")
    command.add_argument("manifest")
    command.add_argument("--output", required=True)
    command.add_argument("--input-store", required=True)
    command.add_argument("--operations", required=True)
    command.add_argument("--operation-id", required=True)
    command.add_argument("--offline", action="store_true")
    command = commands.add_parser("extension-plan")
    command.add_argument("lock")
    command = commands.add_parser("extension-build")
    command.add_argument("lock")
    command.add_argument("--operations", required=True)
    command.add_argument("--operation-id", required=True)
    command.add_argument("--input-store")
    command.add_argument("--publish-config")
    for name in ("publish-plan", "publish-run"):
        command = commands.add_parser(name)
        command.add_argument("source")
        command.add_argument("--config", required=True)
        if name == "publish-run":
            command.add_argument("--operations", required=True)
            command.add_argument("--operation-id", required=True)
            command.add_argument("--wait-for-source", action="store_true")
