"""Tests for the generate and verify commands."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vl_data.cli import app

DEMO = Path(__file__).resolve().parents[2] / "src/vl_data/generator/scenarios/demo.yaml"


def test_generate_prints_summary(runner: CliRunner, tmp_path: Path) -> None:
    """The summary lines are printed and the manifest is verified."""
    result = runner.invoke(app, ["generate", "--scenario", str(DEMO), "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "frames: 1000" in lines
    assert "timestamp_spacing_ns: 62500000" in lines
    assert "points_per_frame: 1000" in lines
    assert "manifest: verified" in lines
    hash_line = next(x for x in lines if x.startswith("scenario_hash: "))
    digest = hash_line.split(": ", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    drive_line = next(x for x in lines if x.startswith("drive_dir: "))
    assert Path(drive_line.split(": ", 1)[1]) == tmp_path / digest[:12]


def test_generate_default_out_uses_vl_raw_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --out the drive goes under VL_RAW_DIR."""
    monkeypatch.setenv("VL_RAW_DIR", str(tmp_path))
    result = runner.invoke(app, ["generate", "--scenario", str(DEMO)])
    assert result.exit_code == 0, result.output
    digest = next(x for x in result.output.splitlines() if x.startswith("scenario_hash: "))
    assert [p.name for p in tmp_path.iterdir()] == [digest.split(": ", 1)[1][:12]]


