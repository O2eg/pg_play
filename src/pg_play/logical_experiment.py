"""Repeatable migration experiments on explicitly prepared source/target databases."""

from __future__ import annotations

import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from pg_play.logical import (
    LogicalError,
    bounded,
    check_references,
    checked_plan,
    command,
    file_reference,
)
from pg_play.logical_runs import durable_json, event, save
from pg_play.runner import (
    ComponentInvocation,
    process_boot_id,
    process_executable,
    process_start_ticks,
    terminate_recorded_process,
)
from pg_play.state import read_state, write_json

SCHEMA = "pg_play/logical-experiment-v1"


def plan_scenario(client, config, description, path, inputs):
    reference = file_reference(path)
    inputs.append(reference)
    path = Path(reference["path"])
    # Use the component's duplicate-key rejecting YAML loader.
    # No database connection is made by this loader.
    try:
        from pg_logical_repl.config import UniqueLoader
    except ImportError:
        raise LogicalError(
            "pg-logical-repl is missing; reinstall pg-play with its dependencies"
        ) from None
    raw = yaml.load(path.read_text(), Loader=UniqueLoader)
    allowed = {
        "schema_version",
        "ddl_files",
        "converter_plans",
        "workload_manifest",
        "diagnostics",
        "oracle",
        "quiescent_at_verification",
        "finish",
        "write_fence",
        "readiness_timeout_seconds",
    }
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA or set(raw) - allowed:
        raise LogicalError("Invalid logical experiment scenario")
    result = dict(
        ddl_files=[],
        converter_plans=[],
        workload=None,
        finish=raw.get("finish", "replicating"),
        readiness_timeout_seconds=bounded(
            raw.get("readiness_timeout_seconds", 600), "readiness_timeout_seconds"
        ),
    )
    for key, default in (
        ("diagnostics", True),
        ("oracle", True),
        ("quiescent_at_verification", False),
    ):
        if type(raw.get(key, default)) is not bool:
            raise LogicalError(key + " must be boolean")
        result[key] = raw.get(key, default)
    if result["finish"] not in {"replicating", "abort", "cutover"}:
        raise LogicalError("finish must be replicating, abort or cutover")
    if result["oracle"] and not result["quiescent_at_verification"]:
        raise LogicalError("The exact oracle requires declared quiescence after workload stop")
    for key in ("ddl_files", "converter_plans"):
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 100:
            raise LogicalError(key + " must contain at most 100 file references")
        for value in values:
            if not isinstance(value, str):
                raise LogicalError("Scenario inputs must be file paths")
            ref = file_reference(path.parent / value)
            inputs.append(ref)
            result[key].append(ref["path"])
    if result["ddl_files"] and description["ddl_mode"] == "frozen":
        raise LogicalError("DDL experiments require managed_only or captured mode")
    for sql_file in result["ddl_files"]:
        client.require(client.call(config, ("ddl", "check", sql_file)))
    if result["converter_plans"] and description["ddl_mode"] != "captured":
        raise LogicalError("Converter packets require captured DDL mode")
    from pg_play.converter_runs import validate_converter_run_plan

    for plan_path in result["converter_plans"]:
        packet = validate_converter_run_plan(
            read_state(Path(plan_path)), runner=client.runner, verify_sources=True
        )
        if packet["component_plan"].get("safety", {}).get("tracker_may_be_initialized", True):
            raise LogicalError(
                "This pg_converter plan may initialize its tracker using compound DDL. "
                "Run the packet before migration prepare, or use reviewed ddl_files. "
                "Live packets require a converter contract guaranteeing no tracker DDL."
            )
        connections = packet["component_plan"]["target"].get("database_connections", {})
        source = description["endpoints"]["source"]
        if len(connections) != 1 or any(
            str(endpoint.get(key)) != str(source[key])
            for endpoint in connections.values()
            for key in ("host", "port", "database")
        ):
            raise LogicalError("Each converter plan must select exactly the migration source")
    if raw.get("write_fence"):
        ref = file_reference(path.parent / raw["write_fence"])
        inputs.append(ref)
        result["write_fence"] = ref["path"]
    if result["finish"] == "cutover" and not result.get("write_fence"):
        raise LogicalError("Cutover requires a real external write-fence record")
    if raw.get("workload_manifest"):
        ref = file_reference(path.parent / raw["workload_manifest"])
        inputs.append(ref)
        result["workload"] = plan_workload(client.runner, ref["path"], description, inputs)
    # Bind prepare to the currently reviewed database identities and catalogs.
    # Subsequent plans are created after their prerequisite stage has completed.
    native = client.native_plan(config, "prepare", {})
    inputs.append(file_reference(native["path"]))
    result["prepare"] = {k: native[k] for k in ("path", "plan_hash")}
    result["identities"] = {side: native["plan"][side]["identity"] for side in ("source", "target")}
    result["authorized_stages"] = [
        "prepare",
        "start",
        "controller",
        "reviewed DDL",
        "verify",
        result["finish"],
    ]
    return result


