"""Own-account attendance events; source records do not identify punch direction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from ..core.errors import ConfigurationError

AttendanceMode = Literal["processed", "raw"]
ATTENDANCE_MODE_VALUES = {"processed": "qryProcess", "raw": "qryFinMachine"}


@dataclass(frozen=True, slots=True)
class AttendanceQuery:
    start: date
    end: date
    mode: AttendanceMode = "processed"

    def __post_init__(self) -> None:
        if type(self.start) is not date or type(self.end) is not date:
            raise ConfigurationError(
                "attendance dates require date values", code="ATTENDANCE_DATE_INVALID"
            )
        if not 0 <= (self.end - self.start).days <= 180:
            raise ConfigurationError(
                "attendance date difference must be 0 to 180 days",
                code="ATTENDANCE_DATE_RANGE_INVALID",
            )
        if not isinstance(self.mode, str) or self.mode not in ATTENDANCE_MODE_VALUES:
            raise ConfigurationError(
                "unknown attendance query mode", code="ATTENDANCE_MODE_INVALID"
            )

    def to_form(self) -> dict[str, str]:
        return {
            "reqCode": "getProcessedFingerLog",
            "value(begDate)": self.start.isoformat(),
            "value(endDate)": self.end.isoformat(),
            "b1": "查詢",
            "value(qryType)": ATTENDANCE_MODE_VALUES[self.mode],
        }


@dataclass(frozen=True, slots=True)
class AttendanceRecord:
    occurred_at: datetime
    description: str
    location_code: str | None = None


@dataclass(frozen=True, slots=True)
class AttendanceState:
    employee_id: str = field(repr=False)
    employee_name: str = field(repr=False)
    unit_label: str = field(repr=False)
    computer_serial: str = field(repr=False)
    last_punch_at: datetime | None
    last_location_code: str | None


@dataclass(frozen=True, slots=True)
class AttendanceHistory:
    employee_name: str = field(repr=False)
    records: tuple[AttendanceRecord, ...]
    total_count: int
    query: AttendanceQuery | None = None
    start: date | None = None
    end: date | None = None
    reported_mode: AttendanceMode | None = None
    # The history page only returns a name. An adapter can bind the immediately
    # verified status and shared Session, without claiming the page returns an ID.
    account_context: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AttendancePunchReceipt:
    record: AttendanceRecord
    history: AttendanceHistory
    status: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"
