"""The motion command, through the root app. Small body (100 cm, 10 per axis)."""

import re
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from click.testing import Result
from typer.testing import CliRunner

from vl_data.cli import app
from vl_data.core.lake import Lake
from vl_data.usecase import cli as usecase_cli
from vl_data.usecase import config as motion_config

from .helpers import MadeDrive

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_env_lake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)


def _run(args: list[str]) -> Result:
    """Invoke the root app; no path may print a traceback."""
    result = runner.invoke(app, args)
    assert "Traceback" not in result.output
    return result


def _motion(drive: MadeDrive) -> Result:
    return _run(["motion", drive.drive_id, "--lake-dir", str(drive.lake.root)])


def _value(output: str, key: str) -> str:
    match = re.search(rf"^{key}: (.*)$", output, re.MULTILINE)
    assert match is not None, output
    return match.group(1)


def _null_frames(table: pa.Table) -> int:
    """Frames of a motion table whose velocity or acceleration is null."""
    either = pc.or_(
        pc.is_null(table.column("velocity_cm_s")), pc.is_null(table.column("acceleration_cm_s2"))
    )
    return int(pc.sum(either).as_py())


def test_help_lists_the_motion_command_and_not_evaluate() -> None:
    result = _run(["--help"])
    assert result.exit_code == 0
    assert "motion" in result.output and "evaluate" not in result.output
    sub = _run(["motion", "--help"])
    assert sub.exit_code == 0
    assert "--lake-dir" in sub.output
    assert "ingest" in " ".join(sub.output.split())  # help says it mirrors the ingest codes
    assert _run(["evaluate", "--help"]).exit_code == 2  # the command no longer exists


def test_motion_prints_counts(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    result = _motion(drive)
    assert result.exit_code == 0, result.output
    out = result.output
    read = int(_value(out, "frames_read").split()[0])
    estimated = int(_value(out, "frames_estimated").split()[0])
    null = int(_value(out, "frames_null").split()[0])
    table = drive.client.motion(drive.drive_id)
    assert read == drive.frame_count == table.num_rows
    assert estimated + null == read
    assert null == _null_frames(table)
    assert _value(out, "frames_null").startswith(f"{null} frames")
    assert _value(out, "frames_read") == f"{read} frames"


def test_motion_prints_nonzero_null_count(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    rows = drive.client.keypoints(drive.drive_id)
    frame_id = rows.column("frame_id")
    keep = pc.or_(pc.less(frame_id, 100), pc.equal(frame_id, 150))  # frame 150 stands alone
    drive.lake.swap_partition("keypoints", drive.drive_id, rows.filter(keep).to_batches())
    result = _motion(drive)
    assert result.exit_code == 0, result.output
    null = int(_value(result.output, "frames_null").split()[0])
    assert null > 0
    assert int(_value(result.output, "frames_read").split()[0]) == 101
    assert null == _null_frames(drive.client.motion(drive.drive_id))


def test_output_contains_units(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    assert f"frames_read: {drive.frame_count} frames" in _motion(drive).output.splitlines()


def test_motion_unknown_drive_exits_1(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    result = _run(["motion", "000000000000", "--lake-dir", str(drive.lake.root)])
    assert result.exit_code == 1
    assert "lineage" in result.output


def test_motion_bad_arguments_exit_2(make_drive: Callable[[], MadeDrive], tmp_path: Path) -> None:
    drive = make_drive()
    missing = tmp_path / "nowhere"
    result = _run(["motion", drive.drive_id, "--lake-dir", str(missing)])
    assert result.exit_code == 2
    assert not missing.exists()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    result = _run(["motion", drive.drive_id, "--lake-dir", str(a_file)])
    assert result.exit_code == 2
    result = _run(["motion", "bad/id", "--lake-dir", str(drive.lake.root)])
    assert result.exit_code == 2


def test_lake_dir_from_environment(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()
    monkeypatch.setenv("VL_LAKE_DIR", str(drive.lake.root))
    result = _run(["motion", drive.drive_id])
    assert result.exit_code == 0, result.output


def test_bad_motion_config_exits_2(
    make_drive: Callable[[], MadeDrive], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()
    bad = tmp_path / "motion.yaml"
    bad.write_text("window_frames: 2\n", encoding="utf-8")
    monkeypatch.setattr(motion_config, "DEFAULT_MOTION_CONFIG_PATH", bad)
    assert _motion(drive).exit_code == 2


def test_io_errors_exit_3(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch compute_motion to raise OSError, simulating an unexpected file failure."""
    drive = make_drive()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(usecase_cli, "compute_motion", boom)
    result = _motion(drive)
    assert result.exit_code == 3
    assert "disk gone" not in result.output


def test_arrow_error_exits_3(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise pa.ArrowInvalid("bad parquet")

    monkeypatch.setattr(usecase_cli, "compute_motion", boom)
    assert _motion(drive).exit_code == 3


def test_drive_id_rule_equals_the_lake_rule(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    names = ["abc123", "a-b_C", "", "a/b", "a b", "a.b", "..", "x" * 80]
    names += ["a" + chr(92) + "b", "ab" + chr(10), chr(233)]
    for name in names:
        try:
            lake.read("motion", name)
            lake_accepts = True
        except ValueError:
            lake_accepts = False
        assert bool(usecase_cli._DRIVE_ID.fullmatch(name)) == lake_accepts, repr(name)


def test_motion_bad_drive_id_exits_2_before_anything(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()

    def never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("compute_motion must not run")

    monkeypatch.setattr(usecase_cli, "compute_motion", never)
    for name in ("a/b", "a b", "..", "x.y"):
        result = _run(["motion", name, "--lake-dir", str(drive.lake.root)])
        assert result.exit_code == 2, name


def test_motion_other_failures_exit_1_with_type_name(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()
    for exc in (ValueError("secret detail"), RuntimeError("secret detail")):

        def boom(*_args: object, _exc: Exception = exc, **_kwargs: object) -> None:
            raise _exc

        monkeypatch.setattr(usecase_cli, "compute_motion", boom)
        result = _motion(drive)
        assert result.exit_code == 1
        assert type(exc).__name__ in result.output
        assert "secret detail" not in result.output
