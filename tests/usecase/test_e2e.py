"""Generate, ingest and compute motion for the 1000-frame demo scenario. Small body."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vl_data.cli import app
from vl_data.core.config import BodyConfig, Config
from vl_data.core.lake import Lake
from vl_data.generator.models import load_scenario
from vl_data.generator.writer import generate_to_disk
from vl_data.sdk.client import VLClient

runner = CliRunner()
DEMO = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "vl_data"
    / "generator"
    / "scenarios"
    / "demo.yaml"
)


def test_demo_drive_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)
    config = Config(body=BodyConfig(size_cm=100, points_per_axis=10))
    drive_dir = generate_to_disk(load_scenario(DEMO), config, tmp_path / "raw")
    lake_dir = str(tmp_path / "lake")

    ingest = runner.invoke(app, ["ingest", str(drive_dir), "--lake-dir", lake_dir])
    assert ingest.exit_code == 0, ingest.output
    motion = runner.invoke(app, ["motion", drive_dir.name, "--lake-dir", lake_dir])
    assert motion.exit_code == 0, motion.output
    assert "frames_read: 1000" in motion.output

    client = VLClient(Lake(tmp_path / "lake"))
    rows = client.sql("SELECT count(*) AS n FROM motion")
    assert rows.column("n")[0].as_py() == 1000
