"""Fixtures for the ETL frame-scan and rule tests. Units: cm, ns; fps in frames/s."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vl_data.core.config import BodyConfig, Config
from vl_data.core.manifest import read_manifest
from vl_data.etl.body import expected_frame_count
from vl_data.etl.config import LimitsConfig
from vl_data.etl.facts import scan_frames
from vl_data.etl.rules import RuleContext
from vl_data.generator.body import cube_lattice
from vl_data.generator.drive import Drive, generate
from vl_data.generator.faults import Fault, inject_faults
from vl_data.generator.models import Scenario, load_scenario
from vl_data.generator.writer import write_drive

from .helpers import Written

DEMO = Path(__file__).resolve().parents[2] / "src/vl_data/generator/scenarios/demo.yaml"


@pytest.fixture(scope="session")
def small_config() -> Config:
    """A 125-point body so tests stay fast."""
    return Config(body=BodyConfig(size_cm=40, points_per_axis=5))


@pytest.fixture(scope="session")
def demo_scenario() -> Scenario:
    """The demo scenario: 1000 frames, peak 600 cm/s."""
    return load_scenario(DEMO)


@pytest.fixture(scope="session")
def clean_drive(demo_scenario: Scenario, small_config: Config) -> Drive:
    """The unmodified demo drive on the small body."""
    return generate(demo_scenario, small_config)


@pytest.fixture
def write_faulty(
    tmp_path: Path, clean_drive: Drive, small_config: Config
) -> Callable[..., Written]:
    """Return write(faults, table=None): inject faults, or use a hand-built table, and write."""
    counter = 0

    def write(faults: Sequence[Fault] = (), table: pa.Table | None = None) -> Written:
        nonlocal counter
        counter += 1
        drive, counts = inject_faults(clean_drive, faults)
        if table is not None:
            drive = replace(drive, frames=table)
        out = write_drive(drive, tmp_path / f"drive{counter}", small_config)
        frames_path = out / "frames.parquet"
        lattice = cube_lattice(small_config.body.size_cm, small_config.body.points_per_axis)
        return Written(
            drive_dir=out,
            frames_path=frames_path,
            schema=pq.ParquetFile(frames_path).schema_arrow,
            offsets=(lattice - lattice[0]).astype(np.int32),
            counts=counts,
            config=small_config,
        )

    return write


@pytest.fixture
def make_ctx() -> Callable[..., RuleContext]:
    """Return ctx(written, limits=None, facts=None): scan the file (unless facts is given)."""

    def ctx(
        written: Written,
        limits: LimitsConfig | None = None,
        facts: pd.DataFrame | None = None,
    ) -> RuleContext:
        if facts is None:
            facts = scan_frames(written.frames_path, written.offsets, 256)
        return RuleContext(
            facts=facts,
            schema=written.schema,
            frame_count=expected_frame_count(read_manifest(written.drive_dir)),
            points_per_frame=written.offsets.shape[0],
            fps=written.config.fps,
            frame_interval_ns=written.config.frame_interval_ns,
            limits=limits or LimitsConfig(),
        )

    return ctx
