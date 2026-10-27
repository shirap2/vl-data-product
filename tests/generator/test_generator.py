"""Tests for drive generation and writing. Units: cm, s, ns."""

from pathlib import Path
from typing import Any

import pytest

from vl_data.core.config import Config
from vl_data.core.hashing import file_sha256, scenario_hash
from vl_data.core.manifest import MANIFEST_NAME, read_manifest, verify_drive
from vl_data.core.schemas import FRAMES_SCHEMA
from vl_data.generator import (
    Drive,
    drive_dir_name,
    generate,
    generate_to_disk,
    write_drive,
)
from vl_data.generator.models import Scenario, load_scenario

DEMO = Path(__file__).resolve().parents[2] / "src/vl_data/generator/scenarios/demo.yaml"
SPACING_NS = 62_500_000  # ns between frames at 16 fps


@pytest.fixture(scope="module")
def config(small_config: Config) -> Config:
    """Small-lattice config (16 fps, 100 cm cube, 10 points per axis)."""
    return small_config


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    """The demo scenario (1000 frames)."""
    return load_scenario(DEMO)


@pytest.fixture(scope="module")
def drive(scenario: Scenario, config: Config) -> Drive:
    """The generated demo drive."""
    return generate(scenario, config)


def _hashes(folder: Path) -> dict[str, str]:
    return {p.name: file_sha256(p) for p in sorted(folder.iterdir())}


def test_frame_timestamps_exact(drive: Drive, scenario: Scenario, config: Config) -> None:
    """1000 frames, spacing 62,500,000 ns, 1,000 points each."""
    frames = drive.frames
    assert frames.num_rows == 1000
    ts = frames.column("timestamp_ns").to_pylist()
    assert frames.column("frame_id").to_pylist() == list(range(1000))
    assert ts == [i * SPACING_NS for i in range(1000)]
    assert {len(p) for p in frames.column("points").to_pylist()} == {1000 * 12}
    assert drive.scenario_hash == scenario_hash(scenario, config.body)


def test_frames_use_the_shared_schema(drive: Drive) -> None:
    """The frames table carries the shared Arrow schema."""
    assert drive.frames.schema.equals(FRAMES_SCHEMA)


def test_same_scenario_same_bytes(scenario: Scenario, config: Config, tmp_path: Path) -> None:
    """Two runs give identical file hashes."""
    a = generate_to_disk(scenario, config, tmp_path / "a")
    b = generate_to_disk(scenario, config, tmp_path / "b")
    assert _hashes(a) == _hashes(b)
    assert set(_hashes(a)) == {"frames.parquet", MANIFEST_NAME}


def test_folder_is_hash12(drive: Drive, config: Config, tmp_path: Path) -> None:
    """Folder name is the first 12 hex of the manifest hash."""
    path = write_drive(drive, tmp_path, config)
    assert path == tmp_path / drive.scenario_hash[:12]
    assert path.name == read_manifest(path).scenario_hash[:12]
    assert verify_drive(path).ok


def test_rewrite_replaces_folder(drive: Drive, config: Config, tmp_path: Path) -> None:
    """Writing twice leaves one clean folder."""
    first = write_drive(drive, tmp_path, config)
    (first / "stale.txt").write_text("x")
    second = write_drive(drive, tmp_path, config)
    assert first == second
    assert [p.name for p in tmp_path.iterdir()] == [first.name]
    assert verify_drive(second).ok


