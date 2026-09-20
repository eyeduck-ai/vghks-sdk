from __future__ import annotations

from ..adapters.protocols import WebMaasAdapterProtocol
from ..models import PatientBasicInfo, PatientDemographics, RegistrationRecord


class PatientsService:
    def __init__(self, adapter: WebMaasAdapterProtocol) -> None:
        self._adapter = adapter

    def get_demographics(self, mrn: str) -> PatientDemographics:
        """Get the compact CHECK_PAT identity and contact response."""
        return self._adapter.get_demographics(mrn)

    def get_basic_info(self, mrn: str) -> PatientBasicInfo:
        """Get QUY15 basic details and its recorded inpatient context (type A)."""
        return self._adapter.get_basic_info(mrn)

    def get_registration_history(self, mrn: str) -> list[RegistrationRecord]:
        """Get registrations across pages; registration does not prove attendance."""
        return self._adapter.get_registration_history(mrn)
