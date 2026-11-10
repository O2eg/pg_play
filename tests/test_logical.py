from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pg_play.contract import EXIT_CODES, MACHINE_INTERFACE
from pg_play.logical import LogicalClient, LogicalError, checked_plan
from pg_play.logical_runs import LogicalRunManager, controller_lock, save
from pg_play.runner import (
    process_boot_id,
    process_executable,
    process_start_ticks,
    recorded_process_is_alive,
)
from pg_play.state import read_state, write_json


class Runner:
    def __init__(self, root):
        self.root = root
        self.version = "0.2.0"
        self.state = "REPLICATING"
        self.calls = []

    def run(self, invocation):
        self.calls.append(invocation)
        args = invocation.arguments
        if args[0] == "--config":
            args = args[2:]
        cmd = args[0]
        if cmd == "--component-capabilities":
            result = dict(
                capability_schema_version="pg_play/capabilities/v1",
                contract_version="pg_play/component/v2",
                component="pg_logical_repl",
                component_version=self.version,
                exit_codes=EXIT_CODES,
                machine_interface=MACHINE_INTERFACE,
                secret_policy={},
                commands={
                    name: dict(mutates_target=False, machine_output=True, accepts_plan_hash=False)
                    for name in ("inspect-config", "plan", "run", "status")
                },
            )
        elif cmd == "inspect-config":
            result = dict(
                migration_id="test",
                state_dir=str(self.root / "migration-state"),
                config_hash="cfg",
                ddl_mode="captured",
                endpoints={},
            )
        elif cmd == "plan":
            path = self.root / "native.json"
            write_json(path, {"operation": args[1]})
            result = dict(path=str(path), plan_hash="native-hash", plan={"actions": [args[1]]})
        elif cmd == "events":
            result = []
        else:
            result = dict(state=self.state)
        return dict(
            contract_version="pg_play/component/v2",
            component="pg_logical_repl",
            component_version=self.version,
            command=cmd,
            request_id=invocation.request_id,
            status="planned" if cmd == "plan" else "succeeded",
            result=result,
            artifacts=[],
            advisories=[],
            error=None,
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("configuration\n")
    runner = Runner(tmp_path)
    manager = LogicalRunManager(runner)

    def spawn(directory, state):
        state["worker"] = dict(
            pid=os.getpid(), process_start_ticks=process_start_ticks(os.getpid())
        )
        save(directory / "state.json", state)
        return state

    monkeypatch.setattr(manager, "_spawn", spawn)
    return manager, runner, config


def create(setup, tmp_path, operation="controller"):
    manager, _, config = setup
    plan = manager.plan(str(config), operation, duration_seconds=1, poll_interval_seconds=0.1)
    state = manager.start(plan, plan["plan_hash"], tmp_path / "runs", "run-1")
    return manager, plan, Path(state["directory"])


def test_native_plan_hash_is_preserved(setup, tmp_path):
    manager, plan, directory = create(setup, tmp_path, "prepare")
    result = manager.execute(directory)
    assert result["state"] == "succeeded"
    invocation = setup[1].calls[-1]
    assert invocation.arguments[-1] == "native-hash"
    assert plan["plan_hash"] != "native-hash"
    assert manager.report(directory)["state"] == "succeeded"
    assert (directory / "report.json").stat().st_mode & 0o777 == 0o600


def test_modified_input_and_plan_rejected(setup):
    manager, _, config = setup
    plan = manager.plan(str(config), "controller")
    plan["duration_seconds"] = 7
    with pytest.raises(LogicalError, match="hash"):
        checked_plan(plan)
    plan = manager.plan(str(config), "controller")
    config.write_text("changed\n")
    with pytest.raises(LogicalError, match="input changed"):
        manager.client.validate(plan)


def test_input_change_between_controller_iterations_stops_before_next_call(setup, tmp_path):
    manager, plan, directory = create(setup, tmp_path)
    manager.tick(directory, plan)
    setup[2].write_text("changed during execution\n")
    count = len(setup[1].calls)
    with pytest.raises(LogicalError, match="input changed"):
        manager.tick(directory, plan)
    assert len(setup[1].calls) == count


def test_component_upgrade_requires_replanning(setup):
    manager, runner, config = setup
    plan = manager.plan(str(config), "controller")
    runner.version = "different"
    with pytest.raises(LogicalError, match="version changed"):
        manager.client.validate(plan)


@pytest.mark.parametrize(
    "migration_state,expected",
    [
        ("BLOCKED", "blocked"),
        ("DETACH_CLEANUP_PENDING", "partial"),
        ("ABORT_CLEANUP_PENDING", "partial"),
    ],
)
def test_successful_inspection_does_not_hide_migration_failure(
    setup, tmp_path, migration_state, expected
):
    manager, _, directory = create(setup, tmp_path, "abort")
    setup[1].state = migration_state
    assert manager.execute(directory)["state"] == expected


def test_controller_blocked_is_not_success(setup, tmp_path):
    manager, _, directory = create(setup, tmp_path)
    setup[1].state = "BLOCKED"
    assert manager.execute(directory)["state"] == "blocked"
    assert manager.report(directory)["migration_state"] == "BLOCKED"


def test_blocked_controller_resumes_only_after_native_repair(setup, tmp_path):
    manager, plan, directory = create(setup, tmp_path)
    setup[1].state = "BLOCKED"
    assert manager.execute(directory)["state"] == "blocked"
    with pytest.raises(LogicalError, match="planned recovery"):
        manager.resume(directory, plan["plan_hash"])
    setup[1].state = "REPLICATING"
    assert manager.resume(directory, plan["plan_hash"])["state"] == "queued"
    with pytest.raises(LogicalError, match="no final report"):
        manager.report(directory)
    assert manager.execute(directory)["state"] == "succeeded"


def test_lost_worker_cleanup_failure_is_persisted_and_retried(setup, tmp_path, monkeypatch):
    from pg_play import logical_experiment

    manager, plan, directory = create(setup, tmp_path)
    state = read_state(directory / "state.json")
    state.update(state="running", worker={"pid": 999999999}, workload_owned=True)
    save(directory / "state.json", state)

    def fail(*args):
        raise LogicalError("Workload cleanup unavailable")

    monkeypatch.setattr(logical_experiment, "workload", fail)
    current = manager.status(directory)
    assert current["state"] == "interrupted" and current["worker"] is None
    assert current["cleanup_pending"]
    assert current["cleanup_errors"]["workload"] == "Workload cleanup unavailable"
    with pytest.raises(LogicalError, match="cleanup is pending"):
        manager.resume(directory, plan["plan_hash"])

    def recover(*args):
        state = read_state(directory / "state.json")
        state["workload_owned"] = False
        save(directory / "state.json", state)

    monkeypatch.setattr(logical_experiment, "workload", recover)
    assert manager.resume(directory, plan["plan_hash"])["state"] == "queued"
    assert not manager.status(directory)["cleanup_pending"]


def test_report_failure_cannot_publish_success(setup, tmp_path, monkeypatch):
    from pg_play import logical_runs

    manager, _, directory = create(setup, tmp_path, "prepare")
    original = logical_runs.durable_json

    def fail(path, value):
        if path.name == "report.json":
            raise OSError("Injected report persistence failure")
        return original(path, value)

    monkeypatch.setattr(logical_runs, "durable_json", fail)
    with pytest.raises(OSError):
        manager.execute(directory)
    state = read_state(directory / "state.json")
    assert state["state"] == "running"
    state["worker"] = {"pid": 999999999}
    save(directory / "state.json", state)
    assert manager.status(directory)["state"] == "interrupted"
    with pytest.raises(LogicalError, match="no final report"):
        manager.report(directory)


def test_published_terminal_state_always_has_report(setup, tmp_path, monkeypatch):
    from pg_play import logical_runs

    manager, _, directory = create(setup, tmp_path, "prepare")
    original = logical_runs.save
    observed = []

    def inspect(path, value):
        if value["state"] == "succeeded":
            assert (directory / "report.json").is_file()
            assert value["report"]["sha256"]
            observed.append(True)
        return original(path, value)

    monkeypatch.setattr(logical_runs, "save", inspect)
    manager.execute(directory)
    assert observed


def test_stop_does_not_pause_or_abort_database(setup, tmp_path):
    manager, _, directory = create(setup, tmp_path)
    manager.stop(directory, "test interruption")
    assert manager.execute(directory)["state"] == "cancelled"
    arguments = [i.arguments for i in setup[1].calls]
    assert not any("abort" in args or "pause" in args for args in arguments)


def test_resume_preserves_progress_but_rejects_blocked(setup, tmp_path):
    manager, plan, directory = create(setup, tmp_path)
    state = read_state(directory / "state.json")
    state.update(state="interrupted", worker=None, elapsed_seconds=0.5)
    save(directory / "state.json", state)
    setup[1].state = "BLOCKED"
    with pytest.raises(LogicalError, match="planned recovery"):
        manager.resume(directory, plan["plan_hash"])
    setup[1].state = "REPLICATING"
    assert manager.resume(directory, plan["plan_hash"])["elapsed_seconds"] == 0.5
    assert manager.execute(directory)["state"] == "succeeded"


def test_lost_worker_and_report_tampering(setup, tmp_path):
    manager, _, directory = create(setup, tmp_path)
    state = read_state(directory / "state.json")
    state["worker"] = {"pid": 999999999, "process_start_ticks": 1}
    save(directory / "state.json", state)
    assert manager.status(directory)["state"] == "interrupted"
    state.update(state="queued")
    save(directory / "state.json", state)
    manager.execute(directory)
    (directory / "report.json").write_text("{}")
    with pytest.raises(LogicalError, match="Report hash"):
        manager.report(directory)


def test_host_reboot_disambiguates_pid_start_time():
    record = dict(
        pid=os.getpid(), process_start_ticks=process_start_ticks(os.getpid()), boot_id="old-boot"
    )
    assert not recorded_process_is_alive(record)


def test_exclusive_controller_owner(tmp_path):
    with controller_lock(tmp_path):
        with pytest.raises(LogicalError, match="Another pg_play"):
            with controller_lock(tmp_path):
                pass


@pytest.mark.parametrize("value", [0, -1, float("nan"), True, 100000])
def test_bounded_controller_duration(setup, value):
    manager, _, config = setup
    with pytest.raises(LogicalError):
        manager.plan(str(config), "controller", duration_seconds=value)


def test_real_component_contract_and_secret_free_config(tmp_path):
    pytest.importorskip("pg_logical_repl")
    import yaml

    endpoint = dict(host="localhost", database="db", user="postgres", password_env="UNSET_SECRET")
    config = tmp_path / "migration.yaml"
    config.write_text(
        yaml.safe_dump(
            dict(
                schema_version="pg_logical_connector/config-v2",
                migration_id="contract_test",
                state_dir="state",
                source=dict(admin=endpoint),
                target=dict(admin=endpoint),
            )
        )
    )
    client = LogicalClient()
    assert client.capabilities()["component"] == "pg_logical_repl"
    result = client.inspect(str(config), "inspect-config")
    assert result["status"] == "succeeded"
    assert "UNSET_SECRET" not in json.dumps(result)


@pytest.fixture
def scenario_setup(setup, tmp_path, monkeypatch):
    from pg_play import logical_experiment

    manager, plan, directory = create(setup, tmp_path)
    plan["scenario"] = dict(
        prepare={},
        workload=None,
        diagnostics=False,
        ddl_files=[],
        converter_plans=[],
        oracle=True,
        finish="cutover",
        write_fence="reviewed-fence.json",
    )
    stages = []
    monkeypatch.setattr(logical_experiment, "ready", lambda *args: None)
    monkeypatch.setattr(
        logical_experiment, "native_step", lambda *args, **kwargs: stages.append(args[3])
    )
    monkeypatch.setattr(manager, "observe", lambda *args: None)
    monkeypatch.setattr(
        manager, "call", lambda *args, **kwargs: dict(status="succeeded", result=dict(passed=True))
    )
    return manager, plan, directory, stages


def test_failed_workload_start_preserves_existing_scheduler(scenario_setup, setup):
    from pg_play.logical_experiment import execute_scenario

    manager, plan, directory, stages = scenario_setup
    runner = setup[1]
    original = runner.run

    def run(invocation):
        if invocation.component != "pg_workload":
            return original(invocation)
        runner.calls.append(invocation)
        return dict(
            component_version="workload-test",
            status="succeeded",
            result=dict(scheduler=dict(running=True)),
        )

    runner.run = run
    plan["scenario"]["workload"] = dict(root=str(directory), version="workload-test")
    with pytest.raises(LogicalError, match="already has a running scheduler"):
        execute_scenario(manager, directory, plan)
    assert not any(
        call.component == "pg_workload" and call.arguments[0] == "stop" for call in runner.calls
    )
    assert stages == ["prepare", "start"]
    assert not read_state(directory / "state.json").get("workload_owned")


def test_failed_start_race_does_not_claim_or_stop_competing_scheduler(scenario_setup, setup):
    from pg_play.logical_experiment import execute_scenario

    manager, plan, directory, _ = scenario_setup
    native = dict(
        plan_hash="hash", scheduler=dict(current_state_hash="old", desired_state_hash="new")
    )
    plan["scenario"]["workload"] = dict(
        root=str(directory),
        version="test",
        arguments=[],
        plan=native,
    )
    calls = []

    def run(invocation):
        command = invocation.arguments[0]
        calls.append(command)
        return dict(
            component_version="test",
            status="failed" if command == "start" else "succeeded",
            result={"scheduler": {"running": False}} if command == "status" else native,
        )

    setup[1].run = run
    with pytest.raises(LogicalError, match="start did not succeed"):
        execute_scenario(manager, directory, plan)
    assert calls == ["--component-capabilities", "status", "plan", "start"]
    state = read_state(directory / "state.json")
    assert not state.get("workload_owned") and not state["workload_start_pending"]


@pytest.mark.parametrize("has_owned_process", [False, True])
def test_cleanup_never_signals_scheduler_from_another_run(scenario_setup, setup, has_owned_process):
    from pg_play.logical_experiment import workload

    manager, plan, directory, _ = scenario_setup
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        for _ in range(2)
    ]
    owned, foreign = processes
    try:
        plan["scenario"]["workload"] = dict(root=str(directory), version="test")
        state = read_state(directory / "state.json")
        state.update(workload_owned=has_owned_process, workload_start_pending=not has_owned_process)
        save(directory / "state.json", state)
        if has_owned_process:
            write_json(
                directory / "workload-scheduler.json",
                dict(
                    schema_version="pg_play/active-process-v1",
                    pid=owned.pid,
                    process_start_ticks=process_start_ticks(owned.pid),
                    boot_id=process_boot_id(),
                    executable=process_executable(owned.pid),
                    owner_uid=os.getuid(),
                ),
            )
        calls = []

        def run(invocation):
            calls.append(invocation.arguments[0])
            return dict(
                status="succeeded", result=dict(scheduler=dict(running=True, pid=foreign.pid))
            )

        setup[1].run = run
        with pytest.raises(LogicalError, match="unowned scheduler"):
            workload(manager, directory, plan, False)
        assert foreign.poll() is None
        assert calls == ["status"]
        if has_owned_process:
            assert owned.wait(timeout=3) < 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_independent_final_mismatch_prevents_cutover_commit(scenario_setup, monkeypatch):
    from pg_play import logical_evidence
    from pg_play.logical_experiment import execute_scenario

    manager, plan, directory, stages = scenario_setup
    monkeypatch.setattr(
        logical_evidence, "exact_oracle", lambda *args, deferred=False: dict(passed=not deferred)
    )
    with pytest.raises(LogicalError, match="Independent final comparison failed"):
        execute_scenario(manager, directory, plan)
    assert stages == ["prepare", "start", "finalize"]
    assert read_state(directory / "oracle.json")["passed"] is False
