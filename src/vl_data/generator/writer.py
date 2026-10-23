"""Write a drive to disk. Units: cm, s, ns."""

import shutil
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from vl_data.core.config import Config
from vl_data.core.manifest import build_manifest, verify_drive, write_manifest
from vl_data.generator.drive import Drive, drive_dir_name, generate
from vl_data.generator.models import Scenario

ROW_GROUP_ROWS = 1000  # rows per parquet row group; fixed so the bytes never depend on size


def _write_parquet(table: pa.Table, path: Path) -> None:
    """Write a table with fixed, deterministic parquet options in fixed-size row groups."""
    with pq.ParquetWriter(
        path,
        table.schema,
        version="2.6",
        compression="snappy",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    ) as writer:
        for start in range(0, table.num_rows, ROW_GROUP_ROWS):
            writer.write_table(table.slice(start, ROW_GROUP_ROWS), row_group_size=ROW_GROUP_ROWS)


def write_drive(drive: Drive, out_root: Path, config: Config) -> Path:
    """Write frames, ground truth and manifest to <out_root>/<hash12>/ via a temp folder swap.

    Units: frames: count; timestamps ns; positions cm. Nothing here is a physical quantity.
    """
    final = out_root / drive_dir_name(drive.scenario_hash)
    if final.is_symlink() or (final.exists() and not final.is_dir()):
        raise NotADirectoryError(f"{final} exists and is not a folder; remove it first")
    out_root.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=out_root))
    aside: Path | None = None
    keep_aside = False
    try:
        _write_parquet(drive.frames, temp / "frames.parquet")
        _write_parquet(drive.ground_truth, temp / "ground_truth.parquet")
        manifest = build_manifest(
            temp,
            drive.scenario_hash,
            drive.scenario.model_dump(mode="json"),
            config.fps,
            drive.frames.num_rows,
            config.body.points_per_axis**3,
        )
        write_manifest(manifest, temp)
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


def generate_to_disk(scenario: Scenario, config: Config, out_root: Path) -> Path:
    """generate + write_drive + verify_drive; raise if verification fails.

    Units: frames: count; timestamps ns; positions cm.
    """
    path = write_drive(generate(scenario, config), out_root, config)
    result = verify_drive(path)
    if not result.ok:
        raise RuntimeError(f"drive failed verification: {'; '.join(result.problems)}")
    return path
