"""CLI and MCP development operations preserve arguments and maker ownership."""

from test_maker import Runner

from pg_play.cli import build_parser
from pg_play.service import PgPlayService


def test_development_build_is_detached_and_has_no_second_scheduler():
    runner = Runner()
    service = PgPlayService(runner)
    service.maker_dev_build("/work/session", components=["pg-wait-sampling"])
    args = runner.calls[-1].arguments
    assert args == (
        "dev",
        "build",
        "/work/session",
        "--goal",
        "build",
        "--component",
        "pg-wait-sampling",
        "--detach",
    )
    assert runner.calls[-1].cancel_path is None
    service.maker_dev_patch(
        "/work/session", "rebase", scope="postgresql", onto_lock="/work/new.json"
    )
    assert runner.calls[-1].arguments == (
        "dev",
        "patch",
        "/work/session",
        "rebase",
        "--scope",
        "postgresql",
        "--onto-lock",
        "/work/new.json",
    )


def test_debug_arguments_are_not_shell_interpolated():
    runner = Runner()
    service = PgPlayService(runner)
    service.maker_dev_exec("/work/session", ["printf", "%s", "$(false)"], component="probe")
    assert runner.calls[-1].arguments[-5:] == ("--detach", "--", "printf", "%s", "$(false)")
    args = build_parser().parse_args(
        ["maker", "dev-exec", "/work/session", "--component", "probe", "--", "make", "-n"]
    )
    assert args.component == "probe" and args.argv == ["make", "-n"]


def test_mcp_development_tools_use_the_service(monkeypatch):
    from pg_play import mcp_server

    runner = Runner()
    monkeypatch.setattr(mcp_server, "_service", lambda: PgPlayService(runner))
    mcp_server.maker_dev_pin("/work/session", "000001")
    assert runner.calls[-1].arguments == ("dev", "pin", "/work/session", "000001")
    mcp_server.maker_dev_prune("/work/session", keep_last=3, apply="/work/prune.json")
    assert runner.calls[-1].arguments[-4:] == ("--keep-last", "3", "--apply", "/work/prune.json")
