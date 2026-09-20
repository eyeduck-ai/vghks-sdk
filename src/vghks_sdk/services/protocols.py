"""Structural interfaces consumed by workflows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from ..extension_protocols import (
    EarningsServiceProtocol,
    PatientQueriesProtocol,
    ReviewsProtocol,
    SurgeryCasesProtocol,
    SurgeryQueriesProtocol,
)
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
    VisitFilter,
)


class AuthServiceProtocol(Protocol):
    def login(self) -> None: ...

    def check(self, only: Sequence[str] | None = None) -> AuthCheckReport: ...


class PatientsServiceProtocol(Protocol):
    def get_basic_info(self, mrn: str) -> PatientBasicInfo: ...

    def get_demographics(self, mrn: str) -> PatientDemographics: ...

    def get_registration_history(self, mrn: str) -> list[RegistrationRecord]: ...


class OpdServiceProtocol(Protocol):
    def get_doctor_patients(self, card_no: str, visit_date: date) -> list[OutpatientPatient]: ...


class RecordsServiceProtocol(PatientQueriesProtocol, Protocol):
    def get_visit_cases(self, mrn: str) -> list[VisitCase]: ...

    def find_visit_cases(self, mrn: str, visit_filter: VisitFilter) -> list[VisitCase]: ...

    def get_case_detail(self, case: VisitCase) -> CaseDetail: ...

    def get_soap(self, case: VisitCase) -> SoapRecord: ...

    def get_numeric_report(self, case: VisitCase) -> NumericReport: ...

    def get_numeric_history(
        self, mrn: str, filter: NumericHistoryFilter
    ) -> NumericHistoryReport: ...

    def get_surgery_history(
        self, mrn: str, filter: SurgeryHistoryFilter
    ) -> list[PatientSurgeryRecord]: ...

    def download_surgery_record(self, ref: PdfAttachmentRef) -> BinaryAsset: ...

    def get_consults(self, case: VisitCase) -> list[ConsultRecord]: ...

    def get_treatments(self, case: VisitCase) -> list[TreatmentRecord]: ...


class OrdersServiceProtocol(Protocol):
    def get_case_orders(self, case: VisitCase) -> list[ClinicalOrder]: ...

    def get_order_history(self, mrn: str, filter: OrderHistoryFilter) -> list[ClinicalOrder]: ...

    def get_order_detail(self, ref: OrderDetailRef) -> OrderDetail: ...

    def get_order_report(self, ref: OrderReportRef) -> OrderReport: ...

    def get_pacs_study(self, ref: PacsStudyRef) -> PacsStudy: ...

    def download_pacs_image(self, ref: PacsImageRef) -> BinaryAsset: ...

    def download_pdf(self, ref: PdfAttachmentRef) -> BinaryAsset: ...


class MedicationsServiceProtocol(Protocol):
    def get_case_medications(self, case: VisitCase) -> list[MedicationOrder]: ...

    def get_medication_history(
        self, mrn: str, filter: MedicationHistoryFilter
    ) -> list[MedicationOrder]: ...


class SurgeryServiceProtocol(SurgeryQueriesProtocol, SurgeryCasesProtocol, Protocol):
    def create_schedule(self, command: SurgeryCommand) -> MutationReceipt: ...
    def edit_schedule(self, command: SurgeryCommand) -> MutationReceipt: ...
    def cancel_schedule(self, command: SurgeryCommand) -> MutationReceipt: ...
    def create_consent(self, command: SurgeryCommand) -> MutationReceipt: ...
    def get_schedule(self, card_no: str, start: date, end: date) -> list[SurgeryRecord]: ...


class AuditServiceProtocol(Protocol):
    def get_unsigned_records(self, doctor: str, start: date, end: date) -> list[UnsignedRecord]: ...


class SDKProtocol(Protocol):
    auth: AuthServiceProtocol
    patients: PatientsServiceProtocol
    opd: OpdServiceProtocol
    records: RecordsServiceProtocol
    orders: OrdersServiceProtocol
    medications: MedicationsServiceProtocol
    surgery: SurgeryServiceProtocol
    audit: AuditServiceProtocol
    earnings: EarningsServiceProtocol
    reviews: ReviewsProtocol
