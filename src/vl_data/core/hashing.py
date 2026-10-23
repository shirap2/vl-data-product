"""Canonical JSON and SHA-256 helpers. No imports from the generator."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

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


def scenario_hash(scenario: BaseModel) -> str:
    """SHA-256 of the canonical JSON (UTF-8) of a validated scenario model."""
    return sha256_hex(canonical_json(scenario.model_dump(mode="json")).encode("utf-8"))