def plan_workload(runner, path, description, inputs):
    from pg_stand.config import load_config

    from pg_play.manifest import load_manifest
    from pg_play.service import PgPlayService

    manifest = load_manifest(path)
    service = PgPlayService(runner)
    config = load_config(service._stand_config(manifest), project_directory=manifest.stand_project)
    connection = service._connection_descriptor(manifest, config)
    source = description["endpoints"]["source"]
    if any(str(connection[key]) != str(source[key]) for key in ("host", "port", "database")):
        raise LogicalError("Workload must target exactly the migration source")
    inputs.append(file_reference(manifest.stand_config))
    args = (
        *service._workload_common_args(manifest, connection),
        *service._profile_args(manifest),
        "--enable-selected",
        "--run-immediately",
    )
    if manifest.workload.job_interval_seconds is not None:
        args += ("--job-interval-seconds", str(manifest.workload.job_interval_seconds))
    envelope = runner.run(
        ComponentInvocation(
            component="pg_workload",
            arguments=("plan", *args, "--operation=scheduler"),
            request_id="logical-workload-plan",
        )
    )
    result = workload_result(envelope)
    return dict(
        root=str(manifest.workload.project),
        arguments=list(args),
        plan=result,
        version=envelope["component_version"],
    )


def workload_result(envelope):
    if envelope["status"] not in {"planned", "succeeded", "running"}:
        raise LogicalError("Workload operation did not succeed")
    return envelope["result"]


def native_step(manager, directory, plan, label, operation, options=None, initial=None):
    checked_plan(plan)
    root = directory / "steps"
    root.mkdir(exist_ok=True, mode=0o700)
    completed = root / (label + ".result.json")
    intent = root / (label + ".intent.json")
    if completed.exists():
        record = read_state(completed)
        check_references([record["intent"]])
        return record["result"]
    if intent.exists():
        native = read_state(intent)["native"]
        check_references([read_state(intent)["native_file"]])
        cursor = 0
        while True:
            rows = manager.client.require(
                manager.call(
                    directory,
                    plan,
                    ("events", "--after", cursor, "--limit", 1000),
                    step=label + "-receipt",
                )
            )
            for row in rows:
                details = row.get("details", {})
                if (
                    row.get("event") == "operation_completed"
                    and details.get("kind") == operation
                    and details.get("plan_hash") == native["plan_hash"]
                ):
                    result = dict(state=details["state"], reconciled=True)
                    durable_json(completed, dict(intent=file_reference(intent), result=result))
                    manager.refresh_migration(directory, plan)
                    event(directory, "stage_reconciled", stage=label, native_event=row["sequence"])
                    return result
            if len(rows) < 1000:
                break
            cursor = rows[-1]["sequence"]
    else:
        native = initial or manager.client.native_plan(
            plan["config"],
            operation,
            options or {},
            **manager.invocation(directory, plan, step=label + "-plan"),
        )
        native = {key: native[key] for key in ("path", "plan_hash")}
        durable_json(
            intent,
            dict(operation=operation, native=native, native_file=file_reference(native["path"])),
        )
    event(directory, "stage_intent", stage=label, native_plan_hash=native["plan_hash"])
    result = manager.client.require(
        manager.call(
            directory,
            plan,
            command(operation, native, options or {}),
            step=label,
        )
    )
    durable_json(completed, dict(intent=file_reference(intent), result=result))
    manager.refresh_migration(directory, plan)
    event(directory, "stage_complete", stage=label)
    return result


def ready(manager, directory, plan):
    deadline = time.monotonic() + plan["scenario"]["readiness_timeout_seconds"]
    while time.monotonic() < deadline:
        result = manager.tick(directory, plan)
        queue = result.get("ddl_queue", [])
        tables = result.get("tables", [])
        new_tables = result.get("new_tables", [])
        if (
            result.get("state") == "REPLICATING"
            and all(row["status"] in {"done", "skipped"} for row in queue)
            and all(row["state"] == "r" for row in tables)
            and all(row["state"] in {"ready", "removed"} for row in new_tables)
        ):
            return result
        time.sleep(min(plan["poll_interval_seconds"], 1))
    raise LogicalError("Migration did not become ready before the deadline")


