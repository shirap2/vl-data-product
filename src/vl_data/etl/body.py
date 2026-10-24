"""Body geometry of a drive, taken from its manifest."""

from vl_data.core.config import BodyConfig
from vl_data.core.manifest import Manifest


def drive_body(manifest: Manifest, fallback: BodyConfig) -> BodyConfig:
    """Return the body a drive was generated with.

    points_per_axis is the integer cube root of manifest.points_per_frame (it must be an
    exact cube >= 2, else ValueError); size_cm comes from fallback. A future manifest body
    key will take precedence over both, and this function is the only place that must change.
    """
    points = manifest.points_per_frame
    try:
        n = round(points ** (1 / 3)) if points > 0 else 0
    except OverflowError:  # an absurdly large count from a corrupt manifest
        n = 0
    if n < 2 or n**3 != points:
        raise ValueError(f"points_per_frame must be a cube n**3 with n >= 2, got {points}")
    return BodyConfig.model_validate({"size_cm": fallback.size_cm, "points_per_axis": n})
