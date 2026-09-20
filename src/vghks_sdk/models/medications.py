"""Typed medications data for the public SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..core.errors import ConfigurationError
from ._validation import _history_lookback, _normalize_filter_code

_MEDICATION_STATUSES = {"*", "E2", "A", "B", "C", "D", "E", "CTC", "TCM"}


@dataclass(frozen=True, slots=True)
class MedicationHistoryFilter:
    lookback_days: int | str = "all"
    status: str = "*"
    order_date: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lookback_days", _history_lookback(self.lookback_days, 2555))
        status = _normalize_filter_code(self.status)
        if status not in _MEDICATION_STATUSES:
            raise ConfigurationError(
                "unsupported medication status",
                code="MEDICATION_STATUS_INVALID",
            )
        if self.order_date is not None and not isinstance(self.order_date, date):
            raise ConfigurationError("order date must be a date", code="ORDER_DATE_INVALID")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class MedicationOrder:
    mrn: str
    case_no: str
    case_type: str
    name: str
    route: str = ""
    dose: str = ""
    unit: str = ""
    frequency: str = ""
    start_date: str = ""
    end_date: str = ""
    status: str = ""
    prescriber: str = ""
    attachment: str = ""
    extra: Mapping[str, Any] | None = None

    @property
    def identity(self) -> tuple[str, ...]:
        return (
            self.mrn,
            self.case_type,
            self.case_no,
            self.name,
            self.start_date,
            self.end_date,
            self.route,
            self.dose,
        )
