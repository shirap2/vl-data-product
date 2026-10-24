"""Tests for the real point density, batching and the streaming writer.

Units: cm, ns; frames and points are counts. Full-density tests write one to three frames
only (a full frame is 12 MB).
"""

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from vl_data.cli import app
from vl_data.core.config import BodyConfig, Config, load_config
from vl_data.core.hashing import file_sha256
from vl_data.core.manifest import read_manifest, verify_drive
from vl_data.core.schemas import FRAMES_SCHEMA, decode_points, encode_points
from vl_data.generator import drive as drive_module
from vl_data.generator import generate, generate_to_disk, write_drive
from vl_data.generator.body import cube_lattice, place_body
from vl_data.generator.drive import BATCH_FRAMES, build_ground_truth, iter_frame_batches
from vl_data.generator.models import Scenario
from vl_data.generator.writer import ROW_GROUP_ROWS

ONE_FRAME = {"segments": [{"type": "stop", "duration_s": 0.0625}]}
# 120 frames: 32 + 48 + 40 at 16 frames/s, so the batches are 50, 50 and 20 frames
LONG = {
    "segments": [
        {"type": "stop", "duration_s": 2.0},
        {"type": "move", "duration_s": 3.0, "end_speed_cm_s": 100.0},
        {"type": "move", "duration_s": 2.5, "end_speed_cm_s": 0.0},
    ]
}
# 48 frames: a single batch
SHORT = {
    "segments": [
        {"type": "stop", "duration_s": 1.0},
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 100.0},
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
    ]
}


def _full_config() -> Config:
    return Config(body=BodyConfig(size_cm=100, points_per_axis=100))


def test_full_frame_has_a_million_points_and_the_eight_corners() -> None:
    config = _full_config()
    drive = generate(Scenario.model_validate(ONE_FRAME), config)
    assert drive.frames.num_rows == 1
    buf = drive.frames.column("points")[0].as_py()
    assert len(buf) == 1_000_000 * 12
    points = decode_points(buf)
    assert points.shape == (1_000_000, 3)
    lattice = cube_lattice(config.body.size_cm, config.body.points_per_axis)
    on_edge = np.isin(lattice, [0, 99]).all(axis=1)
    corners = np.flatnonzero(on_edge)
    assert corners.tolist() == [0, 99, 9900, 9999, 990000, 990099, 999900, 999999]
    # the body is placed at the origin (x = y = z = 0), so the points equal the lattice
    assert np.array_equal(points[corners], lattice[corners])
    assert points[0].tolist() == [0, 0, 0]
    assert points[999999].tolist() == [99, 99, 99]


def test_shipped_density_fits_the_real_arrow_limit() -> None:
    """The shipped BodyConfig() batch of BATCH_FRAMES frames is below the real limit."""
    body = BodyConfig()
    frame_bytes = body.points_per_axis**3 * 12  # bytes
    assert ROW_GROUP_ROWS == BATCH_FRAMES
    assert BATCH_FRAMES * frame_bytes < drive_module._ARROW_BINARY_LIMIT
    assert drive_module._ARROW_BINARY_LIMIT == 2**31


