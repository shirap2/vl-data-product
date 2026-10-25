"""Tests for the ingest and error-codes commands, through the root app. Small body only."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from vl_data.cli import app
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.etl import cli as etl_cli
from vl_data.etl.config import EtlConfig, KeypointSpec
from vl_data.etl.errors_doc import render_error_codes
from vl_data.etl.ingest import IngestResult
from vl_data.generator.drive import generate
from vl_data.generator.faults import BreakSchema, DropFrame
from vl_data.generator.models import Scenario
from vl_data.generator.writer import write_drive

from .helpers import Written

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_env_lake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)


def parse(text: str) -> dict[str, str]:
    """key: value lines of one summary block."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            out[key] = value
    return out


def blocks(text: str) -> list[dict[str, str]]:
    """Split the output on blank lines and parse the blocks that hold a drive line."""
    parsed = [parse(part) for part in text.split("\n\n")]
    return [b for b in parsed if "drive" in b]


def raw_root(tmp_path: Path, *drives: Path) -> Path:
    """A raw folder holding copies of the given drive folders."""
    root = tmp_path / "raw"
    root.mkdir()
    for drive in drives:
        shutil.copytree(drive, root / drive.name)
    return root


def test_ingest_one_drive_summary(tmp_path: Path, write_faulty: Callable[..., Written]) -> None:
    written = write_faulty([DropFrame(5)])
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 0, result.output
    assert result.output.isascii()
    keys = [line.split(": ", 1)[0] for line in result.stdout.splitlines()]
    assert keys == [
        "drive",
        "status",
        "frames_expected",
        "frames_in",
        "frames_kept",
        "frames_quarantined",
        "frames_missing",
        "keypoint_rows",
        "points_per_frame_in",
        "keypoints_per_frame_out",
        "quarantine_VL-001",
        "elapsed_s",
    ]
    summary = parse(result.stdout)
    assert summary["drive"] == written.drive_dir.name
    assert summary["status"] == "ok"
    assert summary["frames_expected"] == "1000"
    assert summary["frames_in"] == "999"
    assert summary["frames_kept"] == "999"
    assert summary["frames_quarantined"] == "1"
    assert summary["frames_missing"] == "1"
    assert summary["keypoint_rows"] == str(999 * 9)
    assert summary["points_per_frame_in"] == "125"
    assert summary["keypoints_per_frame_out"] == "9"
    assert summary["quarantine_VL-001"] == "1"
    float(summary["elapsed_s"])  # parses as a number
    assert Lake(lake_dir).list_drives("lineage") == [written.drive_dir.name]


def test_ingest_all(
    tmp_path: Path,
    write_faulty: Callable[..., Written],
    demo_scenario: Scenario,
    small_config: Config,
) -> None:
    first = write_faulty([])
    other = write_drive(
        generate(demo_scenario.model_copy(update={"seed": 7}), small_config),
        tmp_path / "other",
        small_config,
    )
    root = raw_root(tmp_path, first.drive_dir, other)
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 0, result.output
    found = blocks(result.stdout)
    assert [b["drive"] for b in found] == sorted([first.drive_dir.name, other.name])
    assert all(b["status"] == "ok" for b in found)
    assert sorted(Lake(lake_dir).list_drives("lineage")) == sorted(
        [first.drive_dir.name, other.name]
    )


def test_all_skips_other_entries_and_ingests_hex_folder_without_manifest(
    tmp_path: Path, write_faulty: Callable[..., Written]
) -> None:
    good = write_faulty([])
    root = raw_root(tmp_path, good.drive_dir)
    (root / ".tmp-x").mkdir()
    (root / "notes").mkdir()
    (root / "ABCDEF012345").mkdir()  # upper case is not a 12-hex drive folder
    (root / "0123456789ab.txt").write_text("not a folder", encoding="utf-8")
    (root / "0123456789ab").mkdir()  # a hex folder with no manifest: ingested and rejected
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 1, result.output
    assert "skipped: .tmp-x" in result.stdout
    assert "skipped: notes" in result.stdout
    assert "skipped: ABCDEF012345" in result.stdout
    assert "skipped: 0123456789ab.txt" in result.stdout
    found = blocks(result.stdout)
    status = {b["drive"]: b["status"] for b in found}
    assert status == {"0123456789ab": "rejected", good.drive_dir.name: "ok"}
    assert [b["drive"] for b in found] == sorted(status)  # sorted order
    rejected = next(b for b in found if b["drive"] == "0123456789ab")
    assert rejected["quarantine_INTEGRITY"] == "1"