def workload(manager, directory, plan, start):
    spec = plan["scenario"]["workload"]
    if spec is None:
        return
    if start:
        capability = manager.runner.run(
            ComponentInvocation(
                component="pg_workload",
                arguments=("--component-capabilities",),
                request_id=f"logical-{directory.name}-workload-capabilities",
            )
        )
        if capability["component_version"] != spec["version"]:
            raise LogicalError("Workload component version changed")
        observed = manager.runner.run(
            ComponentInvocation(
                component="pg_workload",
                arguments=("status", "--root", spec["root"]),
                request_id=f"logical-{directory.name}-workload-status",
            )
        )
        if workload_result(observed)["scheduler"]["running"]:
            raise LogicalError("The selected workload root already has a running scheduler")
        fresh = manager.runner.run(
            ComponentInvocation(
                component="pg_workload",
                arguments=("plan", *spec["arguments"], "--operation=scheduler"),
                request_id=f"logical-{directory.name}-workload-revalidate",
            )
        )
        fresh_plan = workload_result(fresh)
        import copy

        previous, current = copy.deepcopy(spec["plan"]), copy.deepcopy(fresh_plan)
        old_current = previous["scheduler"].pop("current_state_hash")
        new_current = current["scheduler"].pop("current_state_hash")
        for value in (previous, current):
            value.pop("plan_hash")
        if previous != current or new_current not in {
            old_current,
            previous["scheduler"]["desired_state_hash"],
        }:
            raise LogicalError("Workload inputs changed since planning")
        state = read_state(directory / "state.json")
        state["workload_start_pending"] = True
        log = Path(spec["root"]) / "state/scheduler.log"
        state["workload_log_offset"] = log.stat().st_size if log.exists() else 0
        save(directory / "state.json", state)
        envelope = manager.runner.run(
            ComponentInvocation(
                component="pg_workload",
                arguments=("start", *spec["arguments"], "--plan-hash", fresh_plan["plan_hash"]),
                request_id=f"logical-{directory.name}-workload",
                cancel_path=directory / "stop.request.json",
                active_process_path=directory / "workload-process.json",
                timeout_seconds=120,
            )
        )
        if envelope["status"] not in {"succeeded", "running"}:
            state["workload_start_pending"] = False
            save(directory / "state.json", state)
            raise LogicalError("Workload start did not succeed")
        result = workload_result(envelope)
        pid = result.get("pid")
        if type(pid) is not int or pid <= 0:
            raise LogicalError("Workload start returned no scheduler identity")
        record = dict(
            schema_version="pg_play/active-process-v1",
            pid=pid,
            process_start_ticks=process_start_ticks(pid),
            boot_id=process_boot_id(),
            executable=process_executable(pid),
            owner_uid=os.getuid(),
        )
        if record["process_start_ticks"] is None or record["executable"] is None:
            raise LogicalError("Scheduler exited before ownership could be recorded")
        durable_json(directory / "workload-scheduler.json", record)
        state.update(workload_owned=True, workload_start_pending=False)
        save(directory / "state.json", state)
        if envelope["component_version"] != spec["version"]:
            raise LogicalError("Workload component version changed")
    else:
        # Never stop by root alone: its PID file may now belong to another run.
        terminate_recorded_process(directory / "workload-scheduler.json", grace_seconds=30)
        observed = manager.runner.run(
            ComponentInvocation(
                component="pg_workload",
                arguments=("status", "--root", spec["root"]),
                request_id=f"logical-{directory.name}-workload-status",
                timeout_seconds=30,
            )
        )
        if workload_result(observed)["scheduler"]["running"]:
            raise LogicalError("Workload root has an unowned scheduler; inspect it before resuming")
        state = read_state(directory / "state.json")
        state.update(workload_owned=False, workload_start_pending=False)
        log = Path(spec["root"]) / "state/scheduler.log"
        content = b""
        if log.is_file():
            with log.open("rb") as stream:
                stream.seek(state.get("workload_log_offset", 0))
                content = stream.read()
        text = content.decode("utf-8", errors="replace")
        finished = re.findall(r"^Job finished (\S+) exit=0$", text, re.MULTILINE)
        failures = len(re.findall(r"^Job failed ", text, re.MULTILINE))
        guarded = "Resource guard triggered:" in text
        expected = {
            profile["name"] + ":" + job
            for profile in spec["plan"]["profiles"]
            for job in profile["selected_jobs"]
        }
        state["workload_evidence"] = dict(
            passed=bool(expected) and expected.issubset(finished) and not failures and not guarded,
            completed_jobs=len(finished),
            failed_jobs=failures,
            resource_guard_triggered=guarded,
            missing_jobs=sorted(expected.difference(finished)),
            log_window_sha256=hashlib.sha256(content).hexdigest(),
        )
        save(directory / "state.json", state)
    event(directory, "workload_started" if start else "workload_stopped")


