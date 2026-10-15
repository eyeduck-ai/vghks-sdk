"""Opt-in typed views of existing JSON results; no HTTP or retries."""

from __future__ import annotations

from typing import Literal, cast, overload

from .core.errors import ConfigurationError
from .models.json_responses import (
    AdvanceDirectiveData,
    AllergyData,
    BedTransfersData,
    CareCasesData,
    PatientFlagData,
    ResearchFlagsData,
    ReviewOptionData,
    ReviewOptionsData,
    SurgeryConsentCatalogData,
    SurgeryConsentDoctorData,
    SurgeryConsentTemplateData,
    SurgeryHolidaysData,
    SurgeryMedicationData,
    SurgeryPatientConsentsData,
    SurgeryPatientData,
    SurgeryPayloadData,
    SurgeryProcedureCatalogData,
    SurgeryRequestNumbersData,
    SurgerySupplyData,
)
from .parsing.oppl import OPPL_JSON_FIELDS, parse_oppl_payload
from .parsing.prq_extensions import parse_patient_flags
from .parsing.review import parse_review_doctors, parse_review_options


@overload
def as_patient_flags(key: Literal["prq.allergy", "allergy"], value: object, mrn: str) -> AllergyData: ...


@overload
def as_patient_flags(key: Literal["prq.advance_directives", "advance_directives"], value: object, mrn: str) -> AdvanceDirectiveData: ...


@overload
def as_patient_flags(key: Literal["prq.research_flags", "research_flags"], value: object, mrn: str) -> ResearchFlagsData: ...


@overload
def as_patient_flags(key: Literal["prq.bed_transfers", "bed_transfers"], value: object, mrn: str) -> BedTransfersData: ...


@overload
def as_patient_flags(key: Literal["prq.care_cases", "care_cases"], value: object, mrn: str) -> CareCasesData: ...


@overload
def as_patient_flags(key: str, value: object, mrn: str) -> PatientFlagData: ...


def as_patient_flags(key: str, value: object, mrn: str) -> PatientFlagData:
    """Validate the recorded shape/context, keeping every value and unknown key."""
    if key.removeprefix("prq.") not in {
        "allergy", "advance_directives", "research_flags", "bed_transfers", "care_cases",
    }:
        raise ConfigurationError("unsupported patient response", code="RESPONSE_OPERATION_UNSUPPORTED")
    return cast(PatientFlagData, parse_patient_flags(key, value, mrn))


@overload
def as_surgery_payload(key: Literal["oppl.patient_info", "patient_info"], value: object, *, mrn: str = "") -> SurgeryPatientData: ...


@overload
def as_surgery_payload(key: Literal["oppl.patient_consents", "patient_consents"], value: object, *, mrn: str = "") -> SurgeryPatientConsentsData: ...


@overload
def as_surgery_payload(key: Literal["oppl.request_numbers", "request_numbers"], value: object, *, mrn: str = "") -> SurgeryRequestNumbersData: ...


@overload
def as_surgery_payload(key: Literal["oppl.anticoagulants", "anticoagulants", "oppl.sglt2", "sglt2"], value: object, *, mrn: str = "") -> SurgeryMedicationData: ...


@overload
def as_surgery_payload(key: Literal["oppl.procedure_catalog", "procedure_catalog"], value: object, *, mrn: str = "") -> SurgeryProcedureCatalogData: ...


@overload
def as_surgery_payload(key: Literal["oppl.holidays", "holidays"], value: object, *, mrn: str = "") -> SurgeryHolidaysData: ...


@overload
def as_surgery_payload(key: Literal["oppl.supply_model", "supply_model"], value: object, *, mrn: str = "") -> SurgerySupplyData: ...


@overload
def as_surgery_payload(key: Literal["oppl.consent_catalog", "consent_catalog"], value: object, *, mrn: str = "") -> SurgeryConsentCatalogData: ...


@overload
def as_surgery_payload(key: Literal["oppl.consent_template", "consent_template"], value: object, *, mrn: str = "") -> SurgeryConsentTemplateData: ...


@overload
def as_surgery_payload(key: Literal["oppl.consent_doctor", "consent_doctor"], value: object, *, mrn: str = "") -> SurgeryConsentDoctorData: ...


@overload
def as_surgery_payload(key: str, value: object, *, mrn: str = "") -> SurgeryPayloadData: ...


def as_surgery_payload(key: str, value: object, *, mrn: str = "") -> SurgeryPayloadData:
    """Validate known OPPL containers without classifying status/medication codes."""
    if key.removeprefix("oppl.") not in OPPL_JSON_FIELDS:
        raise ConfigurationError("unsupported surgery response", code="RESPONSE_OPERATION_UNSUPPORTED")
    return cast(SurgeryPayloadData, parse_oppl_payload(key, value, mrn))


def as_review_options(value: object) -> ReviewOptionsData:
    """Validate selectors and preserve additional source fields/options."""
    return cast(ReviewOptionsData, parse_review_options(value))


def as_review_doctors(value: object) -> list[ReviewOptionData]:
    """Return validated Value/Text choices, retaining unknown row fields."""
    return cast(list[ReviewOptionData], parse_review_doctors(value))
