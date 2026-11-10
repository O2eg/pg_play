"""Public integration boundaries: policies, capabilities and detached operation ownership."""

from pathlib import Path

import pytest
from test_maker import Runner, reviewed_plan

from pg_play.cli import build_parser, main
from pg_play.maker import MakerClient, MakerError
from pg_play.maker_runtime import check_runtime
from pg_play.runner import ComponentRunner
from pg_play.service import PgPlayService
from pg_play.state import read_state, write_json


@pytest.mark.parametrize("override", [None, True, False])
@pytest.mark.parametrize("locked", [True, False])
def test_test_policy_uses_effective_maker_result(tmp_path, override, locked):
    class PlanningRunner(Runner):
        def run(self, invocation):
            result = super().run(invocation)
            if invocation.arguments[0] == "plan":
                args = invocation.arguments
                enabled = (
                    True
                    if "--run-db-tests" in args
                    else (False if "--skip-db-tests" in args else locked)
                )
                return {
                    "status": "succeeded",
                    "result": {
                        "plan_hash": "sha256:build",
                        "spec": {"run_db_tests": enabled},
                    },
                }
            return result

    runner = PlanningRunner()
    client = MakerClient(runner)
    lock = tmp_path / "lock.json"
    write_json(lock, {"transport": {"input_store": str(tmp_path / "inputs")}})
    plan = client.plan(lock, run_db_tests=override)
    effective = locked if override is None else override
    assert plan["run_db_tests"] is effective
    if override is None:
        assert not {"--run-db-tests", "--skip-db-tests"}.intersection(runner.calls[-1].arguments)
    client.start(plan, plan["plan_hash"], tmp_path / "operations", "build")
    flag = "--run-db-tests" if effective else "--skip-db-tests"
    assert flag in runner.calls[-1].arguments
    assert len([c for c in runner.calls if c.arguments == ("--component-capabilities",)]) == 1


def test_cli_preserves_unspecified_test_policy_and_exposes_explicit_skip():
    parser = build_parser()
    assert parser.parse_args(["maker", "plan", "lock.json"]).run_db_tests is None
    assert parser.parse_args(["maker", "plan", "lock.json", "--run-db-tests"]).run_db_tests is True
    assert (
        parser.parse_args(["maker", "plan", "lock.json", "--skip-db-tests"]).run_db_tests is False
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["maker", "plan", "lock.json", "--run-db-tests", "--skip-db-tests"])


def test_missing_capability_fails_before_extension_build(tmp_path):
    class OldRunner(Runner):
        def run(self, invocation):
            result = super().run(invocation)
            if invocation.arguments == ("--component-capabilities",):
                result["result"]["commands"].pop("extension build")
            return result

    runner = OldRunner()
    with pytest.raises(MakerError, match="lacks commands: extension build"):
        PgPlayService(runner).maker_extension_build("lock.json", str(tmp_path), "extensions")
    assert [c.arguments for c in runner.calls] == [("--component-capabilities",)]


@pytest.mark.parametrize("kind", ["database", "extensions"])
def test_automatic_publication_tracks_returned_worker_and_source(tmp_path, kind):
    class PublishingRunner(Runner):
        def run(self, invocation):
            result = super().run(invocation)
            if invocation.arguments[0] in {"build", "extension"}:
                result["result"]["operation_id"] = "publication-other-id"
            return result

    runner = PublishingRunner()
    service = PgPlayService(runner)
    config = tmp_path / "receivers.yaml"
    if kind == "database":
        plan = reviewed_plan()
        result = service.maker_start(
            plan, plan["plan_hash"], str(tmp_path), "build", publish_config=str(config)
        )
    else:
        result = service.maker_extension_build(
            "lock.json", str(tmp_path), "build", publish_config=str(config)
        )
    assert result["operation_directory"] == str(tmp_path / "publication-other-id")
    assert result["source_operation_directory"] == str(tmp_path / "build")
    assert "--detach" in runner.calls[-1].arguments
    assert runner.calls[-1].cancel_path is None
    assert runner.calls[-1].arguments[
        runner.calls[-1].arguments.index("--publish-config") + 1
    ] == str(config)
    service.maker_cancel(result["operation_directory"])
    assert runner.calls[-1].arguments == ("cancel", str(tmp_path / "publication-other-id"))
    service.maker_resume(result["operation_directory"], input_hash="sha256:publication-input")
    assert runner.calls[-1].arguments[-2:] == ("--input-hash", "sha256:publication-input")