def test_all_runs_every_drive_and_exits_one_if_any_rejected(
    tmp_path: Path, write_faulty: Callable[..., Written]
) -> None:
    good = write_faulty([])
    bad = write_faulty([BreakSchema()])
    # same scenario, so the same folder name: put them in separate roots, rename the bad one
    root = tmp_path / "raw"
    root.mkdir()
    shutil.copytree(good.drive_dir, root / good.drive_dir.name)
    shutil.copytree(bad.drive_dir, root / "0000000000aa")
    result = runner.invoke(
        app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "lake")]
    )
    assert result.exit_code == 1
    status = {b["drive"]: b["status"] for b in blocks(result.stdout)}
    assert status == {"0000000000aa": "rejected", good.drive_dir.name: "ok"}


def test_rejected_drive_exits_one(tmp_path: Path, write_faulty: Callable[..., Written]) -> None:
    written = write_faulty([])
    frames = written.drive_dir / "frames.parquet"
    frames.write_bytes(frames.read_bytes() + b"x")  # the manifest hash no longer matches
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 1
    summary = parse(result.stdout)
    assert summary["status"] == "rejected"
    assert summary["keypoint_rows"] == "0"
    assert summary["quarantine_INTEGRITY"] == "2"  # a wrong size and a wrong hash
    assert "frames.parquet" not in result.output  # codes and counts only, no message text


def test_ingest_needs_one_target(tmp_path: Path, write_faulty: Callable[..., Written]) -> None:
    written = write_faulty([])
    lake_dir = tmp_path / "lake"
    neither = runner.invoke(app, ["ingest", "--lake-dir", str(lake_dir)])
    both = runner.invoke(
        app,
        [
            "ingest",
            str(written.drive_dir),
            "--all",
            str(written.drive_dir.parent),
            "--lake-dir",
            str(lake_dir),
        ],
    )
    assert neither.exit_code == 2
    assert both.exit_code == 2
    assert not lake_dir.exists()


def test_missing_or_non_directory_path_exits_two_and_writes_nothing(
    tmp_path: Path,
) -> None:
    lake_dir = tmp_path / "lake"
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    for args in (
        ["ingest", str(tmp_path / "typo" / "0123456789ab")],
        ["ingest", str(a_file)],
        ["ingest", "--all", str(tmp_path / "missing")],
        ["ingest", "--all", str(a_file)],
    ):
        result = runner.invoke(app, [*args, "--lake-dir", str(lake_dir)])
        assert result.exit_code == 2, args
        assert result.stderr.startswith("error: "), args
        assert result.stderr.isascii()
    assert not lake_dir.exists()


def test_all_without_drive_folders_exits_two(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    (root / "notes").mkdir()
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 2
    assert "no drive folders" in result.stderr


def test_bad_drive_id_exits_two_and_writes_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "bad name"  # a space is not a valid lake drive id
    folder.mkdir()
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", str(folder), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
    assert Lake(lake_dir).list_drives("lineage") == []


def test_bad_keypoint_spec_exits_two(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])
    bad = EtlConfig(keypoints=[KeypointSpec(name="far", kind="index", index=10**6)])
    monkeypatch.setattr(etl_cli, "load_etl_config", lambda: bad)
    root = raw_root(tmp_path, written.drive_dir)
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
    assert Lake(tmp_path / "l").list_drives("lineage") == []


def _two_drive_root(tmp_path: Path, write_faulty: Callable[..., Written]) -> tuple[Path, str, str]:
    """A raw folder with two drives; returns it and the two folder names in sorted order."""
    root = tmp_path / "raw"
    root.mkdir()
    good = write_faulty([])
    shutil.copytree(good.drive_dir, root / "0000000000aa")
    shutil.copytree(good.drive_dir, root / "0000000000bb")
    return root, "0000000000aa", "0000000000bb"


