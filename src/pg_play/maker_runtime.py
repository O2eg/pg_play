"""The pg_stand-specific runtime profile lives with the orchestrator."""

import copy
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from pg_play.maker import MakerClient, checked_plan
from pg_play.state import write_json


def check_runtime(client: MakerClient, plan: dict, variant: str, base_image: str) -> dict:
    """Maker validates its profiles; play only supplies the consumer requirements."""
    checked_plan(plan, plan["plan_hash"])
    client.require("runtime check", "runtime plan", "runtime build", "artifacts")
    with TemporaryDirectory(prefix="pg-play-runtime-check-") as scratch:
        root = Path(scratch)
        lock = copy.deepcopy(plan["lock"])
        if plan.get("input_store"):
            lock.setdefault("transport", {})["input_store"] = plan["input_store"]
        write_json(root / "lock.json", lock)
        write_json(root / "plan.json", plan["build_plan"])
        write_json(root / "profile.json", runtime_profile(base_image))
        return client.call(
            (
                "runtime",
                "check",
                str(root / "lock.json"),
                "--plan",
                str(root / "plan.json"),
                "--profile",
                str(root / "profile.json"),
                "--variant",
                variant,
            )
        )


def runtime_profile(
    base_image: str, *, min_free_disk_bytes: int = 4 * 1024**3, memory_bytes: int = 2 * 1024**3
) -> dict:
    stand = files("pg_stand").joinpath("docker")

    def reference(resource):
        path = Path(str(resource)).resolve()
        return {"path": str(path), "sha256": "sha256:" + sha256(path.read_bytes()).hexdigest()}

    return {
        "base_image": base_image,
        "min_free_disk_bytes": min_free_disk_bytes,
        "memory_bytes": memory_bytes,
        "entrypoint": "pg-stand-entrypoint",
        "extensions": [
            "pg_stat_statements",
            "pg_buffercache",
            "pg_wait_sampling",
            "pg_stat_kcache",
        ],
        "libraries": ["auto_explain"],
        "files": {
            "docker-entrypoint.sh": reference(
                files("pg_play").joinpath("assets", "maker-entrypoint.py")
            ),
            "pg-stand-entrypoint": reference(stand.joinpath("pg-stand-entrypoint.sh")),
            "pg-stand-pgbouncer": reference(stand.joinpath("pg-stand-pgbouncer.sh")),
        },
    }


def plan_runtime(client: MakerClient, artifacts: str, base_image: str, output: str) -> dict:
    directory = Path(output).absolute()
    directory.mkdir(parents=True, exist_ok=True)
    profile_path = directory / "runtime-profile.json"
    write_json(profile_path, runtime_profile(base_image))
    result = client.call(
        ("runtime", "plan", str(Path(artifacts).absolute()), "--profile", str(profile_path))
    )
    write_json(directory / "runtime-plan.json", result)
    return result


def start_runtime(
    client: MakerClient, plan: str, plan_hash: str, output: str, operations: str, operation_id: str
) -> dict:
    result = client.call(
        (
            "runtime",
            "build",
            str(Path(plan).absolute()),
            "--plan-hash",
            plan_hash,
            "--out",
            str(Path(output).absolute()),
            "--operations",
            str(Path(operations).absolute()),
            "--operation-id",
            operation_id,
            "--detach",
        ),
        request_id="pg-play-runtime-" + operation_id,
    )
    return {**result, "operation_directory": str(Path(operations).absolute() / operation_id)}
