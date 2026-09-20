"""Typed validation data for the public SDK."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote

from ..core.errors import ConfigurationError


def _normalize_code(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).upper()


def _unique_normalized(values: Iterable[Any], *, mode: str) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if mode == "code":
            normalized = _normalize_code(value)
            identity = normalized
        else:
            normalized = unicodedata.normalize("NFKC", str(value)).strip()
            identity = normalized.casefold()
        if not normalized:
            raise ConfigurationError("visit filter values must not be blank")
        if identity not in seen:
            seen.add(identity)
            output.append(normalized)
    return tuple(output)


def _history_lookback(value: int | str, maximum: int) -> int:
    if isinstance(value, str) and unicodedata.normalize("NFKC", value).strip().casefold() == "all":
        return maximum
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("history lookback must be a positive day count or all") from exc
    if days < 1 or days > maximum:
        raise ConfigurationError(
            f"history lookback must be between 1 and {maximum} days",
            code="HISTORY_LOOKBACK_INVALID",
        )
    return days


def _normalize_filter_code(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    candidate = _normalize_code(value)
    return "*" if candidate in {"", "ALL"} else candidate


def _safe_filter_value(value: Any) -> str:
    candidate = unicodedata.normalize("NFKC", str(value)).strip()
    if not candidate or candidate.casefold() == "all":
        return "*"
    if len(candidate) > 40 or any(char in candidate for char in "&=?#\x00\r\n"):
        raise ConfigurationError("history filter value is unsafe", code="HISTORY_FILTER_INVALID")
    return candidate


def _validate_navigation_ref(mrn: str, *values: str) -> None:
    if not isinstance(mrn, str) or not re_fullmatch_mrn(mrn):
        raise ConfigurationError(
            "navigation reference has an invalid MRN", code="NAV_REF_MRN_INVALID"
        )
    for value in values:
        candidate = str(value)
        if (
            len(candidate) > 256
            or ".." in candidate
            or any(char in candidate for char in "/\\:?&#\x00\r\n")
        ):
            raise ConfigurationError(
                "navigation reference contains unsafe data", code="NAV_REF_INVALID"
            )


def _require_navigation_values(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ConfigurationError(
            "navigation reference is missing required data",
            code="NAV_REF_INCOMPLETE",
        )


def re_fullmatch_mrn(value: str) -> bool:
    return bool(value) and len(value) <= 32 and all(char.isalnum() or char == "-" for char in value)


def re_split_path(value: str) -> tuple[str, ...]:
    windows = PureWindowsPath(value).parts
    posix = PurePosixPath(value.replace("\\", "/")).parts
    return tuple(
        str(part).rstrip(":") for part in (windows if len(windows) >= len(posix) else posix)
    )


def _fully_unquote(value: str) -> str:
    current = str(value).strip()
    for _ in range(3):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current