def test_all_continues_after_an_io_failure_and_exits_three(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, first, second = _two_drive_root(tmp_path, write_faulty)
    real = etl_cli.ingest_drive

    def flaky(folder: Path, lake: Lake, etl: EtlConfig) -> IngestResult:
        if folder.name == first:
            raise PermissionError(13, "denied", str(folder))
        return real(folder, lake, etl)

    monkeypatch.setattr(etl_cli, "ingest_drive", flaky)
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 3, result.output
    assert f"cannot ingest {first}: PermissionError" in result.stderr
    assert str(root) not in result.output  # no path in the message
    assert [b["drive"] for b in blocks(result.stdout)] == [second]
    assert Lake(lake_dir).list_drives("lineage") == [second]


def test_io_failure_beats_rejection_in_the_exit_code(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, first, second = _two_drive_root(tmp_path, write_faulty)
    (root / second / "frames.parquet").write_bytes(b"x")  # rejected
    real = etl_cli.ingest_drive

    def flaky(folder: Path, lake: Lake, etl: EtlConfig) -> IngestResult:
        if folder.name == first:
            raise OSError("disk")
        return real(folder, lake, etl)

    monkeypatch.setattr(etl_cli, "ingest_drive", flaky)
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 3


def test_single_folder_io_failure_exits_three(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])

    def broken(folder: Path, lake: Lake, etl: EtlConfig) -> IngestResult:
        raise PermissionError("denied")

    monkeypatch.setattr(etl_cli, "ingest_drive", broken)
    result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(tmp_path)])
    assert result.exit_code == 3
    assert result.stderr.strip() == f"cannot ingest {written.drive_dir.name}: PermissionError"


def test_unreadable_raw_folder_exits_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "raw"
    root.mkdir()

    def denied(self: Path) -> list[Path]:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 3
    assert "cannot ingest raw: PermissionError" in result.stderr


def test_unreadable_entry_counts_as_skipped(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, first, second = _two_drive_root(tmp_path, write_faulty)
    real_is_dir = Path.is_dir

    def is_dir(self: Path) -> bool:
        if self.name == first:
            raise PermissionError("denied")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 1, result.output
    assert f"skipped: {first}" in result.stdout
    assert [b["drive"] for b in blocks(result.stdout)] == [second]


def test_all_aborts_on_a_config_error_and_earlier_drives_stay_written(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, first, second = _two_drive_root(tmp_path, write_faulty)
    real = etl_cli.ingest_drive

    def picky(folder: Path, lake: Lake, etl: EtlConfig) -> IngestResult:
        if folder.name == second:
            raise ValueError("keypoint spec does not fit")
        return real(folder, lake, etl)

    monkeypatch.setattr(etl_cli, "ingest_drive", picky)
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 2
    assert "keypoint spec does not fit" in result.stderr
    assert Lake(lake_dir).list_drives("lineage") == [first]


def test_safe_filters_and_caps() -> None:
    assert etl_cli._safe("a\x1b[2J\nb\r\x7fé") == "a?[2J?b???"
    assert etl_cli._safe("x" * 100) == "x" * 64
    assert etl_cli._safe("x" * 100, limit=5) == "xxxxx"
    assert etl_cli._safe("") == ""


def _forged_name() -> str:
    return "a\x1b[2J\nstatus: ok"


def test_hostile_names_are_filtered_with_a_listing_stub(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])
    root = raw_root(tmp_path, written.drive_dir)
    real_iterdir = Path.iterdir

    def listing(self: Path) -> list[Path]:
        extra = [self / _forged_name()] if self == root else []  # only the raw folder
        return [*real_iterdir(self), *extra]

    monkeypatch.setattr(Path, "iterdir", listing)
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "\r" not in result.output.replace("\r\n", "\n")
    assert "skipped: a?[2J?status: ok" in result.stdout
    # only the real drive's summary has a line "status: ok"; the name made no line of its own
    assert result.stdout.split("\n").count("status: ok") == 1


def test_hostile_directory_name_on_disk_is_filtered(
    tmp_path: Path, write_faulty: Callable[..., Written]
) -> None:
    written = write_faulty([])
    root = raw_root(tmp_path, written.drive_dir)
    try:
        (root / _forged_name()).mkdir()
    except OSError:
        pytest.skip("this OS cannot create a folder name with ESC and newline")
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert "\x1b" not in result.output
    assert result.stdout.split("\n").count("status: ok") == 1  # the real drive only
    assert result.stdout.startswith("skipped: a?[2J?status: ok")


def test_value_error_text_is_filtered(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])

    def hostile(folder: Path, lake: Lake, etl: EtlConfig) -> IngestResult:
        raise ValueError("bad\x07\x1b[2J\nstatus: ok")

    monkeypatch.setattr(etl_cli, "ingest_drive", hostile)
    result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "\x1b" not in result.stderr
    assert "\x07" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


