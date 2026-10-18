"""Bind a reviewed build to a stand; maker retains ownership of build workers."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import yaml

from pg_play.contract import canonical_hash
from pg_play.maker import MakerClient, MakerError, checked_plan
from pg_play.maker_runtime import plan_runtime, runtime_profile, start_runtime
from pg_play.runner import ComponentCancelledError
from pg_play.state import read_state, write_json, write_text


def phase_plan(manifest) -> dict | None:
    spec = manifest.maker
    if spec is None:
        return None
    reviewed = read_state(spec.plan)
    checked_plan(reviewed, spec.plan_hash)
    if reviewed["goal"] not in {"package", "verified"}:
        raise MakerError("experiment maker goal must produce packages")
    steps = reviewed["build_plan"]["spec"]["steps"]
    if not any(
        ".".join((step["feature_id"], step["target_id"], step["branch"])) == spec.variant
        for step in steps
    ):
        raise MakerError("selected maker variant/branch is absent from the reviewed build")
    profile = runtime_profile(spec.runtime_base_image)
    result = {
        "reviewed_plan": reviewed,
        "variant": spec.variant,
        "profile": profile,
        "timeout_seconds": spec.timeout_seconds,
    }
    result["phase_hash"] = canonical_hash(result)
    # Planning uses a placeholder tied to the reviewed inputs. Execution replaces
    # only this image field with the produced immutable ID and records a stand subplan.
    identity = canonical_hash({"phase": result["phase_hash"], "experiment": manifest.experiment_id})
    result["image_tag"] = "pg-maker/stand:" + identity.split(":")[1]
    return result


def stand_config(manifest, image_id: str | None = None) -> Path:
    phase = phase_plan(manifest)
    if phase is None:
        return manifest.stand_config
    document = yaml.safe_load(manifest.stand_config.read_text(encoding="utf-8"))
    postgres = document["spec"]["postgres"]
    version = phase["reviewed_plan"]["lock"]["spec"]["config"]["postgresql"]["version"]
    if int(postgres["version"]) != int(version.split(".")[0]):
        raise MakerError("stand PostgreSQL major differs from maker build")
    if image_id is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise MakerError("runtime image must be an immutable local ID")
    postgres["image"] = image_id or phase["image_tag"]
    postgres.pop("base_image", None)
    document["spec"].setdefault("docker", {})["pull_policy"] = "never"
    path = (
        manifest.artifact_root / ".maker-plans" / (canonical_hash(document).split(":")[1] + ".yaml")
    )
    text = yaml.safe_dump(document, sort_keys=True)
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise MakerError("bound stand configuration changed")
    if not path.exists():
        write_text(path, text)
    return path


def _docker(*arguments: str, missing_ok: bool = False) -> str:
    result = subprocess.run(
        ["docker", *arguments], text=True, capture_output=True, timeout=60, check=False
    )
    if result.returncode:
        if missing_ok and "No such image" in result.stderr:
            return ""
        raise MakerError("runtime image command failed: " + result.stderr.strip())
    return result.stdout.strip()


def _check_image(record: dict) -> None:
    image = json.loads(_docker("image", "inspect", record["image_id"]))[0]
    if (
        image["Id"] != record["image_id"]
        or image["Config"]["Labels"].get("io.pg-maker.runtime-plan") != record["runtime_plan_hash"]
    ):
        raise MakerError("runtime image identity/provenance mismatch")


def cancel_owned(client: MakerClient, run_directory: Path) -> None:
    reference = read_state(run_directory / "maker" / "active.json")
    if reference.get("state") != "not_found":
        if reference.get("operation_id") not in {"build", "runtime"}:
            raise MakerError("invalid experiment-owned maker operation reference")
        directory = run_directory / "maker" / "operations" / reference["operation_id"]
        if directory.is_dir():
            state = client.status(directory)
            if state["status"] in {"planned", "running"} or state.get("resources"):
                client.cancel(directory)


def execute_phase(
    client: MakerClient, phase: dict, run_directory: Path, cancel_path: Path, *, resume: bool
) -> dict:
    root = run_directory / "maker"
    operations = root / "operations"
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "phase.json", phase)
    deadline = time.monotonic() + phase["timeout_seconds"]

    def observe(operation_id, start, plan_hash):
        directory = operations / operation_id
        # Persist before launching so cancellation works even if pg_play dies
        # between maker starting its worker and returning its response.
        write_json(root / "active.json", {"operation_id": operation_id})
        if cancel_path.exists():
            cancel_owned(client, run_directory)
            raise ComponentCancelledError("cancelled before maker operation")
        if directory.exists():
            status = client.status(directory)
            payload = read_state(directory / "input.json")
            if payload.get("plan", {}).get("plan_hash") != plan_hash:
                raise MakerError("existing maker operation has a different plan")
            if status["status"] in {"failed", "cancelled", "interrupted", "partial"}:
                if not resume:
                    raise MakerError("maker operation failed; resume the experiment explicitly")
                status = client.resume(directory, plan_hash=plan_hash)
        else:
            try:
                status = start()
            except Exception:
                # A timed-out/disconnected launcher can already have created its
                # detached worker. Its known directory is the cancellation handle.
                if directory.is_dir():
                    client.cancel(directory)
                raise
        while True:
            if cancel_path.exists() or time.monotonic() >= deadline:
                client.cancel(directory)
                if cancel_path.exists():
                    raise ComponentCancelledError("maker cancelled with experiment")
                raise MakerError("maker phase timed out; worker cancellation requested")
            if status["status"] == "succeeded":
                return status
            if status["status"] in {"failed", "cancelled", "interrupted", "partial"}:
                raise MakerError("maker operation did not succeed: " + str(status.get("error")))
            time.sleep(0.5)
            status = client.status(directory)

    reviewed = phase["reviewed_plan"]
    build = observe(
        "build",
        lambda: client.start(reviewed, reviewed["plan_hash"], operations, "build"),
        reviewed["build_plan"]["plan_hash"],
    )
    artifact_directory = operations / "build" / "workspace/output" / phase["variant"]
    # The exact runtime plan includes package bytes, not just the requested build hash.
    runtime = plan_runtime(
        client, str(artifact_directory), phase["profile"]["base_image"], str(root)
    )
    if runtime["profile"] != phase["profile"]:
        raise MakerError("runtime profile changed since experiment planning")
    image_file = root / "runtime-image.json"
    observe(
        "runtime",
        lambda: start_runtime(
            client,
            str(root / "runtime-plan.json"),
            runtime["plan_hash"],
            str(image_file),
            str(operations),
            "runtime",
        ),
        runtime["plan_hash"],
    )
    record = read_state(image_file)
    if record.get("document_hash") != canonical_hash(
        {k: v for k, v in record.items() if k != "document_hash"}
    ):
        raise MakerError("runtime result hash changed")
    if record.get("runtime_plan_hash") != runtime["plan_hash"]:
        raise MakerError("runtime result belongs to another plan")
    _check_image(record)
    artifacts = client.artifacts(operations / "build")
    result = {
        "phase_hash": phase["phase_hash"],
        "build_plan_hash": runtime["build_plan_hash"],
        "artifact_manifest_hash": runtime["manifest_hash"],
        "image": record,
        "planned_image_reference": phase["image_tag"],
        "qualification": build["result"]["qualification"],
        "db_tests": build["result"]["db_tests"],
        "artifacts": artifacts["artifacts"],
    }
    write_json(root / "provenance.json", result)
    return result