def test_generate_missing_scenario(runner: CliRunner, tmp_path: Path) -> None:
    """A missing scenario file gives a one-line error and exit 1."""
    result = runner.invoke(
        app, ["generate", "--scenario", str(tmp_path / "nope.yaml"), "--out", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "Traceback" not in result.output


def test_generate_invalid_scenario(runner: CliRunner, tmp_path: Path) -> None:
    """An invalid scenario gives a clean error and exit 1."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("segments: []\n", encoding="utf-8")
    result = runner.invoke(app, ["generate", "--scenario", str(bad), "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert result.stderr.startswith("error:")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.output


def test_verify_succeeds(runner: CliRunner, tmp_path: Path) -> None:
    """verify prints manifest: verified for an intact drive."""
    made = runner.invoke(app, ["generate", "--scenario", str(DEMO), "--out", str(tmp_path)])
    assert made.exit_code == 0, made.output
    folder = next(tmp_path.iterdir())
    result = runner.invoke(app, ["verify", str(folder)])
    assert result.exit_code == 0
    assert "manifest: verified" in result.output.splitlines()


def test_verify_fails_on_tamper(runner: CliRunner, tmp_path: Path) -> None:
    """verify exits non-zero after a byte is changed."""
    made = runner.invoke(app, ["generate", "--scenario", str(DEMO), "--out", str(tmp_path)])
    assert made.exit_code == 0, made.output
    folder = next(tmp_path.iterdir())
    target = folder / "frames.parquet"
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    result = runner.invoke(app, ["verify", str(folder)])
    assert result.exit_code == 1
    assert "frames.parquet" in result.output
    assert "manifest: verified" not in result.output


def test_generate_out_of_range_is_one_line(runner: CliRunner, tmp_path: Path) -> None:
    """A position outside int32 cm fails in generate_to_disk: one error line, nothing written."""
    big = tmp_path / "big.yaml"
    lines = [
        "seed: 0",
        "start: {x_cm: 1.0e+12, y_cm: 0, z_cm: 0, speed_cm_s: 0}",
        "segments:",
        "  - {type: stop, duration_s: 1.0}",
    ]
    big.write_text(chr(10).join(lines), encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, ["generate", "--scenario", str(big), "--out", str(out)])
    assert result.exit_code == 1
    assert "cannot write drive" in result.stderr
    assert result.stderr.startswith("error:")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.output
    assert not out.exists() or list(out.iterdir()) == []


def test_generate_prints_restore_note(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notes attached to a write error (where the old drive is kept) reach the error line."""
    err = OSError("rename failed")
    err.add_note("could not restore the old drive; it is kept at X")

    def boom(*args: object, **kwargs: object) -> Path:
        raise err

    monkeypatch.setattr("vl_data.generator.cli.generate_to_disk", boom)
    result = runner.invoke(app, ["generate", "--scenario", str(DEMO), "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "kept at X" in result.stderr
    assert result.stderr.startswith("error:")
    assert len(result.stderr.strip().splitlines()) == 1


def test_generate_out_is_a_file(runner: CliRunner, tmp_path: Path) -> None:
    """--out pointing at an existing file gives an error line and exit 1."""
    afile = tmp_path / "afile"
    afile.write_text("x")
    result = runner.invoke(app, ["generate", "--scenario", str(DEMO), "--out", str(afile)])
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "Traceback" not in result.output


def test_roundtrip_command(runner: CliRunner) -> None:
    """The command prints "round trip OK" and exits 0."""
    result = runner.invoke(app, ["roundtrip", "--scenario", str(DEMO)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "segments: expected 8, found 8" in lines
    assert "round trip OK" in lines
    values = {x.split(": ")[0]: float(x.split(": ")[1]) for x in lines if "max_" in x}
    assert values["max_boundary_error_frames"] <= 6
    assert values["max_duration_error_frames"] <= 8
    assert values["max_end_speed_error_cm_s"] <= 40


def test_roundtrip_missing_scenario_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["roundtrip", "--scenario", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1
    assert "error: cannot load scenario" in result.output


def test_roundtrip_failed_comparison_exits_1(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed comparison prints the segment line and the problems, and exits 1."""
    from vl_data.generator import cli as gen_cli
    from vl_data.generator.inverse import RoundTripReport

    bad = RoundTripReport(False, ["segment 0: expected stop, found move"], 0, 0, 0.0)
    monkeypatch.setattr(gen_cli, "compare_scenarios", lambda *a, **k: bad)
    result = runner.invoke(app, ["roundtrip", "--scenario", str(DEMO)])
    assert result.exit_code == 1
    assert "segments: expected 8, found 8" in result.output
    assert "segment 0: expected stop, found move" in result.output
    assert "round trip OK" not in result.output


def test_roundtrip_unbuildable_scenario_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    """A scenario whose body leaves the int32 cm range gives one error line, exit 1."""
    path = tmp_path / "far.yaml"
    path.write_text(
        "start: {x_cm: 1000000000000000.0}\nsegments:\n  - {type: stop, duration_s: 1.0}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["roundtrip", "--scenario", str(path)])
    assert result.exit_code == 1
    assert "error: cannot round-trip scenario" in result.output


def test_roundtrip_warns_about_short_segments(runner: CliRunner, tmp_path: Path) -> None:
    """Segments under 32 frames get a warning before the result, whatever the outcome."""
    path = tmp_path / "short.yaml"
    path.write_text(
        "start: {x_cm: 0.0}\nsegments:\n"
        "  - {type: stop, duration_s: 2.0}\n"
        "  - {type: move, duration_s: 1.0, end_speed_cm_s: 100.0}\n"
        "  - {type: move, duration_s: 1.0, end_speed_cm_s: 0.0}\n"
        "  - {type: stop, duration_s: 2.0}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["roundtrip", "--scenario", str(path)])
    warnings = result.stderr.splitlines()
    assert len(warnings) == 2
    assert warnings[0].startswith("warning: segment 1 is 16 frames;")
    assert warnings[1].startswith("warning: segment 2 is 16 frames;")
    assert "32 frames or more" in warnings[0]
    # the warnings go to stderr only; stdout keeps the result lines
    assert "warning" not in result.stdout
    assert result.stdout.splitlines()[0] == "segments: expected 4, found 4"
    # the warnings come before the result in the combined stream
    combined = result.output.splitlines()
    assert combined.index(warnings[1]) < combined.index("segments: expected 4, found 4")
    # the exit code and the result line are the command's own; the warning does not change them
    assert result.exit_code in (0, 1)
    assert ("round trip OK" in result.stdout) == (result.exit_code == 0)


def test_roundtrip_warns_when_comparison_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning is printed even when the comparison fails and the command exits 1."""
    from vl_data.generator import cli as gen_cli
    from vl_data.generator.inverse import RoundTripReport

    path = tmp_path / "short.yaml"
    path.write_text(
        "start: {x_cm: 0.0}\nsegments:\n"
        "  - {type: move, duration_s: 1.0, end_speed_cm_s: 100.0}\n"
        "  - {type: move, duration_s: 1.0, end_speed_cm_s: 0.0}\n",
        encoding="utf-8",
    )
    bad = RoundTripReport(False, ["segment 0: expected move, found stop"], 0, 0, 0.0)
    monkeypatch.setattr(gen_cli, "compare_scenarios", lambda *a, **k: bad)
    result = runner.invoke(app, ["roundtrip", "--scenario", str(path)])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0].startswith("warning: segment 0 is 16 frames;")
    assert "round trip OK" not in result.stdout


def test_roundtrip_demo_has_no_warning(runner: CliRunner) -> None:
    """The demo (shortest segment 32 frames) prints no warning."""
    result = runner.invoke(app, ["roundtrip", "--scenario", str(DEMO)])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
