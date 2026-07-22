from __future__ import annotations

import pytest

from pg_play.cli import build_parser, main


def test_parser_uses_public_command_name() -> None:
    assert build_parser().prog == "pg-play"


def test_main_accepts_empty_arguments() -> None:
    assert main([]) == 0


def test_version_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "pg-play 0.2.0\n"


def test_join_and_teardown_cli_use_explicit_safe_arguments() -> None:
    parser = build_parser()
    join = parser.parse_args(
        [
            "join-benchmark-reports",
            "--report",
            "baseline.json",
            "--report",
            "candidate.json",
            "--join-task",
            "optimize-db-config",
            "--out",
            "joined",
            "--report-name",
            "comparison",
        ]
    )
    teardown = parser.parse_args(["teardown", "experiment.yaml", "--clear-stand-data"])

    assert join.report == ["baseline.json", "candidate.json"]
    assert join.out == "joined"
    assert teardown.clear_stand_data is True
