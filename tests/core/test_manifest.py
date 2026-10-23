"""Tests for the drive manifest and verifier."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vl_data.core.hashing import canonical_json, file_sha256, sha256_hex
from vl_data.core.manifest import (
    build_manifest,
    read_manifest,
    verify_drive,
    write_manifest,
)
from vl_data.core.schemas import SCHEMA_VERSION

SCENARIO: dict[str, object] = {"seed": 0, "segments": [{"type": "stop", "duration_s": 1.0}]}


def _make_drive(tmp_path: Path) -> Path:
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "frames.parquet").write_bytes(b"frames" * 100)
    (drive / "ground_truth.parquet").write_bytes(b"truth" * 50)
    manifest = build_manifest(
        drive, sha256_hex(canonical_json(SCENARIO).encode("utf-8")), SCENARIO, 16, 16, 1000
    )
    write_manifest(manifest, drive)
    return drive


def test_verify_ok(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    result = verify_drive(drive)
    assert result.ok
    assert result.problems == []


def test_verify_detects_changed_byte(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    path = drive / "frames.parquet"
    data = bytearray(path.read_bytes())
    data[10] ^= 0x01  # same size, one bit changed
    path.write_bytes(bytes(data))
    result = verify_drive(drive)
    assert not result.ok
    assert len(result.problems) == 1
    assert "frames.parquet" in result.problems[0]
    assert "sha256" in result.problems[0]


def test_verify_detects_size_change(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    with (drive / "ground_truth.parquet").open("ab") as f:
        f.write(b"x")
    result = verify_drive(drive)
    assert not result.ok
    assert any("ground_truth.parquet" in p and "size" in p for p in result.problems)


def test_verify_detects_missing_and_extra(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    (drive / "ground_truth.parquet").unlink()
    (drive / "stray.txt").write_text("hi", encoding="utf-8")
    (drive / "subdir").mkdir()
    result = verify_drive(drive)
    assert not result.ok
    assert "missing: ground_truth.parquet" in result.problems
    assert "extra: stray.txt" in result.problems
    assert "extra: subdir" in result.problems
    assert len(result.problems) == 3


def test_verify_detects_scenario_hash_mismatch(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    manifest = read_manifest(drive)
    edited = manifest.model_copy(update={"scenario": {"seed": 1, "segments": []}})
    write_manifest(edited, drive)
    result = verify_drive(drive)
    assert not result.ok
    assert result.problems == ["scenario hash does not match scenario"]


def test_verify_missing_or_invalid_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = verify_drive(empty)
    assert not result.ok and len(result.problems) == 1
    assert "manifest.json" in result.problems[0]

    (empty / "manifest.json").write_text("{not json", encoding="utf-8")
    result = verify_drive(empty)
    assert not result.ok and len(result.problems) == 1
    assert "manifest.json" in result.problems[0]

    missing = verify_drive(tmp_path / "no_such_folder")
    assert not missing.ok and len(missing.problems) == 1


def test_build_manifest_contents(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    manifest = read_manifest(drive)
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.seed == 0
    assert (manifest.fps, manifest.frame_count, manifest.points_per_frame) == (16, 16, 1000)
    assert manifest.scenario == SCENARIO
    assert list(manifest.files) == ["frames.parquet", "ground_truth.parquet"]
    entry = manifest.files["frames.parquet"]
    assert entry.bytes == 600
    assert entry.sha256 == file_sha256(drive / "frames.parquet")


def test_build_manifest_rejects_subdirectory(tmp_path: Path) -> None:
    drive = tmp_path / "d"
    (drive / "sub").mkdir(parents=True)
    with pytest.raises(ValueError, match="sub"):
        build_manifest(drive, "0" * 64, SCENARIO, 16, 0, 0)


def test_build_manifest_ignores_existing_manifest(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    again = build_manifest(drive, "0" * 64, SCENARIO, 16, 16, 1000)
    assert "manifest.json" not in again.files


def test_write_manifest_format_and_roundtrip(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    raw = (drive / "manifest.json").read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    text = raw.decode("utf-8")
    parsed = json.loads(text)
    assert text == json.dumps(parsed, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    manifest = read_manifest(drive)
    path = write_manifest(manifest, drive)
    assert path == drive / "manifest.json"
    assert path.read_bytes() == raw
    assert read_manifest(drive) == manifest


def test_read_manifest_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_manifest(tmp_path)
    (tmp_path / "manifest.json").write_text('{"schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValidationError):
        read_manifest(tmp_path)


def _rewrite_manifest(drive: Path, **changes: object) -> None:
    path = drive / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_verify_invalid_names_reported_without_raising(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    entry = {"sha256": "0" * 64, "bytes": 1}
    names = ["../evil", "sub/x", "", ".", ".."]
    _rewrite_manifest(drive, files={n: entry for n in names})
    result = verify_drive(drive)
    assert not result.ok
    for name in names:
        assert f"invalid name: {name}" in result.problems


def test_verify_unreadable_manifest(tmp_path: Path) -> None:
    as_dir = tmp_path / "a"
    (as_dir / "manifest.json").mkdir(parents=True)
    result = verify_drive(as_dir)
    assert not result.ok
    assert result.problems[0].startswith("cannot read manifest.json:")

    bad = tmp_path / "b"
    bad.mkdir()
    (bad / "manifest.json").write_bytes(b"\xff\xfe\x00bad")
    result = verify_drive(bad)
    assert not result.ok
    assert result.problems[0].startswith("cannot read manifest.json:")


def test_verify_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = _make_drive(tmp_path)

    def boom(path: Path) -> str:
        if path.name == "frames.parquet":
            raise PermissionError("denied")
        return file_sha256(path)

    monkeypatch.setattr("vl_data.core.manifest.file_sha256", boom)
    result = verify_drive(drive)
    assert not result.ok
    assert any(p.startswith("changed: frames.parquet (unreadable:") for p in result.problems)


@pytest.mark.parametrize("token", ["NaN", "1" + "0" * 5000], ids=["nan", "huge_int"])
def test_verify_unhashable_scenario(tmp_path: Path, token: str) -> None:
    drive = _make_drive(tmp_path)
    path = drive / "manifest.json"
    text = path.read_text(encoding="utf-8")
    text = text.replace('"seed": 0,\n    "segments"', f'"seed": {token},\n    "segments"')
    assert token in text
    path.write_text(text, encoding="utf-8")
    result = verify_drive(drive)
    assert not result.ok
    # a huge int is already refused by the JSON reader, which is also not a raise
    expected = "scenario hash does not match scenario" if token == "NaN" else "cannot read"
    assert any(p.startswith(expected) for p in result.problems)


def test_write_manifest_rejects_nan(tmp_path: Path) -> None:
    drive = _make_drive(tmp_path)
    bad = read_manifest(drive).model_copy(update={"scenario": {"x": float("nan")}})
    with pytest.raises(ValueError):
        write_manifest(bad, drive)


def test_build_manifest_seed_cases(tmp_path: Path) -> None:
    drive = tmp_path / "d"
    drive.mkdir()
    assert build_manifest(drive, "0" * 64, {}, 16, 0, 0).seed == 0
    assert build_manifest(drive, "0" * 64, {"seed": 7}, 16, 0, 0).seed == 7
    for bad in (True, 7.0):
        with pytest.raises(ValueError, match="seed"):
            build_manifest(drive, "0" * 64, {"seed": bad}, 16, 0, 0)


def test_hash_roundtrip_with_float(tmp_path: Path) -> None:
    drive = tmp_path / "d"
    drive.mkdir()
    (drive / "f.bin").write_bytes(b"abc")
    scenario: dict[str, object] = {"seed": 3, "duration_s": 600.0}
    manifest = build_manifest(
        drive, sha256_hex(canonical_json(scenario).encode("utf-8")), scenario, 16, 1, 1
    )
    write_manifest(manifest, drive)
    read = read_manifest(drive)
    assert verify_drive(drive).ok
    assert sha256_hex(canonical_json(read.scenario).encode("utf-8")) == read.scenario_hash
