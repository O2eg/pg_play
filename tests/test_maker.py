"""Maker lifecycle and artifact handoff without building or starting PostgreSQL."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pg_play.contract import canonical_hash
from pg_play.maker import MakerClient, MakerError
from pg_play.maker_experiment import cancel_owned, execute_phase, phase_plan, stand_config
from pg_play.manifest import MakerSpec
from pg_play.runner import ComponentCancelledError, ComponentRunner
from pg_play.service import PgPlayService
from pg_play.state import read_state, write_json


def reviewed_plan():
    value = {
        "schema_version": "pg_play/maker-plan-v1",
        "goal": "package",
        "run_db_tests": False,
        "input_store": "/inputs",
        "lock": {"spec": {"config": {"postgresql": {"version": "18.1"}}}},
        "build_plan": {
            "plan_hash": "sha256:build",
            "spec": {
                "steps": [
                    {"feature_id": "custom", "target_id": "debian13-x64", "branch": "candidate"}
                ]
            },
        },
    }
    value["plan_hash"] = canonical_hash(value)
    return value


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, invocation):
        self.calls.append(invocation)
        return {"status": "succeeded", "result": {"operation_id": "build", "status": "running"}}


def test_configuration_options_reach_detached_maker_cli(tmp_path):
    runner = Runner()
    service = PgPlayService(runner)
    service.maker_resolve(
        "build.yaml",
        "env.json",
        "lock.json",
        str(tmp_path),
        "inputs",
        "build",
        with_doc=True,
        doc_languages=["en", "ru"],
        features=["vendor"],
        build_mode="debug",
        config_dir="config",
    )
    args = runner.calls[-1].arguments
    assert args[0] == "resolve"
    assert "--detach" in args and "--with-doc" in args
    assert args[args.index("--build-mode") + 1] == "debug"
    assert args[args.index("--feature") + 1] == "vendor"
    assert args.count("--doc-language") == 2
    service.maker_changes_workspace(
        "work", "inputs", "sha256:source", lock="lock.json", until="change/fix"
    )
    assert runner.calls[-1].arguments[-2:] == ("--until", "change/fix")


def test_start_freezes_reviewed_inputs_and_cancels_worker_directly(tmp_path):
    runner = Runner()
    client = MakerClient(runner)
    plan = reviewed_plan()
    result = client.start(plan, plan["plan_hash"], tmp_path, "build")
    assert "--detach" in runner.calls[0].arguments
    assert "--skip-db-tests" in runner.calls[0].arguments
    assert runner.calls[0].cancel_path is None
    assert result["build_plan_hash"] == "sha256:build"
    client.cancel(result["operation_directory"])
    assert runner.calls[-1].arguments == ("cancel", str(tmp_path / "build"))
    changed = copy.deepcopy(plan)
    changed["goal"] = "verified"
    with pytest.raises(MakerError, match="changed since review"):
        client.start(changed, plan["plan_hash"], tmp_path, "other")
    assert len(runner.calls) == 2


def test_same_operation_id_cannot_be_rebound(tmp_path):
    client = MakerClient(Runner())
    plan = reviewed_plan()
    client.start(plan, plan["plan_hash"], tmp_path, "build")
    plan["input_store"] = "/other"
    plan["plan_hash"] = canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"})
    with pytest.raises(MakerError, match="different reviewed plan"):
        client.start(plan, plan["plan_hash"], tmp_path, "build")


def test_maker_plan_identity_is_independent_of_replanning_time(tmp_path):
    lock = tmp_path / "lock.json"
    write_json(lock, {"created_at": "locked-time", "transport": {"input_store": "/inputs"}})

    class PlanningRunner:
        counter = 0

        def run(self, invocation):
            self.counter += 1
            return {
                "status": "succeeded",
                "result": {"created_at": str(self.counter), "plan_hash": "sha256:build"},
            }

    client = MakerClient(PlanningRunner())
    assert client.plan(lock) == client.plan(lock)


def manifest(tmp_path):
    plan = reviewed_plan()
    write_json(tmp_path / "reviewed.json", plan)
    config = tmp_path / "stand.yaml"
    config.write_text(yaml.safe_dump({"spec": {"postgres": {"version": 18}}}))
    return SimpleNamespace(
        maker=MakerSpec(
            tmp_path / "reviewed.json",
            plan["plan_hash"],
            "custom.debian13-x64.candidate",
            "sha256:" + "a" * 64,
            20,
        ),
        stand_config=config,
        artifact_root=tmp_path / "runs",
        experiment_id="custom-test",
    )


def test_phase_binds_image_identity_and_detects_profile_changes(tmp_path, monkeypatch):
    value = manifest(tmp_path)
    phase = phase_plan(value)
    path = stand_config(value)
    assert yaml.safe_load(path.read_text())["spec"]["postgres"]["image"] == phase["image_tag"]
    assert "image" not in yaml.safe_load(value.stand_config.read_text())["spec"]["postgres"]
    monkeypatch.setattr("pg_play.maker_experiment.runtime_profile", lambda _: {"changed": True})
    assert phase_plan(value)["image_tag"] != phase["image_tag"]


def test_phase_cancel_reaches_maker_after_start(tmp_path):
    phase = phase_plan(manifest(tmp_path))
    cancel = tmp_path / "cancel.json"
    calls = []

    class Client:
        def start(self, *args):
            cancel.write_text("{}")
            return {"status": "running"}

        def cancel(self, directory):
            calls.append(directory)

    with pytest.raises(ComponentCancelledError):
        execute_phase(Client(), phase, tmp_path / "run", cancel, resume=False)
    assert calls == [tmp_path / "run/maker/operations/build"]


def test_orphan_cancel_refuses_path_escape(tmp_path):
    write_json(tmp_path / "maker/active.json", {"operation_id": "../../other"})
    with pytest.raises(MakerError, match="invalid experiment-owned"):
        cancel_owned(MakerClient(Runner()), tmp_path)


def test_phase_preserves_unverified_qualification_and_resumes_failed_runtime(tmp_path, monkeypatch):
    phase = phase_plan(manifest(tmp_path))
    root = tmp_path / "run"
    runtime_directory = root / "maker/operations/runtime"
    runtime = {
        "plan_hash": "sha256:runtime",
        "profile": phase["profile"],
        "build_plan_hash": "sha256:build",
        "manifest_hash": "sha256:manifest",
    }
    write_json(runtime_directory / "input.json", {"plan": runtime})
    record = {"runtime_plan_hash": runtime["plan_hash"], "image_id": "sha256:image"}
    record["document_hash"] = canonical_hash(record)
    write_json(root / "maker/runtime-image.json", record)
    monkeypatch.setattr("pg_play.maker_experiment.plan_runtime", lambda *a: runtime)
    monkeypatch.setattr("pg_play.maker_experiment._check_image", lambda *a: None)
    resumed = []

    class Client:
        def start(self, *args):
            return {
                "status": "succeeded",
                "result": {"qualification": "unverified", "db_tests": "skipped"},
            }

        def status(self, directory):
            return {"status": "failed"}

        def resume(self, directory, *, plan_hash):
            resumed.append((directory, plan_hash))
            return {"status": "succeeded"}

        def artifacts(self, directory):
            return {"artifacts": [{"path": "package.deb", "sha256": "sha256:package"}]}

    result = execute_phase(Client(), phase, root, root / "cancel.json", resume=True)
    assert resumed == [(runtime_directory, "sha256:runtime")]
    assert result["qualification"] == "unverified"
    assert result["db_tests"] == "skipped"
    assert read_state(root / "maker/provenance.json") == result


def test_real_machine_capabilities_round_trip():
    import shutil
    import sys

    if not shutil.which("pg-maker") and not Path(sys.executable).with_name("pg-maker").is_file():
        pytest.skip("optional pg-maker extra is not installed")
    result = PgPlayService(ComponentRunner()).component_capabilities("pg_maker")["pg_maker"]
    assert isinstance(result["secret_policy"], dict)
    assert result["commands"]["runtime build"]["accepts_plan_hash"]


def test_experiment_runs_maker_before_stand_and_binds_actual_image_id(tmp_path, monkeypatch):
    from test_service import FakeRunner, _manifest

    path = _manifest(tmp_path)
    reviewed = reviewed_plan()
    write_json(tmp_path / "maker-plan.json", reviewed)
    document = yaml.safe_load(path.read_text())
    document["spec"]["maker"] = {
        "plan": "maker-plan.json",
        "plan_hash": reviewed["plan_hash"],
        "variant": "custom.debian13-x64.candidate",
        "runtime_base_image": "sha256:" + "a" * 64,
    }
    path.write_text(yaml.safe_dump(document))

    class ExperimentRunner(FakeRunner):
        def run(self, invocation):
            response = super().run(invocation)
            if invocation.component == "pg_workload" and invocation.arguments[0] == "start":
                response["status"] = "running"
            return response

    runner = ExperimentRunner()
    service = PgPlayService(runner)
    monkeypatch.setattr(
        service, "component_capabilities", lambda _: {"pg_maker": {"component_version": "test"}}
    )
    monkeypatch.setattr(
        service,
        "_credential_context",
        lambda *a: {"workload_password": "fixture", "admin_password": "fixture"},
    )
    image_id = "sha256:" + "b" * 64

    def execute(client, phase, run_directory, cancel_path, *, resume):
        result = {
            "image": {"image_id": image_id},
            "qualification": "unverified",
            "db_tests": "skipped",
        }
        write_json(run_directory / "maker/provenance.json", result)
        return result

    monkeypatch.setattr("pg_play.maker_experiment.execute_phase", execute)
    plan = service.plan_experiment(path)
    result = service.run_experiment(path, plan_hash=plan["plan_hash"], run_id="candidate")
    assert result["state"] == "succeeded", result["error"]
    assert [step["name"] for step in result["steps"]][:2] == ["maker", "stand"]
    assert result["maker"]["qualification"] == "unverified"
    assert result["maker_stand_plan"]["desired_state_hash"] != plan["stand"]["desired_state_hash"]
    actions = [
        i for i in runner.invocations if i.component == "pg_stand" and "status" in i.arguments
    ]
    assert len(actions) == 1
    config = yaml.safe_load(Path(actions[0].arguments[1]).read_text())
    assert config["spec"]["postgres"]["image"] == image_id
    assert config["spec"]["docker"]["pull_policy"] == "never"
