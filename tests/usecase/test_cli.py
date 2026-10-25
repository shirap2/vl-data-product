"""The motion and evaluate commands, through the root app. Small body (100 cm, 10 per axis)."""

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
from vl_data.usecase.evaluate import EvaluationError, evaluate_drive

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


def _evaluate(drive: MadeDrive) -> Result:
    return _run(["evaluate", str(drive.drive_dir), "--lake-dir", str(drive.lake.root)])


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


def test_help_lists_both_commands() -> None:
    result = _run(["--help"])
    assert result.exit_code == 0
    assert "motion" in result.output and "evaluate" in result.output
    for name in ("motion", "evaluate"):
        sub = _run([name, "--help"])
        assert sub.exit_code == 0
        assert "--lake-dir" in sub.output
        assert "ingest" in " ".join(sub.output.split())  # help says it mirrors the ingest codes


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
    out = _evaluate(drive).output
    for unit in ("(cm)", "(cm/s)", "(cm/s^2)"):
        assert unit in out
    assert "PASS" in out


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
    assert _evaluate(drive).exit_code == 2


def test_evaluate_exit_codes(
    make_drive: Callable[[], MadeDrive], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()
    passing = _evaluate(drive)
    assert passing.exit_code == 0, passing.output
    assert "PASS" in passing.output and "FAIL" not in passing.output
    assert _value(passing.output, "frames_compared") == f"{drive.frame_count} frames"

    tight = tmp_path / "motion.yaml"
    tight.write_text(
        "tolerances:\n  velocity_cm_s: 0.0001\n  acceleration_cm_s2: 0.0001\n", encoding="utf-8"
    )
    monkeypatch.setattr(motion_config, "DEFAULT_MOTION_CONFIG_PATH", tight)
    failing = _evaluate(drive)
    assert failing.exit_code == 1
    assert "FAIL" in failing.output
    assert "PASS" not in failing.output


def test_evaluate_without_motion_exits_1(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    drive.lake.drop_partition("motion", drive.drive_id)
    result = _evaluate(drive)
    assert result.exit_code == 1
    assert "run motion first" in result.output


def test_evaluate_altered_motion_exits_1(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    table = drive.client.motion(drive.drive_id)
    shifted = table.set_column(
        table.schema.get_field_index("velocity_cm_s"),
        "velocity_cm_s",
        pc.add(table.column("velocity_cm_s"), 500.0),
    )
    drive.lake.swap_partition("motion", drive.drive_id, shifted.to_batches())
    result = _evaluate(drive)
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_evaluate_bad_arguments_exit_2(make_drive: Callable[[], MadeDrive], tmp_path: Path) -> None:
    drive = make_drive()
    nowhere = str(tmp_path / "nowhere")
    result = _run(["evaluate", nowhere, "--lake-dir", str(drive.lake.root)])
    assert result.exit_code == 2
    result = _run(["evaluate", str(drive.drive_dir), "--lake-dir", nowhere])
    assert result.exit_code == 2
    assert not Path(nowhere).exists()


def test_io_errors_exit_3(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch compute_motion and evaluate_drive to raise OSError, simulating an unexpected failure.

    A real read error inside evaluate_drive is an EvaluationError (exit 1), so for evaluate this
    covers only a failure of evaluate_drive itself.
    """
    drive = make_drive()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(usecase_cli, "compute_motion", boom)
    result = _motion(drive)
    assert result.exit_code == 3
    assert "disk gone" not in result.output
    monkeypatch.setattr(usecase_cli, "evaluate_drive", boom)
    assert _evaluate(drive).exit_code == 3


def test_arrow_error_exits_3(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise pa.ArrowInvalid("bad parquet")

    monkeypatch.setattr(usecase_cli, "compute_motion", boom)
    assert _motion(drive).exit_code == 3


def test_evaluation_error_exits_1(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise EvaluationError("drive failed verification")

    monkeypatch.setattr(usecase_cli, "evaluate_drive", refuse)
    result = _evaluate(drive)
    assert result.exit_code == 1
    assert "drive failed verification" in result.output


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


def test_evaluate_other_failure_exits_1_with_type_name(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(usecase_cli, "evaluate_drive", boom)
    result = _evaluate(drive)
    assert result.exit_code == 1
    assert "RuntimeError" in result.output and "secret detail" not in result.output


def test_evaluate_refuses_a_linked_drive_folder(
    make_drive: Callable[[], MadeDrive], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()
    link = tmp_path / "link"
    try:
        link.symlink_to(drive.drive_dir, target_is_directory=True)
    except OSError:
        link = drive.drive_dir  # no symlink right: stand in for the link check below
        real = usecase_cli._is_link
        monkeypatch.setattr(usecase_cli, "_is_link", lambda p: p == drive.drive_dir or real(p))
    result = _run(["evaluate", str(link), "--lake-dir", str(drive.lake.root)])
    assert result.exit_code == 2
    assert "link" in result.output


def test_evaluate_refuses_a_link_inside_the_drive_folder(
    make_drive: Callable[[], MadeDrive], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = make_drive()
    inner = sorted(drive.drive_dir.iterdir())[0]
    real = usecase_cli._is_link
    monkeypatch.setattr(usecase_cli, "_is_link", lambda p: p == inner or real(p))
    result = _evaluate(drive)
    assert result.exit_code == 2
    assert "link" in result.output


def test_evaluate_output_matches_the_report(make_drive: Callable[[], MadeDrive]) -> None:
    drive = make_drive()
    report = evaluate_drive(drive.drive_dir, drive.client, drive.config, drive.motion_cfg)
    out = _evaluate(drive).output
    assert _value(out, "scenario_hash") == report.scenario_hash
    assert _value(out, "frames_interior") == f"{report.frames_interior} frames"
    assert _value(out, "frames_null") == f"{report.frames_null} frames"
    cfg_tol, tol = drive.motion_cfg.tolerances, drive.config.tolerances
    expected = {
        "position (cm)": (report.position_cm, tol.position_cm),
        "velocity (cm/s)": (report.velocity_cm_s, cfg_tol.velocity_cm_s),
        "acceleration (cm/s^2)": (report.acceleration_cm_s2, cfg_tol.acceleration_cm_s2),
    }
    for name, (errors, tolerance) in expected.items():
        line = _value(out, re.escape(name))
        assert f"interior max {errors.max_abs:.3f} rms {errors.rms:.3f}" in line
        assert f"tolerance {tolerance:g}" in line
    assert f"all frames max {report.velocity_all_cm_s.max_abs:.3f}" in _value(
        out, re.escape("velocity (cm/s)")
    )