def test_batch_limit_boundary_is_exact(
    small_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch of exactly the limit is rejected; one byte below it is accepted."""
    scenario = Scenario.model_validate(LONG)  # 120 frames, more than one batch
    frame_bytes = small_config.body.points_per_axis**3 * 12  # bytes
    batch_bytes = BATCH_FRAMES * frame_bytes
    monkeypatch.setattr(drive_module, "_ARROW_BINARY_LIMIT", batch_bytes + 1)
    first = next(iter_frame_batches(scenario, small_config))
    assert first.num_rows == BATCH_FRAMES
    monkeypatch.setattr(drive_module, "_ARROW_BINARY_LIMIT", batch_bytes)
    with pytest.raises(ValueError, match="too large"):
        next(iter_frame_batches(scenario, small_config))


def test_every_row_of_a_batch_is_the_placed_body(small_config: Config) -> None:
    """Each frame equals encode_points(place_body(lattice, x_k, y, z)) for a moving body."""
    scenario = Scenario.model_validate(
        {
            "seed": 0,
            "start": {"x_cm": 12.0, "y_cm": 5.0, "z_cm": -7.0, "speed_cm_s": 40.0},
            "segments": [{"type": "move", "duration_s": 1.0, "end_speed_cm_s": 120.0}],
        }
    )
    batch = next(iter_frame_batches(scenario, small_config))
    x_cm = build_ground_truth(scenario, small_config).column("x_cm").to_numpy()
    lattice = cube_lattice(small_config.body.size_cm, small_config.body.points_per_axis)
    assert batch.num_rows >= 3
    assert x_cm[1] != x_cm[0] and x_cm[2] != x_cm[1]
    points = batch.column("points")
    for k in range(batch.num_rows):
        expected = encode_points(place_body(lattice, float(x_cm[k]), 5.0, -7.0))
        assert points[k].as_py() == expected, k


def test_batch_over_the_arrow_limit_is_rejected_before_allocating() -> None:
    config = Config(body=BodyConfig(size_cm=200, points_per_axis=200))  # 96 MB per frame
    scenario = Scenario.model_validate(ONE_FRAME)
    with pytest.raises(ValueError, match="too large"):
        next(iter_frame_batches(scenario, config))


def test_batches_are_at_most_batch_frames_and_cover_every_frame(small_config: Config) -> None:
    scenario = Scenario.model_validate(LONG)
    batches = list(iter_frame_batches(scenario, small_config))
    assert [b.num_rows for b in batches] == [50, 50, 20]
    assert all(b.schema.equals(FRAMES_SCHEMA) for b in batches)
    ids = [i for b in batches for i in b.column("frame_id").to_pylist()]
    assert ids == list(range(120))
    drive = generate(scenario, small_config)
    assert drive.frames.equals(pa.Table.from_batches(batches, schema=FRAMES_SCHEMA))
    assert build_ground_truth(scenario, small_config).equals(drive.ground_truth)


@pytest.mark.parametrize("spec", [SHORT, LONG], ids=["48_frames", "120_frames"])
def test_generate_and_streaming_writer_give_identical_bytes(
    spec: dict[str, Any], small_config: Config, tmp_path: Path
) -> None:
    scenario = Scenario.model_validate(spec)
    in_memory = write_drive(generate(scenario, small_config), tmp_path / "a", small_config)
    streamed = generate_to_disk(scenario, small_config, tmp_path / "b")
    assert in_memory.name == streamed.name
    for name in ("frames.parquet", "ground_truth.parquet", "manifest.json"):
        assert file_sha256(in_memory / name) == file_sha256(streamed / name), name
    assert verify_drive(streamed).ok


def test_streaming_writer_layout(small_config: Config, tmp_path: Path) -> None:
    """Row groups of 50 rows, zstd, for both files; the manifest carries the body."""
    path = generate_to_disk(Scenario.model_validate(LONG), small_config, tmp_path)
    for name in ("frames.parquet", "ground_truth.parquet"):
        meta = pq.ParquetFile(path / name).metadata
        assert meta.num_rows == 120
        assert [meta.row_group(i).num_rows for i in range(meta.num_row_groups)] == [50, 50, 20]
        assert meta.row_group(0).column(0).compression == "ZSTD"
    manifest = read_manifest(path)
    assert manifest.frame_count == 120
    assert manifest.points_per_frame == 1000
    assert (manifest.body.size_cm, manifest.body.points_per_axis) == (100, 10)


def test_streaming_writer_reads_batches_lazily(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_to_disk pulls the batches from iter_frame_batches, 50 frames at a time."""
    import vl_data.generator.writer as writer

    events: list[str] = []
    real = writer.iter_frame_batches
    real_write = pq.ParquetWriter.write_table

    def spy(scenario: Scenario, config: Config) -> Any:
        iterator = real(scenario, config)
        while True:
            events.append("pull")
            batch = next(iterator, None)
            if batch is None:
                return
            events.append(f"batch {batch.num_rows}")
            yield batch

    def spy_write(self: pq.ParquetWriter, table: pa.Table, *args: Any, **kwargs: Any) -> None:
        if table.schema.equals(FRAMES_SCHEMA):
            events.append(f"write {table.num_rows}")
        real_write(self, table, *args, **kwargs)

    monkeypatch.setattr(writer, "iter_frame_batches", spy)
    monkeypatch.setattr(pq.ParquetWriter, "write_table", spy_write)
    generate_to_disk(Scenario.model_validate(LONG), small_config, tmp_path)
    assert events == [
        "pull", "batch 50", "write 50",
        "pull", "batch 50", "write 50",
        "pull", "batch 20", "write 20",
        "pull",
    ]  # fmt: skip


def test_streaming_failure_midway_leaves_nothing(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vl_data.generator.writer as writer

    real = writer.iter_frame_batches

    def failing(scenario: Scenario, config: Config) -> Any:
        yield next(real(scenario, config))
        raise OSError("disk full")

    monkeypatch.setattr(writer, "iter_frame_batches", failing)
    with pytest.raises(OSError, match="disk full"):
        generate_to_disk(Scenario.model_validate(LONG), small_config, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_two_densities_give_two_hashes(small_config: Config) -> None:
    scenario = Scenario.model_validate(ONE_FRAME)
    small = generate(scenario, small_config)
    full = generate(scenario, _full_config())
    assert small.scenario_hash != full.scenario_hash
    assert small.scenario_hash[:12] != full.scenario_hash[:12]


def test_default_yaml_is_full_density() -> None:
    config = load_config()
    assert (config.body.size_cm, config.body.points_per_axis) == (100, 100)


def test_cli_generate_at_full_density_for_two_frames(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped config (100 points per axis) through the CLI, for a 2-frame scenario."""
    monkeypatch.setattr("vl_data.generator.cli.load_config", load_config)
    path = tmp_path / "two.yaml"
    path.write_text("segments:\n  - {type: stop, duration_s: 0.125}\n", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, ["generate", "--scenario", str(path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "frames: 2" in lines
    assert "points_per_frame: 1000000" in lines
    assert "manifest: verified" in lines
    manifest = read_manifest(next(out.iterdir()))
    assert manifest.body.points_per_axis == 100
    assert manifest.files["frames.parquet"].bytes < 2_000_000  # zstd; 24 MB raw
