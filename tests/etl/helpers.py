"""Plain helpers shared by the ETL tests. Units: cm, ns."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from vl_data.core.config import Config
from vl_data.core.schemas import FRAMES_SCHEMA
from vl_data.generator.drive import Drive


@dataclass(frozen=True)
class Written:
    """A drive written to disk, with what the ETL scan and rules need."""

    drive_dir: Path
    frames_path: Path
    schema: pa.Schema
    offsets: NDArray[np.int32]  # lattice minus its first point, cm
    counts: dict[str, int]  # faults injected per code
    config: Config


def build_table(ids: list[int], ts: list[int], pts: list[bytes | None]) -> pa.Table:
    """A frames table with the FRAMES_SCHEMA from column lists (ts in ns)."""
    return pa.Table.from_arrays(
        [pa.array(ids), pa.array(ts), pa.array(pts, type=pa.binary())], schema=FRAMES_SCHEMA
    )


def columns(drive: Drive) -> tuple[list[int], list[int], list[bytes | None]]:
    """The frame_id, timestamp_ns and points columns of a drive as Python lists."""
    f = drive.frames
    return (
        f.column("frame_id").to_pylist(),
        f.column("timestamp_ns").to_pylist(),
        f.column("points").to_pylist(),
    )
