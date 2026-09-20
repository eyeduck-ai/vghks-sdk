"""Structural contracts between typed services and domain adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from ..extension_protocols import PatientQueriesProtocol, SurgeryQueriesProtocol
from ..models import (
    AuthCheckReport,
    BinaryAsset,
    CaseDetail,
    ClinicalOrder,
    ConsultRecord,
    MedicationHistoryFilter,
    MedicationOrder,
    MutationReceipt,
    NumericHistoryFilter,
    NumericHistoryReport,
    NumericReport,
    OrderDetail,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReport,
    OrderReportRef,
    OutpatientPatient,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PatientBasicInfo,
    PatientDemographics,
    PatientSurgeryRecord,
    PdfAttachmentRef,
    RegistrationRecord,
    SoapRecord,
    SurgeryCommand,
    SurgeryHistoryFilter,
    SurgeryRecord,
    TreatmentRecord,
    UnsignedRecord,
    VisitCase,
)


class AuthAdapterProtocol(Protocol):
    def login(self) -> None: ...

    def auth_check(self, only: Sequence[str] | None = None) -> AuthCheckReport: ...


class WebMaasAdapterProtocol(Protocol):
    def get_basic_info(self, mrn: str) -> PatientBasicInfo: ...

    def get_demographics(self, mrn: str) -> PatientDemographics: ...

    def get_registration_history(self, mrn: str) -> list[RegistrationRecord]: ...


class PrqAdapterProtocol(PatientQueriesProtocol, Protocol):
    def get_doctor_patients(
        self,
        doctor_card: str,
        visit_date: date,
    ) -> list[OutpatientPatient]: ...

    def get_visit_cases(self, mrn: str) -> list[VisitCase]: ...

    def get_case_detail(self, case: VisitCase) -> CaseDetail: ...

    def get_soap(self, case: VisitCase) -> SoapRecord: ...

    def get_numeric_report(self, case: VisitCase) -> NumericReport: ...

    def get_case_orders(self, case: VisitCase) -> list[ClinicalOrder]: ...

    def get_order_history(
        self, mrn: str, history_filter: OrderHistoryFilter
    ) -> list[ClinicalOrder]: ...

    def get_order_detail(self, reference: OrderDetailRef) -> OrderDetail: ...

    def get_order_report(self, reference: OrderReportRef) -> OrderReport: ...

    def get_pacs_study(self, reference: PacsStudyRef) -> PacsStudy: ...

    def download_pacs_image(self, reference: PacsImageRef) -> BinaryAsset: ...

    def download_pdf(self, reference: PdfAttachmentRef) -> BinaryAsset: ...

    def get_case_medications(self, case: VisitCase) -> list[MedicationOrder]: ...

    def get_medication_history(
        self, mrn: str, history_filter: MedicationHistoryFilter
    ) -> list[MedicationOrder]: ...

    def get_numeric_history(
        self, mrn: str, history_filter: NumericHistoryFilter
    ) -> NumericHistoryReport: ...

    def get_surgery_history(
        self, mrn: str, history_filter: SurgeryHistoryFilter
    ) -> list[PatientSurgeryRecord]: ...

    def get_consults(self, case: VisitCase) -> list[ConsultRecord]: ...

    def get_treatments(self, case: VisitCase) -> list[TreatmentRecord]: ...


class OpplAdapterProtocol(SurgeryQueriesProtocol, Protocol):
    def submit_command(self, command: SurgeryCommand) -> MutationReceipt: ...
    def get_schedule(
        self,
        doctor_card: str,
        start: date,
        end: date,
        **filters: str,
    ) -> list[SurgeryRecord]: ...


class AuditAdapterProtocol(Protocol):
    def get_unsigned_records(
        self,
        doctor_card: str,
        start: date,
        end: date,
    ) -> list[UnsignedRecord]: ...
