"""Tests for the root CLI (version and help)."""

import runpy
import sys

import pytest
from typer.testing import CliRunner

from vl_data.cli import app


def test_version_prints(runner: CliRunner) -> None:
    """--version prints the package name and version, then exits 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "vl_data 0.1.0" in result.output.splitlines()


def test_help_runs(runner: CliRunner) -> None:
    """--help exits 0 and shows the usage text with the --version option."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "--version" in result.output


def test_no_args_prints_help_and_exits_2(runner: CliRunner) -> None:
    """Running with no arguments prints the usage text and exits 2 (no_args_is_help)."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in result.output


def test_module_entry_point_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m vl_data --version` path (the __main__ module) prints the version, exits 0."""
    monkeypatch.setattr(sys, "argv", ["vl_data", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("vl_data", run_name="__main__")
    assert exc.value.code == 0
    assert "vl_data 0.1.0" in capsys.readouterr().out.splitlines()
