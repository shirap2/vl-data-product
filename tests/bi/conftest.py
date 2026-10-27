"""Fixtures for the BI tests: small ingested drives. Units: cm, cm/s, cm/s^2, frames, s."""

from pathlib import Path

import pytest

from vl_data.core.config import BodyConfig, Config
from vl_data.core.lake import Lake
from vl_data.etl.config import EtlConfig
from vl_data.etl.ingest import ingest_drive
from vl_data.generator.drive import generate
from vl_data.generator.faults import CorruptPoints, DropFrame, inject_faults
from vl_data.generator.models import Scenario
from vl_data.generator.writer import generate_to_disk, write_drive
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.pipeline import compute_motion

from .helpers import DROPPED_FRAME, SCENARIO, BiDrive


@pytest.fixture(scope="module")
def bi_config() -> Config:
    """Small body: 10 points per axis (1,000 points per frame), 100 cm cube."""
    return Config(body=BodyConfig(size_cm=100, points_per_axis=10))


def _ingest(root: Path, drive_dir: Path, config: Config, *, motion: bool = True) -> BiDrive:
    lake = Lake(root / "lake")
    result = ingest_drive(drive_dir, lake, EtlConfig())
    client = VLClient(lake)
    has_motion = motion and result.status == "ok"
    if has_motion:
        compute_motion(client, lake, drive_dir.name, config, MotionConfig())
    return BiDrive(root, drive_dir, drive_dir.name, lake, client, result, has_motion)


@pytest.fixture(scope="module")
def clean(tmp_path_factory: pytest.TempPathFactory, bi_config: Config) -> BiDrive:
    """A clean drive, ingested, with its motion table."""
    root = tmp_path_factory.mktemp("clean")
    drive_dir = generate_to_disk(Scenario.model_validate(SCENARIO), bi_config, root / "raw")
    return _ingest(root, drive_dir, bi_config)


@pytest.fixture(scope="module")
def dropped(tmp_path_factory: pytest.TempPathFactory, bi_config: Config) -> BiDrive:
    """The same scenario with frame 5 dropped (VL-001), in its own lake, with motion."""
    root = tmp_path_factory.mktemp("dropped")
    drive, _ = inject_faults(
        generate(Scenario.model_validate(SCENARIO), bi_config), [DropFrame(DROPPED_FRAME)]
    )
    drive_dir = write_drive(drive, root / "raw", bi_config)
    return _ingest(root, drive_dir, bi_config)


@pytest.fixture(scope="module")
def rejected(tmp_path_factory: pytest.TempPathFactory, bi_config: Config) -> BiDrive:
    """A drive whose frames file changed after it was written: rejected, no keypoints."""
    root = tmp_path_factory.mktemp("rejected")
    drive_dir = generate_to_disk(Scenario.model_validate(SCENARIO), bi_config, root / "raw")
    with (drive_dir / "frames.parquet").open("ab") as handle:
        handle.write(b"x")  # the file hash no longer matches the manifest
    return _ingest(root, drive_dir, bi_config)


@pytest.fixture(scope="module")
def unmotioned(tmp_path_factory: pytest.TempPathFactory, bi_config: Config) -> BiDrive:
    """A clean drive, ingested (status ok) in its own lake, with no motion table."""
    root = tmp_path_factory.mktemp("unmotioned")
    drive_dir = generate_to_disk(Scenario.model_validate(SCENARIO), bi_config, root / "raw")
    return _ingest(root, drive_dir, bi_config, motion=False)


@pytest.fixture(scope="module")
def multi_fault(tmp_path_factory: pytest.TempPathFactory, bi_config: Config) -> BiDrive:
    """Frame 5 dropped (VL-001, no row); frames 20 and 30 with a short cloud (VL-004)."""
    root = tmp_path_factory.mktemp("multi_fault")
    drive, _ = inject_faults(
        generate(Scenario.model_validate(SCENARIO), bi_config),
        [DropFrame(DROPPED_FRAME), CorruptPoints(20), CorruptPoints(30)],
    )
    drive_dir = write_drive(drive, root / "raw", bi_config)
    return _ingest(root, drive_dir, bi_config)
