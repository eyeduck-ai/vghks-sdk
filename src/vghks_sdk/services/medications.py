from __future__ import annotations

from ..adapters.protocols import PrqAdapterProtocol
from ..models import MedicationHistoryFilter, MedicationOrder, VisitCase


class MedicationsService:
    def __init__(self, adapter: PrqAdapterProtocol) -> None:
        self._adapter = adapter

    def get_case_medications(self, case: VisitCase) -> list[MedicationOrder]:
        return self._adapter.get_case_medications(case)

    def get_medication_history(
        self,
        mrn: str,
        filter: MedicationHistoryFilter,
    ) -> list[MedicationOrder]:
        return self._adapter.get_medication_history(mrn, filter)
