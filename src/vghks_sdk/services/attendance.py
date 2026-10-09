"""Attendance APIs for the current Portal account."""

from __future__ import annotations

from datetime import date

from ..extension_protocols import AttendanceProtocol
from ..models.attendance import (
    AttendanceHistory,
    AttendanceMode,
    AttendancePunchReceipt,
    AttendanceQuery,
    AttendanceState,
)


class AttendanceService:
    def __init__(self, adapter: AttendanceProtocol) -> None:
        self._adapter = adapter

    def get_status(self) -> AttendanceState:
        """Read the current account, last punch and server-returned workstation."""
        return self._adapter.get_status()

    def get_records(
        self, start: date, end: date, *, mode: AttendanceMode = "processed"
    ) -> AttendanceHistory:
        """Read the recorded event table; requested mode is separate from reported mode."""
        return self._adapter.get_records(AttendanceQuery(start, end, mode))

    def punch(self) -> AttendancePunchReceipt:
        """Submit one punch explicitly; direction and location are not client inputs."""
        return self._adapter.punch()