def execute_scenario(manager, directory, plan):
    from pg_play.logical_evidence import capture, exact_oracle

    scenario = plan["scenario"]
    native_step(manager, directory, plan, "prepare", "prepare", initial=scenario["prepare"])
    native_step(manager, directory, plan, "start", "start")
    ready(manager, directory, plan)
    diagnostics = {}
    packet_results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        try:
            if scenario["workload"] is not None:
                workload(manager, directory, plan, True)
            if scenario["diagnostics"]:
                futures = {
                    side: pool.submit(capture, manager, directory, plan, side)
                    for side in ("source", "target")
                }
            for index, sql_file in enumerate(scenario["ddl_files"]):
                ready(manager, directory, plan)
                native_step(
                    manager, directory, plan, f"ddl-{index}", "ddl-exec", {"sql_file": sql_file}
                )
            from pg_play.converter_runs import ConverterRunManager

            packets = ConverterRunManager(manager.runner)
            for index, path in enumerate(scenario["converter_plans"]):
                ready(manager, directory, plan)
                packet = read_state(Path(path))
                packet_dir = directory / "packets" / f"packet-{index}"
                if not packet_dir.exists():
                    packets.start(packet, packet["plan_hash"], packet_dir.parent, packet_dir.name)
                try:
                    while True:
                        state = packets.status(packet_dir)
                        if state["state"] not in {"queued", "running"}:
                            break
                        manager.tick(directory, plan)
                        time.sleep(min(1, plan["poll_interval_seconds"]))
                    if state["state"] != "succeeded":
                        raise LogicalError(
                            "Converter outcome requires inspection; automatic replay disabled"
                        )
                    packet_results.append(str(packet_dir / "result.json"))
                finally:
                    if packets.status(packet_dir)["state"] in {"queued", "running"}:
                        packets.cancel(packet_dir, reason="Migration experiment stopped")
            manager.observe(directory, plan)
        finally:
            state = read_state(directory / "state.json")
            if state.get("workload_owned") or state.get("workload_start_pending"):
                workload(manager, directory, plan, False)
            # Bounded captures terminate cooperatively on the same stop request.
            for side, future in futures.items():
                diagnostics[side] = future.result()
    ready(manager, directory, plan)
    verification = manager.client.require(manager.call(directory, plan, ("verify",), step="verify"))
    oracle = exact_oracle(plan["config"]) if scenario["oracle"] else None
    write_json(directory / "oracle.json", oracle)
    passed = bool(verification.get("passed")) and (oracle is None or oracle["passed"])
    passed = passed and all(value["passed"] for value in diagnostics.values())
    workload_evidence = read_state(directory / "state.json").get("workload_evidence")
    if scenario["workload"] is not None:
        passed = passed and bool(workload_evidence and workload_evidence["passed"])
    if passed and scenario["finish"] == "abort":
        native_step(manager, directory, plan, "abort", "abort", {"reason": "Experiment complete"})
    elif passed and scenario["finish"] == "cutover":
        native_step(
            manager,
            directory,
            plan,
            "finalize",
            "cutover-finalize",
            {"write_fence": scenario["write_fence"]},
        )
        if scenario["oracle"]:
            oracle = exact_oracle(plan["config"], deferred=True)
            write_json(directory / "oracle.json", oracle)
            if not oracle["passed"]:
                raise LogicalError("Independent final comparison failed; target remains closed")
        native_step(manager, directory, plan, "commit", "cutover-commit")
    migration = manager.client.require(manager.call(directory, plan, ("status",), step="status"))
    state = read_state(directory / "state.json")
    state["migration_state"] = migration["state"]
    save(directory / "state.json", state)
    return dict(
        passed=passed,
        migration=migration,
        verification=verification,
        oracle=oracle,
        diagnostics=diagnostics,
        workload=workload_evidence,
        packets=packet_results,
        quiescence="operator_declared" if scenario["oracle"] else "not_checked",
    )
