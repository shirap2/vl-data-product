"""Canonical JSON and SHA-256 helpers. No imports from the generator."""

import hashlib
import json
import math
from collections.abc import Mapping
from numbers import Integral
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel


def _normalise(obj: Any) -> Any:
    """Walk dicts, lists and tuples; make integral floats ints; reject nan/inf."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be str, got {type(key).__name__}")
            out[key] = _normalise(value)
        return out
    if isinstance(obj, list | tuple):
        return [_normalise(v) for v in obj]
    if isinstance(obj, bool):  # bool is an int subclass: leave it alone
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"cannot hash non-finite float {obj!r}")
        return int(obj) if obj.is_integer() else obj
    return obj


def canonical_json(obj: Any) -> str:
    """Sorted-key, no-space, ASCII JSON with shortest round-trip floats.

    A float with an integral value is written as an int (600.0 -> 600, -0.0 -> 0),
    so 600 and 600.0 agree. nan and inf raise ValueError; non-str keys raise TypeError.
    Units are whatever the caller's values carry.
    """
    return json.dumps(
        _normalise(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_hex(data: bytes) -> str:
    """SHA-256 of bytes, 64 lowercase hex characters."""
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes on disk, 64 lowercase hex characters."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _whole_number(name: str, value: Any) -> int:
    """Return value as an int; it must have an integral value (100, 100.0, np.int64(100) are fine).

    Raises ValueError for a bool, a str, a non-numeric value, or a non-integral or non-finite
    number (100.5, nan, inf, np.float32(100.5), Decimal("100.5")). int() alone would truncate
    100.5 to 100 and so give it the hash (and folder) of 100.
    """
    message = f"body {name} must be a whole number, got {value!r}"
    if isinstance(value, bool | str | np.bool_):
        raise ValueError(message)
    if isinstance(value, Integral):
        return int(value)
    try:
        as_float = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not as_float.is_integer():  # also false for nan and inf
        raise ValueError(message)
    return int(as_float)


def drive_hash(scenario: dict[str, Any], body: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of {"body": {size_cm, points_per_axis}, "scenario": ...}.

    scenario is the validated scenario as a JSON-ready dict. body needs size_cm (cm) and
    points_per_axis (count); each must be a whole number, so 100.0 and 100 agree and give the
    same hash. A non-integral value such as 100.5 raises ValueError. Extra body keys are ignored.
    The same scenario at another density gets another hash, so each density has its own drive
    folder.
    """
    payload = {
        "body": {
            "size_cm": _whole_number("size_cm", body["size_cm"]),
            "points_per_axis": _whole_number("points_per_axis", body["points_per_axis"]),
        },
        "scenario": scenario,
    }
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def scenario_hash(scenario: BaseModel, body: Any = None) -> str:
    """SHA-256 of a validated scenario model; with a body, the drive hash (see drive_hash).

    Without body this is the SHA-256 of the canonical JSON (UTF-8) of the scenario alone.
    body is a dict or an object with size_cm and points_per_axis (BodyConfig, BodyInfo).
    A no-body hash is NOT a drive hash: a drive's hash always includes its body, so never use
    scenario_hash(scenario) to name or look up a drive folder. Raises ValueError for a body
    value that is not a whole number (see drive_hash).
    """
    dumped = scenario.model_dump(mode="json")
    if body is None:
        return sha256_hex(canonical_json(dumped).encode("utf-8"))
    if isinstance(body, Mapping):
        return drive_hash(dumped, body)
    return drive_hash(dumped, {"size_cm": body.size_cm, "points_per_axis": body.points_per_axis})
