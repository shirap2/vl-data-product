"""Shared pytest fixtures."""

import pytest
from typer.testing import CliRunner

from vl_data.core.config import BodyConfig, Config


@pytest.fixture
def runner() -> CliRunner:
    """A Typer CliRunner for invoking the root CLI in-process."""
    return CliRunner()


@pytest.fixture(scope="session")
def small_config() -> Config:
    """Default config with a small lattice: 10 points per axis (1,000 per frame), 100 cm cube.

    The shipped default is the real density (100 per axis); tests use this one unless they
    check the full density.
    """
    return Config(body=BodyConfig(size_cm=100, points_per_axis=10))
