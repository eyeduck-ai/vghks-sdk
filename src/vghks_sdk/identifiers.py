"""Central normalization for medical record identifiers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .core.errors import ConfigurationError

_MRN_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")


def normalize_mrn(value: object, *, location: str = "value") -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"medical record number is invalid at {location}")
    mrn = unicodedata.normalize("NFKC", value).strip()
    if not mrn or _MRN_RE.fullmatch(mrn) is None:
        raise ConfigurationError(f"medical record number is invalid at {location}")
    return mrn


def normalize_national_id(value: object) -> str:
    """Normalize the PRQ identifier input without inventing a checksum rule."""
    if not isinstance(value, str):
        raise ConfigurationError("patient national ID is invalid", code="PATIENT_ID_INVALID")
    identifier = unicodedata.normalize("NFKC", value).strip().upper()
    if not _MRN_RE.fullmatch(identifier):
        raise ConfigurationError("patient national ID is invalid", code="PATIENT_ID_INVALID")
    return identifier


def normalize_mrns(
    values: Iterable[object],
    *,
    allow_empty: bool = False,
) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for position, value in enumerate(values, start=1):
        if isinstance(value, str) and not unicodedata.normalize("NFKC", value).strip():
            continue
        mrn = normalize_mrn(value, location=f"position {position}")
        if mrn not in seen:
            seen.add(mrn)
            output.append(mrn)
    if not output and not allow_empty:
        raise ConfigurationError("medical record number input is empty")
    return output
