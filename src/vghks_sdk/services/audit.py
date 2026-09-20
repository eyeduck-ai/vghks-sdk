from __future__ import annotations

from datetime import date

from ..adapters.protocols import AuditAdapterProtocol
from ..models import UnsignedRecord


class AuditService:
    def __init__(self, adapter: AuditAdapterProtocol) -> None:
        self._adapter = adapter

    def get_unsigned_records(self, doctor: str, start: date, end: date) -> list[UnsignedRecord]:
        return self._adapter.get_unsigned_records(doctor, start, end)
