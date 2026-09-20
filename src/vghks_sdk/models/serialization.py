"""Typed serialization data for the public SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .assets import BinaryAsset


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses and dates to deterministic JSON-compatible data."""

    if isinstance(value, BinaryAsset):
        return {
            "media_type": value.media_type,
            "size": value.size,
            "sha256": value.sha256,
        }
    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (PurePosixPath, PureWindowsPath)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value