def test_all_skips_links(tmp_path: Path, write_faulty: Callable[..., Written]) -> None:
    written = write_faulty([])
    root = raw_root(tmp_path, written.drive_dir)
    link = root / "0000000000cc"
    try:
        link.symlink_to(written.drive_dir, target_is_directory=True)
    except OSError:
        pytest.skip("this OS or account cannot create symlinks")
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 0, result.output
    assert "skipped: 0000000000cc (link)" in result.stdout
    assert [b["drive"] for b in blocks(result.stdout)] == [written.drive_dir.name]


def test_all_skips_a_link_entry_listing_stub(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, first, second = _two_drive_root(tmp_path, write_faulty)
    real_is_symlink = Path.is_symlink

    def is_symlink(self: Path) -> bool:
        return self.name == first or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    result = runner.invoke(app, ["ingest", "--all", str(root), "--lake-dir", str(tmp_path / "l")])
    assert result.exit_code == 1, result.output
    assert f"skipped: {first} (link)" in result.stdout
    assert [b["drive"] for b in blocks(result.stdout)] == [second]


def test_single_folder_must_be_named_by_12_hex(tmp_path: Path) -> None:
    for name in ("good_name-1", "ABCDEF012345", "0123456789a"):
        folder = tmp_path / name
        folder.mkdir()
        lake_dir = tmp_path / f"lake-{name}"
        result = runner.invoke(app, ["ingest", str(folder), "--lake-dir", str(lake_dir)])
        assert result.exit_code == 2, name
        assert "12" in result.stderr
        assert not lake_dir.exists()


def test_app_hides_tracebacks_and_locals() -> None:
    assert app.pretty_exceptions_enable is False


@pytest.mark.parametrize("error", [ValueError("bad"), OSError("no file"), yaml.YAMLError("yaml")])
def test_unreadable_etl_config_exits_two(
    tmp_path: Path,
    write_faulty: Callable[..., Written],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    written = write_faulty([])

    def broken() -> EtlConfig:
        raise error

    monkeypatch.setattr(etl_cli, "load_etl_config", broken)
    result = runner.invoke(app, ["ingest", str(written.drive_dir)])
    assert result.exit_code == 2
    assert "cannot load the ETL config" in result.stderr


def test_lake_dir_option_beats_environment(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])
    monkeypatch.setenv("VL_LAKE_DIR", str(tmp_path / "from-env"))
    result = runner.invoke(
        app, ["ingest", str(written.drive_dir), "--lake-dir", str(tmp_path / "from-flag")]
    )
    assert result.exit_code == 0
    assert Lake(tmp_path / "from-flag").list_drives("lineage") == [written.drive_dir.name]
    assert not (tmp_path / "from-env").exists()


def test_environment_beats_config_default(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VL_LAKE_DIR", str(tmp_path / "from-env"))
    result = runner.invoke(app, ["ingest", str(written.drive_dir)])
    assert result.exit_code == 0
    assert Lake(tmp_path / "from-env").list_drives("lineage") == [written.drive_dir.name]
    assert not (tmp_path / "warehouse").exists()


def test_config_default_lake_dir_is_warehouse(
    tmp_path: Path, write_faulty: Callable[..., Written], monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_faulty([])
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ingest", str(written.drive_dir)])
    assert result.exit_code == 0
    assert Lake(tmp_path / "warehouse").list_drives("lineage") == [written.drive_dir.name]


def test_error_codes_prints_text_by_default() -> None:
    result = runner.invoke(app, ["error-codes"])
    assert result.exit_code == 0
    assert result.stdout.replace("\r\n", "\n").rstrip("\n") == render_error_codes().rstrip("\n")


def test_error_codes_both_flags_is_usage_error(tmp_path: Path) -> None:
    target = tmp_path / "E.md"
    result = runner.invoke(app, ["error-codes", "--write", "--check", "--path", str(target)])
    assert result.exit_code == 2
    assert not target.exists()


def test_error_codes_write_then_check(tmp_path: Path) -> None:
    target = tmp_path / "E.md"
    wrote = runner.invoke(app, ["error-codes", "--write", "--path", str(target)])
    assert wrote.exit_code == 0
    assert target.read_bytes() == render_error_codes().encode("utf-8")
    assert b"\r" not in target.read_bytes()
    assert runner.invoke(app, ["error-codes", "--check", "--path", str(target)]).exit_code == 0


def test_error_codes_check(tmp_path: Path) -> None:
    target = tmp_path / "E.md"
    missing = runner.invoke(app, ["error-codes", "--check", "--path", str(target)])
    assert missing.exit_code == 1
    target.write_text("old text\n", encoding="utf-8")
    differs = runner.invoke(app, ["error-codes", "--check", "--path", str(target)])
    assert differs.exit_code == 1
    crlf = render_error_codes().replace("\n", "\r\n")
    target.write_bytes(crlf.encode("utf-8"))  # a Windows checkout still matches
    assert runner.invoke(app, ["error-codes", "--check", "--path", str(target)]).exit_code == 0


def test_error_codes_default_path_is_the_repo_document() -> None:
    assert etl_cli.DEFAULT_ERROR_CODES_PATH == (
        Path(__file__).resolve().parents[2] / "docs" / "ERROR_CODES.md"
    )
    assert etl_cli.DEFAULT_ERROR_CODES_PATH.is_file()


def test_error_codes_default_path_needs_a_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "site" / "docs" / "ERROR_CODES.md"  # no docs folder, no pyproject
    monkeypatch.setattr(etl_cli, "DEFAULT_ERROR_CODES_PATH", fake)
    for flag in ("--write", "--check"):
        result = runner.invoke(app, ["error-codes", flag])
        assert result.exit_code == 2, flag
        assert "--path" in result.stderr
    assert not fake.parent.exists()
    fake.parent.mkdir(parents=True)  # a docs folder but still no pyproject.toml
    assert runner.invoke(app, ["error-codes", "--write"]).exit_code == 2
    assert not fake.exists()
    (tmp_path / "site" / "pyproject.toml").write_text("", encoding="utf-8")
    assert runner.invoke(app, ["error-codes", "--write"]).exit_code == 0
    assert runner.invoke(app, ["error-codes", "--check"]).exit_code == 0
    assert fake.read_bytes() == render_error_codes().encode("utf-8")


def test_error_codes_write_refuses_a_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("keep\n", encoding="utf-8")
    link = tmp_path / "E.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("this OS or account cannot create symlinks")
    result = runner.invoke(app, ["error-codes", "--write", "--path", str(link)])
    assert result.exit_code == 2
    assert real.read_text(encoding="utf-8") == "keep\n"


def test_error_codes_write_refuses_a_link_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "E.md"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "E.md")
    result = runner.invoke(app, ["error-codes", "--write", "--path", str(target)])
    assert result.exit_code == 2
    assert "link" in result.stderr
    assert not target.exists()


def test_error_codes_write_failure_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "E.md"
    target.write_text("old\n", encoding="utf-8")

    def fail(src: object, dst: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(etl_cli.os, "replace", fail)
    result = runner.invoke(app, ["error-codes", "--write", "--path", str(target)])
    assert result.exit_code == 2
    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["E.md"]


def test_error_codes_printed_path_is_filtered(tmp_path: Path) -> None:
    target = tmp_path / "no-such\x07\x1b[2J" / "E.md"
    result = runner.invoke(app, ["error-codes", "--check", "--path", str(target)])
    assert result.exit_code == 1
    assert "\x1b" not in result.output
    assert "\x07" not in result.output


def test_error_codes_write_failure_exits_two(tmp_path: Path) -> None:
    target = tmp_path / "no-such-folder" / "E.md"
    result = runner.invoke(app, ["error-codes", "--write", "--path", str(target)])
    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
