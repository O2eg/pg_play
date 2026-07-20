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
    assert capsys.readouterr().out == "pg-play 0.1.0\n"
