"""Constants and the drive record shared by the BI tests. Units: cm, cm/s, cm/s^2, frames, s."""

from dataclasses import dataclass
from pathlib import Path

from vl_data.core.lake import Lake
from vl_data.etl.ingest import IngestResult
from vl_data.sdk.client import VLClient

# 288 frames at 16 fps: stop, ramp up, cruise, reversal through zero, slow to a stop, stop.
SCENARIO = {
    "seed": 0,
    "start": {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0},
    "segments": [
        {"type": "stop", "duration_s": 2.0},
        {"type": "move", "duration_s": 4.0, "end_speed_cm_s": 300.0},
        {"type": "move", "duration_s": 3.0, "end_speed_cm_s": 300.0},
        {"type": "move", "duration_s": 5.0, "end_speed_cm_s": -200.0},
        {"type": "move", "duration_s": 2.0, "end_speed_cm_s": 0.0},
        {"type": "stop", "duration_s": 2.0},
    ],
}
DROPPED_FRAME = 5  # frame id removed from the faulty drive


@dataclass(frozen=True)
class BiDrive:
    """A drive on disk, ingested into its own lake under root."""

    root: Path
    drive_dir: Path  # raw drive folder, root/raw/<drive id>
    drive_id: str
    lake: Lake
    client: VLClient
    ingest: IngestResult
    has_motion: bool
