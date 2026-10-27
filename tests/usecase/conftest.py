"""Fixtures for the use-case tests: one built drive, shared or copied. Units: cm, s."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient

from .helpers import MadeDrive, build_drive


@pytest.fixture(scope="module")
def shared_drive(tmp_path_factory: pytest.TempPathFactory) -> MadeDrive:
    """A drive for read-only tests: never modify its files or lake."""
    return build_drive(tmp_path_factory.mktemp("shared"))


@pytest.fixture
def make_drive(shared_drive: MadeDrive, tmp_path: Path) -> Callable[[], MadeDrive]:
    """Return make(): a private copy of the shared drive and lake that a test may change."""
    counter = 0

    def make() -> MadeDrive:
        nonlocal counter
        counter += 1
        root = tmp_path / f"copy{counter}"
        drive_dir = root / "raw" / shared_drive.drive_id
        shutil.copytree(shared_drive.drive_dir, drive_dir)
        shutil.copytree(shared_drive.lake.root, root / "lake")
        lake = Lake(root / "lake")
        return MadeDrive(
            drive_dir,
            shared_drive.drive_id,
            lake,
            VLClient(lake),
            shared_drive.config,
            shared_drive.motion_cfg,
            shared_drive.frame_count,
            shared_drive.kink_frames,
        )

    return make
