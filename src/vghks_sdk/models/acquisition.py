"""Optional acquisition contract, independent of individual Service return types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from ..core.errors import ErrorInfo

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DataAssessment:
    """Availability within this query; UNKNOWN does not assert data absence.

    complete describes supported parsing, not clinical or historical coverage.
    Attachment references and binary bytes do not imply extracted clinical data.
    issues affect completeness; recoverable warnings retain source evidence.
    """

    availability: str
    item_count: int | None = None
    complete: bool | None = None
    issues: tuple[ErrorInfo, ...] = ()
    warnings: tuple[ErrorInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class AcquisitionResult(Generic[T]):
    """One attempted read: OK, EMPTY, PARTIAL, or ERROR; never an automatic retry."""

    status: str
    value: T | None = None
    data: DataAssessment | None = None
    error: ErrorInfo | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "EMPTY", "PARTIAL"}
