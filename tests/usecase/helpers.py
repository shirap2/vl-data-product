"""Helpers shared by the use-case tests. Units: cm, cm/s, cm/s^2, frames, s."""

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import numpy as np
from numpy.typing import NDArray

from vl_data.core.config import BodyConfig, Config
from vl_data.core.lake import Lake
from vl_data.etl.config import EtlConfig
from vl_data.etl.ingest import ingest_drive
from vl_data.generator.models import Scenario
from vl_data.generator.writer import generate_to_disk
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.pipeline import compute_motion

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
KINK_FRAMES = (32, 96, 144, 224, 256)  # frames where the acceleration changes
FRAME_COUNT = 288  # frames


@dataclass(frozen=True)
class MadeDrive:
    """A drive on disk, ingested into a temporary lake, with its motion table written."""

    drive_dir: Path
    drive_id: str
    lake: Lake
    client: VLClient
    config: Config
    motion_cfg: MotionConfig
    frame_count: int  # frames
    kink_frames: tuple[int, ...]  # frames where the true acceleration changes


def build_drive(root: Path) -> MadeDrive:
    """Generate a small drive under root/raw, ingest it into root/lake and compute motion.

    Adapt this one function when the scenario, body or ingest call changes.
    """
    motion_cfg = MotionConfig()
    config = Config(body=BodyConfig(size_cm=100, points_per_axis=10))
    drive_dir = generate_to_disk(Scenario.model_validate(SCENARIO), config, root / "raw")
    lake = Lake(root / "lake")
    result = ingest_drive(drive_dir, lake, EtlConfig())
    assert result.status == "ok"
    client = VLClient(lake)
    compute_motion(client, lake, drive_dir.name, config, motion_cfg)
    return MadeDrive(
        drive_dir, drive_dir.name, lake, client, config, motion_cfg, FRAME_COUNT, KINK_FRAMES
    )


def interior_from_truth(
    ids: NDArray[np.int64], acceleration: NDArray[np.float64], half_window: float
) -> NDArray[np.bool_]:
    """Independent interior mask: no acceleration change within half_window frames; not last.

    ids: ordered frame ids; acceleration: true acceleration in cm/s^2 per frame.
    """
    changed_at = [int(ids[i]) for i in range(1, ids.size) if acceleration[i] != acceleration[i - 1]]
    mask = np.array(
        [all(abs(int(f) - k) > half_window for k in changed_at) for f in ids], dtype=bool
    )
    mask[-1] = False
    return mask


def max_rms(estimate: NDArray[np.float64], truth: NDArray[np.float64]) -> tuple[float, float]:
    """Independent max and RMS of estimate minus truth (unit of the inputs)."""
    diff = estimate - truth
    return float(np.abs(diff).max()), float(np.sqrt((diff**2).mean()))


def load_sweep_tool() -> ModuleType:
    """Import tools/motion_sweep.py (tools/ is not a package) once and return the module.

    The module has its own name in sys.modules; the entry is removed again if the import fails.
    """
    name = "vl_tools_motion_sweep"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[2] / "tools" / "motion_sweep.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up by name
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module
