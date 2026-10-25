"""Tests for the shared body limits (core.schemas) and the BodyConfig points-vs-size check."""

import ast
from pathlib import Path

import annotated_types
import pytest
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from vl_data.core import schemas
from vl_data.core.config import BodyConfig, Config, load_config
from vl_data.core.manifest import BodyInfo
from vl_data.generator import body

SRC = Path(__file__).resolve().parents[2] / "src" / "vl_data"


def _le(field: FieldInfo) -> object:
    """The Le bound (value) in a field's metadata."""
    found = [m.le for m in field.metadata if isinstance(m, annotated_types.Le)]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("size_cm", [100, 2])
def test_points_equal_to_size_is_accepted(size_cm: int) -> None:
    assert BodyConfig(size_cm=size_cm, points_per_axis=size_cm).points_per_axis == size_cm


def test_points_above_size_is_rejected_everywhere(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="repeated"):
        BodyConfig(size_cm=100, points_per_axis=101)
    with pytest.raises(ValidationError, match="repeated"):
        Config.model_validate({"body": {"size_cm": 100, "points_per_axis": 101}})
    path = tmp_path / "cfg.yaml"
    path.write_text("body:\n  size_cm: 100\n  points_per_axis: 101\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="points_per_axis 101 > size_cm 100"):
        load_config(path)


def test_small_size_with_default_points_fails_at_load(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("body:\n  size_cm: 50\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="repeated"):
        load_config(path)


def test_body_info_does_not_get_the_points_vs_size_check() -> None:
    """BodyInfo is a reader contract (Modules 2 and 3): it stays as it was."""
    assert BodyInfo(size_cm=50, points_per_axis=100).points_per_axis == 100


def test_bounds_come_from_the_schemas_constants() -> None:
    assert schemas.MAX_BODY_SIZE_CM == 2_147_483_647  # cm
    assert schemas.MAX_POINTS_PER_AXIS == 1_000_000  # count
    for model in (BodyConfig, BodyInfo):
        assert _le(model.model_fields["size_cm"]) == schemas.MAX_BODY_SIZE_CM
        assert _le(model.model_fields["points_per_axis"]) == schemas.MAX_POINTS_PER_AXIS


@pytest.mark.parametrize("name", ["core/config.py", "core/manifest.py", "generator/body.py"])
def test_no_retyped_limit_literals(name: str) -> None:
    tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
    ints = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, int)
        and not isinstance(n.value, bool)
    ]
    assert 2_147_483_647 not in ints
    assert 1_000_000 not in ints


def test_body_alias_is_the_schemas_constant() -> None:
    assert body._MAX_POINTS_PER_AXIS == schemas.MAX_POINTS_PER_AXIS
    # smoke check only: small ints may be shared by value, not a contract
    assert body._MAX_POINTS_PER_AXIS is schemas.MAX_POINTS_PER_AXIS
