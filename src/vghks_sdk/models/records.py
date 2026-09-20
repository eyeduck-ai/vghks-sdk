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
    """Validated selector for the currently supported outpatient case type.

    Section-name and section-code selectors use OR semantics.  That result is
    then combined with case type and the optional inclusive date range.
    """

    section_name_contains: tuple[str, ...] = ()
    section_codes: tuple[str, ...] = ()
    all_sections: bool = False
    case_types: tuple[str, ...] = ("O",)
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        names = _unique_normalized(self.section_name_contains, mode="name")
        codes = _unique_normalized(self.section_codes, mode="code")
        case_types = _unique_normalized(self.case_types, mode="code")
        object.__setattr__(self, "section_name_contains", names)
        object.__setattr__(self, "section_codes", codes)
        object.__setattr__(self, "case_types", case_types)

        if case_types != ("O",):
            raise ConfigurationError("only outpatient case type O is supported in this SDK version")
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
class SoapRecord:
    case: VisitCase
    blocks: tuple[str, ...]

    @property
    def full_text(self) -> str:
        return "\n\n".join(block for block in self.blocks if block).strip()


@dataclass(frozen=True, slots=True)
class NumericTable:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


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
