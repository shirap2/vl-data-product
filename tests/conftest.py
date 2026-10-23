"""Shared pytest fixtures."""

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    """A Typer CliRunner for invoking the root CLI in-process."""
    return CliRunner()
