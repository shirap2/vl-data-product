"""Drive manifest: build, write, read and verify. Sizes in bytes, times in ns."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from vl_data.core.hashing import drive_hash, file_sha256
from vl_data.core.schemas import SCHEMA_VERSION

MANIFEST_NAME = "manifest.json"


class FileEntry(BaseModel):
    """SHA-256 and size of one data file."""

    sha256: str
    bytes: int


class BodyInfo(BaseModel):
    """The simulated body of a drive: cube edge (size_cm, cm, 1 to 2**31 - 1) and lattice
    points per axis (count, 1 to 1,000,000); out-of-range values are rejected."""

    size_cm: int = Field(gt=0, le=2_147_483_647)  # cube edge, cm (int32 range)
    points_per_axis: int = Field(gt=0, le=1_000_000)  # lattice points per axis (count)


class Manifest(BaseModel):
    """What a drive folder must contain, and the scenario that made it (schema version 2)."""

    schema_version: int
    scenario_hash: str
    seed: int
    fps: int
    frame_count: int
    points_per_frame: int
    body: BodyInfo
    scenario: dict[str, object]
    files: dict[str, FileEntry]

    @model_validator(mode="after")
    def _points_match_body(self) -> "Manifest":
        """points_per_frame (points) must equal points_per_axis (count) cubed.

        BodyInfo bounds points_per_axis (field validation runs first), so the cube is never
        computed on a huge or non-positive value.
        """
        expected = self.body.points_per_axis**3
        if self.points_per_frame != expected:
            raise ValueError(
                f"points_per_frame {self.points_per_frame} != body.points_per_axis cubed "
                f"({expected})"
            )
        return self


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of verify_drive: ok only when problems is empty."""

    ok: bool
    problems: list[str]


def build_manifest(
    drive_dir: Path,
    scenario_hash: str,
    scenario: dict[str, object],
    fps: int,
    frame_count: int,
    points_per_frame: int,
    body: BodyInfo,
) -> Manifest:
    """Hash every data file in drive_dir (bytes on disk) and build the manifest.

    Files are keyed by name, sorted; manifest.json itself is skipped. A subdirectory
    raises ValueError. fps is in frames/s; body is the cube edge (cm) and lattice; sizes are in
    bytes. A folder that cannot be listed or read raises OSError. points_per_frame must equal
    body.points_per_axis cubed (pydantic ValidationError, a ValueError, otherwise).
    """
    files: dict[str, FileEntry] = {}
    for path in sorted(drive_dir.iterdir(), key=lambda p: p.name):
        if path.name == MANIFEST_NAME:
            continue
        if not path.is_file():
            raise ValueError(f"unexpected non-file entry in drive folder: {path.name}")
        files[path.name] = FileEntry(sha256=file_sha256(path), bytes=path.stat().st_size)
    seed = scenario.get("seed", 0)
    if type(seed) is not int:
        raise ValueError("scenario seed must be an int")
    return Manifest(
        schema_version=SCHEMA_VERSION,
        scenario_hash=scenario_hash,
        seed=seed,
        fps=fps,
        frame_count=frame_count,
        points_per_frame=points_per_frame,
        body=body,
        scenario=scenario,
        files=files,
    )


def write_manifest(manifest: Manifest, drive_dir: Path) -> Path:
    """Write manifest.json (sorted keys, indent 2, UTF-8, LF newline) and return its path.

    Raises ValueError for NaN or infinity and TypeError for a non-JSON value in the scenario;
    nothing is written in either case.
    """
    text = json.dumps(
        manifest.model_dump(),  # python mode keeps NaN so allow_nan=False can reject it
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    )
    path = drive_dir / MANIFEST_NAME
    path.write_bytes((text + "\n").encode("utf-8"))
    return path


def read_manifest(drive_dir: Path) -> Manifest:
    """Read and validate manifest.json.

    Raises FileNotFoundError if absent and pydantic ValidationError if invalid.
    """
    path = drive_dir / MANIFEST_NAME
    return Manifest.model_validate_json(path.read_bytes())


def _listed_file_problems(drive_dir: Path, name: str, entry: FileEntry) -> list[str]:
    """Problems with one file the manifest lists: invalid name, missing, or changed."""
    if name in ("", ".", "..") or name != Path(name).name:
        return [f"invalid name: {name}"]
    path = drive_dir / name
    if not path.is_file():
        return [f"missing: {name}"]
    problems: list[str] = []
    try:
        if path.stat().st_size != entry.bytes:
            problems.append(f"changed: {name} (size mismatch)")
        if file_sha256(path) != entry.sha256:
            problems.append(f"changed: {name} (sha256 mismatch)")
    except OSError as exc:
        problems.append(f"changed: {name} (unreadable: {exc})")
    return problems


def verify_drive(drive_dir: Path) -> VerifyResult:
    """Check every listed file's hash and size; report changed, missing and extra files.

    A bad manifest, a bad or missing listed file, an unreadable file and an unhashable
    scenario are reported as problems, not raised. A pathological listed name (hundreds of
    characters) or a folder that cannot be listed can still raise OSError from the
    filesystem. Problem strings are 'changed: <name> (...)', 'missing: <name>',
    'invalid name: <name>', 'extra: <name>', 'scenario hash does not match scenario' and
    'cannot read manifest.json: ...'.
    """
    try:
        manifest = read_manifest(drive_dir)
    except (OSError, ValueError) as exc:
        return VerifyResult(ok=False, problems=[f"cannot read {MANIFEST_NAME}: {exc}"])
    problems: list[str] = []
    for name, entry in sorted(manifest.files.items()):
        problems.extend(_listed_file_problems(drive_dir, name, entry))
    present = sorted(p.name for p in drive_dir.iterdir() if p.name != MANIFEST_NAME)
    problems.extend(f"extra: {name}" for name in present if name not in manifest.files)
    try:
        expected = drive_hash(manifest.scenario, manifest.body.model_dump())
        matches = expected == manifest.scenario_hash
    except (ValueError, TypeError, RecursionError):
        matches = False
    if not matches:
        problems.append("scenario hash does not match scenario")
    return VerifyResult(ok=not problems, problems=problems)
