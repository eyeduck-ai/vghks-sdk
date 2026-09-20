"""Typed orders data for the public SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from ..core.errors import ConfigurationError
from ._validation import (
    _history_lookback,
    _normalize_code,
    _normalize_filter_code,
    _require_navigation_values,
    _validate_navigation_ref,
)
from .assets import PacsStudyRef, PdfAttachmentRef


class OrderCategory(str, Enum):
    """Documented PRQ order categories.

    The friendly ``DEPARTMENTAL`` name deliberately maps to the historical
    PRQ ``OR`` value captured by the UI.
    """

    ALL = "*"
    LABORATORY = "LAB"
    PATHOLOGY = "PATH"
    RADIOLOGY = "RAD"
    NUCLEAR_MEDICINE = "NM"
    WARD = "WARD"
    DEPARTMENTAL = "OR"
    COMBINED = "CMB"
    BLOOD_BANK = "BBK"
    THERAPEUTIC_DRUG_MONITORING = "TDM"


_ORDER_CATEGORIES = {item.value for item in OrderCategory}


_ORDER_STATUSES = {"*", "A", "B", "C", "D"}


_ORDER_SUBTYPES = {"*", "01", "02", "03", "04", "05", "06", "07", "08"}


@dataclass(frozen=True, slots=True)
class OrderHistoryFilter:
    lookback_days: int | str = "all"
    category: str | OrderCategory = OrderCategory.ALL
    status: str = "*"
    subtype: str = "*"
    order_date: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lookback_days", _history_lookback(self.lookback_days, 4000))
        category_value: Any = self.category
        category_name = _normalize_code(category_value)
        if category_name in OrderCategory.__members__:
            category_value = OrderCategory[category_name]
        category = _normalize_filter_code(category_value)
        status = _normalize_filter_code(self.status)
        subtype = _normalize_filter_code(self.subtype)
        if category not in _ORDER_CATEGORIES:
            raise ConfigurationError("unsupported order category", code="ORDER_CATEGORY_INVALID")
        if status not in _ORDER_STATUSES:
            raise ConfigurationError("unsupported order status", code="ORDER_STATUS_INVALID")
        if subtype not in _ORDER_SUBTYPES:
            raise ConfigurationError("unsupported order subtype", code="ORDER_SUBTYPE_INVALID")
        if self.order_date is not None and not isinstance(self.order_date, date):
            raise ConfigurationError("order date must be a date", code="ORDER_DATE_INVALID")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "subtype", subtype)


@dataclass(frozen=True, slots=True)
class OrderDetailRef:
    mrn: str
    case_no: str
    case_type: str
    sequence_no: str
    result_type: str = ""
    order_step: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        _validate_navigation_ref(
            self.mrn,
            self.case_no,
            self.case_type,
            self.sequence_no,
            self.result_type,
            self.order_step,
            self.source,
        )
        _require_navigation_values(self.case_no, self.case_type, self.sequence_no)


@dataclass(frozen=True, slots=True)
class OrderReportRef:
    mrn: str
    case_no: str
    case_type: str
    sequence_no: str
    department: str = ""
    result_type: str = ""
    fee_code: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        _validate_navigation_ref(
            self.mrn,
            self.case_no,
            self.case_type,
            self.sequence_no,
            self.department,
            self.result_type,
            self.fee_code,
            self.source,
        )
        _require_navigation_values(self.case_no, self.case_type, self.sequence_no)


@dataclass(frozen=True, slots=True)
class ClinicalOrder:
    mrn: str
    case_no: str
    case_type: str
    name: str
    order_date: str = ""
    execution_date: str = ""
    requester: str = ""
    status: str = ""
    attachment: str = ""
    detail_ref: OrderDetailRef | None = None
    report_ref: OrderReportRef | None = None
    pacs_ref: PacsStudyRef | None = None
    extra: Mapping[str, Any] | None = None
    pdf_refs: tuple[PdfAttachmentRef, ...] = ()

    @property
    def identity(self) -> tuple[str, ...]:
        ref = self.detail_ref or self.report_ref
        return (
            self.mrn,
            self.case_type,
            self.case_no,
            ref.sequence_no if ref else "",
            self.name,
            self.order_date,
        )


@dataclass(frozen=True, slots=True)
class OrderDetail:
    reference: OrderDetailRef
    fields: Mapping[str, str]
    report_refs: tuple[OrderReportRef, ...] = ()
    pacs_refs: tuple[PacsStudyRef, ...] = ()
    pdf_refs: tuple[PdfAttachmentRef, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderReport:
    reference: OrderReportRef
    fields: Mapping[str, str]
    pdf_refs: tuple[PdfAttachmentRef, ...] = ()
    pacs_refs: tuple[PacsStudyRef, ...] = ()
    report_text: str = ""
    report_data_status: str = "UNASSESSED"
    text_extraction_notes: tuple[str, ...] = ()
