from __future__ import annotations

from datetime import date

from ..adapters.protocols import PrqAdapterProtocol
from ..models import OutpatientPatient


class OpdService:
    def __init__(self, adapter: PrqAdapterProtocol) -> None:
        self._adapter = adapter

    def get_doctor_patients(self, card_no: str, visit_date: date) -> list[OutpatientPatient]:
        return self._adapter.get_doctor_patients(card_no, visit_date)
