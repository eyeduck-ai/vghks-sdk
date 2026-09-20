from __future__ import annotations

from typing import Any

from ..adapters.protocols import PrqAdapterProtocol
from ..core.errors import ConfigurationError
from ..models import (
    BinaryAsset,
    CaseDetail,
    ConsultRecord,
    NumericHistoryFilter,
    NumericHistoryReport,
    NumericReport,
    OrderReport,
    OrderReportRef,
    PatientSurgeryRecord,
    PdfAttachmentRef,
    SoapRecord,
    SurgeryHistoryFilter,
    TextReportHistory,
    TreatmentRecord,
    UploadHistory,
    VisitCase,
    VisitFilter,
)


class RecordsService:
    def __init__(self, adapter: PrqAdapterProtocol) -> None:
        self._adapter = adapter

    def get_allergy(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_allergy(mrn)

    def get_advance_directives(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_advance_directives(mrn)

    def get_research_flags(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_research_flags(mrn)

    def get_bed_transfers(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_bed_transfers(mrn)

    def get_care_cases(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_care_cases(mrn)

    def get_text_report_history(
        self, mrn: str, department: str, days: int = 3650
    ) -> TextReportHistory:
        return self._adapter.get_text_report_history(mrn, department, days)

    def get_text_report(self, ref: OrderReportRef) -> OrderReport:
        return self._adapter.get_text_report(ref)

    def get_upload_types(self) -> list[dict[str, str]]:
        return self._adapter.get_upload_types()

    def get_upload_history(self, mrn: str, main_type: str = "", days: str = "*") -> UploadHistory:
        return self._adapter.get_upload_history(mrn, main_type, days)

    def get_visit_cases(
        self, mrn: str | None = None, *, national_id: str | None = None
    ) -> list[VisitCase]:
        """Fetch every returned visit by exactly one patient identifier."""
        if national_id is not None:
            return self._adapter.get_visit_cases(mrn, national_id=national_id)
        return self._adapter.get_visit_cases(mrn)

    def find_visit_cases(
        self,
        mrn: str | None = None,
        visit_filter: VisitFilter | None = None,
        *,
        national_id: str | None = None,
    ) -> list[VisitCase]:
        """Fetch a patient list, then apply a reusable local selector."""
        if not isinstance(visit_filter, VisitFilter):
            raise ConfigurationError("find_visit_cases requires a VisitFilter")
        return visit_filter.select(self.get_visit_cases(mrn, national_id=national_id))

    def get_case_detail(self, case: VisitCase) -> CaseDetail:
        return self._adapter.get_case_detail(case)

    def get_soap(self, case: VisitCase) -> SoapRecord:
        return self._adapter.get_soap(case)

    def get_numeric_report(self, case: VisitCase) -> NumericReport:
        return self._adapter.get_numeric_report(case)

    def get_numeric_history(
        self,
        mrn: str,
        filter: NumericHistoryFilter,
    ) -> NumericHistoryReport:
        return self._adapter.get_numeric_history(mrn, filter)

    def get_surgery_history(
        self,
        mrn: str,
        filter: SurgeryHistoryFilter,
    ) -> list[PatientSurgeryRecord]:
        return self._adapter.get_surgery_history(mrn, filter)

    def download_surgery_record(self, ref: PdfAttachmentRef) -> BinaryAsset:
        """Download a history button using the shared PRQ PDF operation."""
        return self._adapter.download_pdf(ref)

    def get_consults(self, case: VisitCase) -> list[ConsultRecord]:
        return self._adapter.get_consults(case)

    def get_treatments(self, case: VisitCase) -> list[TreatmentRecord]:
        return self._adapter.get_treatments(case)
