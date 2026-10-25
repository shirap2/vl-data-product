"""Body geometry of a drive, taken from its manifest."""

from vl_data.core.config import BodyConfig
from vl_data.core.manifest import Manifest
from vl_data.generator.models import Scenario
from vl_data.generator.motion import frame_count


def drive_body(manifest: Manifest) -> BodyConfig:
    """Return the body a drive was generated with, read from manifest.body.

    Raises ValueError (pydantic's ValidationError is one) if the body is not a valid lattice
    (points_per_axis below 2).
    """
    return BodyConfig.model_validate(
        {"size_cm": manifest.body.size_cm, "points_per_axis": manifest.body.points_per_axis}
    )


def expected_frame_count(manifest: Manifest) -> int:
    """Frames the drive's scenario implies at the manifest fps (frames).

    Never use manifest.frame_count for this: it counts the rows of the file, which a duplicate
    or dropped frame changes. The scenario in the manifest is covered by the verified scenario
    hash. Raises ValueError (pydantic's ValidationError is one) if the scenario does not validate.
    """
    return frame_count(Scenario.model_validate(manifest.scenario), manifest.fps)