def test_package_commands_forward_paths_without_shell_and_keep_native_ownership(tmp_path):
    runner = Runner()
    service = PgPlayService(runner)
    project = str(tmp_path / "extension project")
    service.maker_extension_init(
        project,
        ["pg_wait_sampling"],
        profile="custom-profile",
        packages=str(tmp_path / "native packages"),
        split_bundled=True,
    )
    args = runner.calls[-1].arguments
    assert args[args.index("--profile") + 1] == "custom-profile"
    assert "--split-bundled" in args
    service.maker_extension_prepare(
        project + "/extensions.yaml",
        project + "/lock.json",
        project + "/operations",
        project + "/inputs",
        "prepare",
    )
    assert "--detach" in runner.calls[-1].arguments
    assert runner.calls[-1].request_id == "pg-play-extension-prepare-prepare"
    service.maker_publish_plan(project, str(tmp_path / "receivers.yaml"))
    assert runner.calls[-1].arguments[:2] == ("publish", "plan")
    service.maker_publish_run(
        project, str(tmp_path / "receivers.yaml"), project, "publish", wait_for_source=True
    )
    assert "--wait-for-source" in runner.calls[-1].arguments
    assert runner.calls[-1].request_id == "pg-play-publish-publish"
    service.maker_export(project, str(tmp_path / "export"), include_inputs=True)
    assert runner.calls[-1].arguments[-1] == "--include-inputs"


def test_runtime_preflight_sends_unchanged_build_and_consumer_requirements(tmp_path):
    paths = []
    plan = reviewed_plan()

    class RuntimeRunner(Runner):
        def run(self, invocation):
            result = super().run(invocation)
            args = invocation.arguments
            if args[:2] == ("runtime", "check"):
                paths.append(Path(args[2]).parent)
                assert read_state(Path(args[2]))["transport"]["input_store"] == plan["input_store"]
                assert read_state(Path(args[args.index("--plan") + 1])) == plan["build_plan"]
                profile = read_state(Path(args[args.index("--profile") + 1]))
                assert "pg_wait_sampling" in profile["extensions"]
                return {
                    "status": "failed",
                    "error": {
                        "message": "build profile lacks runtime modules",
                        "details": {"missing": ["pg_wait_sampling"]},
                    },
                }
            return result

    with pytest.raises(MakerError, match="pg_wait_sampling"):
        check_runtime(
            MakerClient(RuntimeRunner()),
            plan,
            "custom.debian13-x64.candidate",
            "sha256:" + "a" * 64,
        )
    assert paths and not paths[0].exists()


def test_cli_and_mcp_use_native_publication_adapter(tmp_path, monkeypatch, capsys):
    from pg_play import mcp_server

    runner = Runner()
    service = PgPlayService(runner)
    monkeypatch.setattr("pg_play.cli.PgPlayService", lambda: service)
    assert (
        main(
            [
                "maker",
                "publish-run",
                str(tmp_path),
                "--config",
                "receivers.yaml",
                "--operations",
                str(tmp_path),
                "--operation-id",
                "publish",
            ]
        )
        == 0
    )
    assert runner.calls[-1].arguments[:2] == ("publish", "run")
    assert '"operation_directory"' in capsys.readouterr().out
    monkeypatch.setattr(mcp_server, "_service", lambda: service)
    mcp_server.maker_extension_build("lock.json", str(tmp_path), "extensions")
    assert runner.calls[-1].arguments[:2] == ("extension", "build")


def test_real_maker_initializes_generic_projects_and_reports_identity(tmp_path):
    runner = ComponentRunner()
    service = PgPlayService(runner)
    project = tmp_path / "database"
    service.maker_init(str(project))
    config = service.maker_config_show(str(project / "build.yaml"))
    assert config["data"]["defaults"]["validation"]["run_db_tests"] is True
    assert service.maker_package_names(str(project / "build.yaml"))
    for platform in ("debian-13", "rocky-10"):
        directory = tmp_path / platform
        service.maker_extension_init(
            str(directory), ["pg_wait_sampling", "pg_stat_kcache", "pg_repack"], platform=platform
        )
        assert (directory / "extensions.yaml").is_file()
