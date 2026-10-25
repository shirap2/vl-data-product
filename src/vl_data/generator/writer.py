"""Write a drive to disk. Units: cm, s, ns."""

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from vl_data.core.config import Config
from vl_data.core.hashing import scenario_hash as compute_scenario_hash
from vl_data.core.manifest import BodyInfo, build_manifest, verify_drive, write_manifest
from vl_data.core.schemas import BYTES_PER_POINT, FRAMES_SCHEMA
from vl_data.generator.drive import (
    BATCH_FRAMES,
    Drive,
    build_ground_truth,
    check_in_memory_size,
    drive_dir_name,
    iter_frame_batches,
)
from vl_data.generator.models import Scenario

# Rows per parquet row group, for frames and ground truth, on both write paths. It equals the
# batch size so that the in-memory and the streaming writer give the same bytes.
ROW_GROUP_ROWS = BATCH_FRAMES
CODEC = "zstd"


def _parquet_writer(path: Path, schema: pa.Schema) -> pq.ParquetWriter:
    """A ParquetWriter with fixed, deterministic options.

    The row groups are fixed at ROW_GROUP_ROWS rows, so callers must write batches of exactly
    that many rows (the last one shorter) to get the same bytes.
    """
    return pq.ParquetWriter(
        path,
        schema,
        version="2.6",
        compression=CODEC,
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )


def _write_parquet(table: pa.Table, path: Path) -> None:
    """Write a table in fixed-size row groups of ROW_GROUP_ROWS rows."""
    with _parquet_writer(path, table.schema) as writer:
        for start in range(0, table.num_rows, ROW_GROUP_ROWS):
            writer.write_table(table.slice(start, ROW_GROUP_ROWS), row_group_size=ROW_GROUP_ROWS)


def _publish(
    out_root: Path,
    scenario_hash: str,
    scenario_json: dict[str, object],
    config: Config,
    write_files: Callable[[Path], int],
    verify: bool = False,
) -> Path:
    """Fill a temp folder, add the manifest and swap it in as <out_root>/<hash12>/.

    write_files(temp) writes frames.parquet and ground_truth.parquet into temp and returns the
    frame count. On any failure the temp folder is removed and an old drive folder is
    restored. config.body gives the cube edge in cm and the lattice points per axis (count);
    fps is in frames/s and file sizes in the manifest are in bytes. With verify, verify_drive
    runs on the temp folder before the swap and RuntimeError is raised if it fails, so a bad
    drive never replaces a good one (the files are hashed once more, a full re-read).
    """
    final = out_root / drive_dir_name(scenario_hash)
    if final.is_symlink() or (final.exists() and not final.is_dir()):
        raise NotADirectoryError(f"{final} exists and is not a folder; remove it first")
    out_root.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=out_root))
    aside: Path | None = None
    keep_aside = False
    try:
        frame_count = write_files(temp)
        body = BodyInfo(size_cm=config.body.size_cm, points_per_axis=config.body.points_per_axis)
        manifest = build_manifest(
            temp,
            scenario_hash,
            scenario_json,
            config.fps,
            frame_count,
            config.body.points_per_axis**3,
            body,
        )
        write_manifest(manifest, temp)
        if verify:
            result = verify_drive(temp)
            if not result.ok:
                raise RuntimeError(f"drive failed verification: {'; '.join(result.problems)}")
        if final.exists():
            aside = Path(tempfile.mkdtemp(prefix=".old-", dir=out_root))
            final.rename(aside / "old")
        temp.rename(final)
    except BaseException as exc:
        if aside is not None and not final.exists():
            try:
                (aside / "old").rename(final)
            except OSError:
                keep_aside = True  # the aside folder holds the only copy of the old drive
                exc.add_note(f"could not restore the old drive; it is kept at {aside / 'old'}")
        shutil.rmtree(temp, ignore_errors=True)
        raise
    finally:
        if aside is not None and not keep_aside:
            shutil.rmtree(aside, ignore_errors=True)
    return final


def write_drive(drive: Drive, out_root: Path, config: Config) -> Path:
    """Write an in-memory drive (frames, ground truth, manifest) to <out_root>/<hash12>/.

    The folder is built in a temp folder and swapped in. For large drives use
    generate_to_disk. Units: frames: count; timestamps ns; positions cm. Raises ValueError if
    the drive was generated with a different body than config.body (its hash differs), or when
    its points are over the in-memory limit (MAX_IN_MEMORY_BYTES in drive.py).
    """
    if drive.scenario_hash != compute_scenario_hash(drive.scenario, config.body):
        raise ValueError(
            "drive was generated with a different body than the config passed to write_drive"
        )
    frame_bytes = config.body.points_per_axis**3 * BYTES_PER_POINT  # bytes
    check_in_memory_size(int(drive.frames.num_rows), frame_bytes)

    def write_files(temp: Path) -> int:
        _write_parquet(drive.frames, temp / "frames.parquet")
        _write_parquet(drive.ground_truth, temp / "ground_truth.parquet")
        return int(drive.frames.num_rows)

    return _publish(
        out_root,
        drive.scenario_hash,
        drive.scenario.model_dump(mode="json"),
        config,
        write_files,
    )


def generate_to_disk(scenario: Scenario, config: Config, out_root: Path) -> Path:
    """Stream a scenario's frames to disk, write ground truth and manifest, then verify.

    Frames go from iter_frame_batches into one ParquetWriter, one row group of ROW_GROUP_ROWS
    (50) frames per batch, so memory stays near one batch. The result is byte-identical to
    write_drive(generate(...)). verify_drive checks the temp folder before it is swapped in;
    RuntimeError if it fails, and then an existing folder is left untouched. Units: frames:
    count; timestamps ns; positions cm.
    """

    def write_files(temp: Path) -> int:
        with _parquet_writer(temp / "frames.parquet", FRAMES_SCHEMA) as writer:
            for batch in iter_frame_batches(scenario, config):
                writer.write_table(pa.Table.from_batches([batch]), row_group_size=ROW_GROUP_ROWS)
        truth = build_ground_truth(scenario, config)
        _write_parquet(truth, temp / "ground_truth.parquet")
        return int(truth.num_rows)

    return _publish(
        out_root,
        compute_scenario_hash(scenario, config.body),
        scenario.model_dump(mode="json"),
        config,
        write_files,
        verify=True,
    )