def test_write_failure_cleans_temp(
    drive: Drive, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while writing leaves no temp folder and no drive folder."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("vl_data.generator.writer.write_manifest", boom)
    with pytest.raises(OSError, match="disk full"):
        write_drive(drive, tmp_path, config)
    assert list(tmp_path.iterdir()) == []


def test_failed_swap_keeps_old_folder(
    drive: Drive, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the final rename fails, the previous drive folder is restored and temps are removed."""
    first = write_drive(drive, tmp_path, config)
    before = _hashes(first)
    real_rename = Path.rename

    def flaky(self: Path, target: Any) -> Path:
        if self.name.startswith(".tmp-"):
            raise OSError("rename failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError, match="rename failed"):
        write_drive(drive, tmp_path, config)
    monkeypatch.undo()
    assert [p.name for p in tmp_path.iterdir()] == [first.name]
    assert _hashes(first) == before


def test_write_creates_missing_out_root(drive: Drive, config: Config, tmp_path: Path) -> None:
    """The output root is created when absent."""
    path = write_drive(drive, tmp_path / "new" / "root", config)
    assert verify_drive(path).ok


def test_generate_to_disk_raises_when_verify_fails(
    scenario: Scenario, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_to_disk raises if the written drive does not verify."""
    from vl_data.core.manifest import VerifyResult

    monkeypatch.setattr(
        "vl_data.generator.writer.verify_drive",
        lambda p: VerifyResult(ok=False, problems=["changed: frames.parquet"]),
    )
    with pytest.raises(RuntimeError, match="frames.parquet"):
        generate_to_disk(scenario, config, tmp_path)


def test_a_drive_holds_exactly_the_frames_and_the_manifest(
    scenario: Scenario, config: Config, drive: Drive, tmp_path: Path
) -> None:
    """Both write paths write two files and no ground truth, and the folder verifies."""
    streamed = generate_to_disk(scenario, config, tmp_path / "streamed")
    in_memory = write_drive(drive, tmp_path / "memory", config)
    for path in (streamed, in_memory):
        assert sorted(p.name for p in path.iterdir()) == ["frames.parquet", MANIFEST_NAME]
        assert sorted(read_manifest(path).files) == ["frames.parquet"]
        assert verify_drive(path).ok


def test_rewriting_replaces_a_folder_that_still_holds_an_old_ground_truth_file(
    scenario: Scenario, config: Config, tmp_path: Path
) -> None:
    """A folder from an older version, with its ground_truth.parquet, is replaced as a whole."""
    old = generate_to_disk(scenario, config, tmp_path)
    frames_before = (old / "frames.parquet").read_bytes()
    (old / "ground_truth.parquet").write_bytes(b"left over from an older version")
    again = generate_to_disk(scenario, config, tmp_path)
    assert again == old
    assert not (again / "ground_truth.parquet").exists()
    assert (again / "frames.parquet").read_bytes() == frames_before
    assert verify_drive(again).ok


def test_failed_swap_and_failed_restore_keep_old_drive(
    drive: Drive, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both the final rename and the restore fail, the .old-* folder keeps the old drive."""
    first = write_drive(drive, tmp_path, config)
    real_rename = Path.rename

    def flaky(self: Path, target: Any) -> Path:
        if self.name.startswith(".tmp-"):
            raise OSError("rename failed")
        if self.name == "old":
            raise OSError("restore failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError, match="rename failed") as info:
        write_drive(drive, tmp_path, config)
    monkeypatch.undo()
    asides = [p for p in tmp_path.iterdir() if p.name.startswith(".old-")]
    assert len(asides) == 1
    assert (asides[0] / "old" / "frames.parquet").is_file()
    assert not first.exists()
    assert any(str(asides[0]) in note for note in info.value.__notes__)


def test_target_that_is_a_file_is_rejected(drive: Drive, config: Config, tmp_path: Path) -> None:
    """A file at the drive path raises a clear error and nothing else is created."""
    (tmp_path / drive_dir_name(drive.scenario_hash)).write_text("x")
    with pytest.raises(NotADirectoryError, match="not a folder"):
        write_drive(drive, tmp_path, config)
    assert [p.name for p in tmp_path.iterdir()] == [drive_dir_name(drive.scenario_hash)]


def test_write_drive_rejects_a_config_with_another_body(
    drive: Drive, config: Config, tmp_path: Path
) -> None:
    """A drive made at one density cannot be written under a config of another density."""
    other = config.model_copy(
        update={"body": config.body.model_copy(update={"points_per_axis": 5})}
    )
    with pytest.raises(ValueError, match="different body"):
        write_drive(drive, tmp_path, other)
    assert list(tmp_path.iterdir()) == []


def test_failed_verification_never_replaces_a_good_folder(
    scenario: Scenario, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_drive runs on the temp folder before the swap: a bad drive leaves the old one."""
    from vl_data.core.manifest import VerifyResult

    good = generate_to_disk(scenario, config, tmp_path)
    before = _hashes(good)
    (good / "marker.txt").write_text("old")  # not in the manifest; a rewrite would remove it
    seen: list[Path] = []

    def bad(path: Path) -> VerifyResult:
        seen.append(path)
        return VerifyResult(ok=False, problems=["changed: frames.parquet"])

    monkeypatch.setattr("vl_data.generator.writer.verify_drive", bad)
    with pytest.raises(RuntimeError, match="frames.parquet"):
        generate_to_disk(scenario, config, tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [good.name]  # no temp or aside leftovers
    assert (good / "marker.txt").read_text() == "old"
    assert {k: v for k, v in _hashes(good).items() if k != "marker.txt"} == before
    assert seen[0].name.startswith(".tmp-")  # it checked the temp folder, not the final one
