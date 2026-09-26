"""Typed records data for the public SDK."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from ..core.errors import ConfigurationError, ErrorInfo
from ._validation import _history_lookback, _normalize_code, _safe_filter_value, _unique_normalized


@dataclass(frozen=True, slots=True)
class OutpatientPatient:
    mrn: str
    name: str
    visit_date: date
    sex: str = ""
    age: str = ""
    section_code: str = ""
    room: str = ""
    doctor_card: str = ""
    doctor_label_present: bool = False
    sequence_no: str = ""


@dataclass(frozen=True, slots=True)
class VisitCase:
    mrn: str
    visit_date: date | None
    case_type: str
    case_no: str
    section_code: str
    section_name: str
    index: int | None = None
    detail_params: Mapping[str, str] | None = None
    doctor_name: str = ""
    doctor_card: str = ""
    # The MRN used to obtain the patient-scoped list.  `mrn` remains the
    # original MRN from this visit's detail link, including legacy aliases.
    lookup_mrn: str = ""

    @property
    def patient_mrn(self) -> str:
        """MRN of the patient lookup that returned this visit."""

        return self.lookup_mrn or self.mrn

    @property
    def case_type_label(self) -> str:
        return {"O": "門診", "A": "住院", "E": "急診"}.get(self.case_type, self.case_type)

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.mrn,
            self.case_type,
            self.case_no,
            self.visit_date.isoformat() if self.visit_date else "",
            self.section_code,
        )


@dataclass(frozen=True, slots=True)
class VisitFilter:
    """Local selector over the complete case list returned by PRQ.

    Section-name and section-code selectors use OR semantics.  That result is
    then combined with case type, physician and the inclusive date range.
    Selecting a case type does not imply every downstream report supports it.
    """

    section_name_contains: tuple[str, ...] = ()
    section_codes: tuple[str, ...] = ()
    all_sections: bool = False
    case_types: tuple[str, ...] = ("O",)
    start_date: date | None = None
    end_date: date | None = None
    doctor_names: tuple[str, ...] = ()
    doctor_name_contains: tuple[str, ...] = ()
    # Compatibility filter for an optional raw vsNo value. Patient-history
    # physician selection uses names; resolve employee cards via personnel.
    doctor_cards: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        names = _unique_normalized(self.section_name_contains, mode="name")
        codes = _unique_normalized(self.section_codes, mode="code")
        case_types = _unique_normalized(self.case_types, mode="code")
        object.__setattr__(self, "section_name_contains", names)
        object.__setattr__(self, "section_codes", codes)
        object.__setattr__(self, "case_types", case_types)
        for field_name in ("doctor_names", "doctor_name_contains", "doctor_cards"):
            object.__setattr__(
                self,
                field_name,
                _unique_normalized(
                    getattr(self, field_name),
                    mode="code" if field_name == "doctor_cards" else "name",
                ),
            )

        if not case_types or set(case_types) - {"O", "A", "E"}:
            raise ConfigurationError(
                "case types must be O (outpatient), A (inpatient) or E (emergency)"
            )
        if self.all_sections and (names or codes):
            raise ConfigurationError("all_sections cannot be combined with section names or codes")
        if not self.all_sections and not (names or codes):
            raise ConfigurationError("choose at least one section name/code or enable all_sections")
        if (self.start_date is None) != (self.end_date is None):
            raise ConfigurationError("case start and end dates must be provided together")
        if self.start_date is not None and not isinstance(self.start_date, date):
            raise ConfigurationError("case start date must be a date")
        if self.end_date is not None and not isinstance(self.end_date, date):
            raise ConfigurationError("case end date must be a date")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ConfigurationError("case end date must not be before case start date")

    def matches(self, case: VisitCase) -> bool:
        case_type = _normalize_code(case.case_type)
        if case_type not in self.case_types:
            return False
        if self.start_date is not None and self.end_date is not None:
            if case.visit_date is None:
                return False
            if not self.start_date <= case.visit_date <= self.end_date:
                return False
        if self.doctor_names or self.doctor_name_contains or self.doctor_cards:
            doctor_name = unicodedata.normalize("NFKC", case.doctor_name).strip().casefold()
            doctor_match = (
                doctor_name in {name.casefold() for name in self.doctor_names}
                or any(name.casefold() in doctor_name for name in self.doctor_name_contains)
                or (
                    bool(case.doctor_card)
                    and _normalize_code(case.doctor_card) in self.doctor_cards
                )
            )
            if not doctor_match:
                return False
        if self.all_sections:
            return True
        section_name = unicodedata.normalize("NFKC", case.section_name).casefold()
        name_match = any(name.casefold() in section_name for name in self.section_name_contains)
        code_match = _normalize_code(case.section_code) in self.section_codes
        return name_match or code_match

    def select(self, cases: Iterable[VisitCase]) -> list[VisitCase]:
        """Filter, deduplicate by case identity, and sort newest first."""

        unique: dict[tuple[str, str, str, str, str], VisitCase] = {}
        for case in cases:
            if self.matches(case):
                unique.setdefault(case.identity, case)
        return sorted(
            unique.values(),
            key=lambda item: (
                item.visit_date or date.min,
                -(item.index if item.index is not None else 10**9),
            ),
            reverse=True,
        )


@dataclass(frozen=True, slots=True)
class CaseDetail:
    case: VisitCase
    tab_ids: tuple[str, ...] = ()
    tab_titles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SoapDiagnosis:
    """A diagnosis printed in SOAP, without inferring priority or ICD edition."""

    code: str
    name: str
    coding_system: str = "ICD"
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class SoapOrder:
    """A printed SOAP order summary; use ClinicalOrder for report navigation."""

    name: str
    quantity: str
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class SoapMedication:
    """A printed SOAP prescription, preserving doses and quantities as strings."""

    name: str
    dose: str
    unit: str
    route: str
    frequency: str
    days: str
    total_quantity: str
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class SoapChronicPrescriptionPeriod:
    """The printed medication-use period of a chronic prescription notice."""

    start_date: date
    end_date: date
    source_block_index: int
    source_label: str = "服藥期限"
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class SoapRecord:
    case: VisitCase
    blocks: tuple[str, ...]
    # None means no identified label; "" means an explicitly empty section.
    subjective: str | None = None
    objective: str | None = None
    assessment_plan: str | None = None
    assessment: str | None = None
    plan: str | None = None
    diagnoses: tuple[SoapDiagnosis, ...] = ()
    orders: tuple[SoapOrder, ...] = ()
    medications: tuple[SoapMedication, ...] = ()
    present_sections: tuple[str, ...] = ()
    unclassified_blocks: tuple[str, ...] = ()
    parsing_issues: tuple[str, ...] = ()
    chronic_prescription_periods: tuple[SoapChronicPrescriptionPeriod, ...] = ()

    @property
    def full_text(self) -> str:
        return "\n\n".join(block for block in self.blocks if block).strip()


@dataclass(frozen=True, slots=True)
class NumericTable:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    # Raw header rows retain the layout that ``headers`` historically flattens.
    header_rows: tuple[tuple[str, ...], ...] = ()
    # One label path per value column, only when the source can be aligned.
    column_paths: tuple[tuple[str, ...], ...] = ()
    parsing_issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NumericReport:
    case: VisitCase
    tables: tuple[NumericTable, ...]


@dataclass(frozen=True, slots=True)
class NumericHistoryFilter:
    lookback_days: int | str = "all"
    department: str = "*"
    subtype: str = "*"

    def __post_init__(self) -> None:
        object.__setattr__(self, "lookback_days", _history_lookback(self.lookback_days, 3650))
        object.__setattr__(self, "department", _safe_filter_value(self.department))
        object.__setattr__(self, "subtype", _safe_filter_value(self.subtype))


@dataclass(frozen=True, slots=True)
class NumericHistoryReport:
    mrn: str
    tables: tuple[NumericTable, ...]


@dataclass(frozen=True, slots=True)
class ConsultRecord:
    case: VisitCase
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class TreatmentRecord:
    case: VisitCase
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class UnsignedRecord:
    category: str
    headers: tuple[str, ...]
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LatestRecordBundle:
    schema_version: int
    mrn: str
    status: str
    case: VisitCase | None
    soap: SoapRecord | None
    numeric_report: NumericReport | None
    issues: tuple[ErrorInfo, ...] = ()

    @property
    def error_code(self) -> str:
        return ";".join(item.code for item in self.issues)


@dataclass(frozen=True, slots=True)
class VisitHistoryRecord:
    schema_version: int
    mrn: str
    status: str
    case: VisitCase | None
    soap_status: str
    numeric_status: str
    soap: SoapRecord | None
    numeric_report: NumericReport | None
    issues: tuple[ErrorInfo, ...] = ()

    @property
    def error_code(self) -> str:
        return ";".join(item.code for item in self.issues)
