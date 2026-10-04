"""Interpret supported typed results without HTTP or application retry policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import is_dataclass
from typing import TypeVar

from .core.errors import ErrorInfo, NotFoundError, SDKError, error_info
from .models.acquisition import AcquisitionResult, DataAssessment
from .models.assets import BinaryAsset, PacsStudy
from .models.auth import AuthCheckReport
from .models.documents import HtmlDocument, TextReportHistory, UploadHistory
from .models.orders import ClinicalOrder, OrderReport
from .models.records import NumericHistoryReport, NumericReport, NumericTable, SoapRecord
from .order_status import classify_order_execution

T = TypeVar("T")


def assess_data(value: object) -> DataAssessment:
    """Describe an already parsed SDK read result; do not guess opaque JSON schemas."""

    availability, count, complete = "UNKNOWN", None, None
    issues: tuple[ErrorInfo, ...] = ()
    warnings: tuple[ErrorInfo, ...] = ()
    if value is None:
        availability, count, complete = "EMPTY", 0, True
    elif isinstance(value, (list, tuple)):
        children = tuple(assess_data(item) for item in value)
        availability = "AVAILABLE" if value else "EMPTY"
        if children and all(item.availability == "NOT_EXECUTED" for item in children):
            availability = "NOT_EXECUTED"
        count, complete = (
            len(value),
            (None if any(item.complete is None for item in children) else True),
        )
        issues = tuple(issue for item in children for issue in item.issues)
        warnings = tuple(warning for item in children for warning in item.warnings)
        if any(item.complete is False for item in children):
            complete = False
    elif isinstance(value, BinaryAsset):
        availability, count, complete = (
            "BINARY_AVAILABLE" if value.size else "EMPTY",
            1 if value.size else 0,
            True,
        )
    elif isinstance(value, OrderReport):
        # UNASSESSED/unknown status must not silently become an empty report.
        availability = {
            "TEXT_AVAILABLE": "AVAILABLE",
            "ATTACHMENT_ONLY": "ATTACHMENT_ONLY",
            "METADATA_ONLY": "METADATA_ONLY",
            "EMPTY": "EMPTY",
        }.get(value.report_data_status, "UNKNOWN")
        count = 0 if availability == "EMPTY" else None
        complete = None if availability == "UNKNOWN" else True
        issues = _parsing_issues(value.text_extraction_notes)
    elif isinstance(value, SoapRecord):
        availability = (
            "AVAILABLE"
            if value.full_text
            else "ATTACHMENT_ONLY"
            if value.scanned_pdf_refs
            else "EMPTY"
        )
        count, complete = len(value.blocks), True
        issues = _parsing_issues(value.parsing_issues)
    elif isinstance(value, (NumericReport, NumericHistoryReport)):
        children = tuple(assess_data(table) for table in value.tables)
        count = sum(item.item_count or 0 for item in children)
        availability, complete = "AVAILABLE" if count else "EMPTY", True
        issues = tuple(issue for item in children for issue in item.issues)
        warnings = tuple(warning for item in children for warning in item.warnings)
    elif isinstance(value, NumericTable):
        count, complete = len(value.rows), True
        availability = "AVAILABLE" if count else "EMPTY"
        recoverable = {"NUMERIC_HEADER_SPAN_MISMATCH"} if _aligned_eye_table(value) else set()
        issues = _parsing_issues(tuple(code for code in value.parsing_issues if code not in recoverable))
        warnings = _parsing_issues(tuple(code for code in value.parsing_issues if code in recoverable))
    elif isinstance(value, PacsStudy):
        availability = (
            "ATTACHMENT_ONLY"
            if value.images
            else "EMPTY"
            if value.data_status == "EMPTY"
            else "UNKNOWN"
        )
        count, complete = len(value.images), None if availability == "UNKNOWN" else True
    elif isinstance(value, (UploadHistory, TextReportHistory)):
        refs = value.pdf_refs if isinstance(value, UploadHistory) else value.report_refs
        availability, count, complete = "AVAILABLE" if refs else "EMPTY", len(refs), True
        issues = _parsing_issues(value.document.rendering_notes)
    elif isinstance(value, ClinicalOrder):
        availability = (
            "NOT_EXECUTED"
            if classify_order_execution(value.status) == "NOT_EXECUTED"
            else "AVAILABLE"
        )
        count, complete = 1, True
    elif isinstance(value, HtmlDocument):
        issues = _parsing_issues(value.rendering_notes)
    elif isinstance(value, AuthCheckReport):
        complete = value.ok
        issues = tuple(target.issue for target in value.targets if target.issue is not None)
    elif is_dataclass(value) and type(value).__module__.startswith("vghks_sdk.models."):
        availability, count, complete = "AVAILABLE", 1, True
        issues = _parsing_issues(getattr(value, "parsing_issues", ()))
        issues += tuple(
            item for item in getattr(value, "issues", ()) if isinstance(item, ErrorInfo)
        )
    if issues:
        complete = False
    return DataAssessment(
        availability, count, complete, tuple(dict.fromkeys(issues)), tuple(dict.fromkeys(warnings))
    )


def acquire(operation: Callable[[], T]) -> AcquisitionResult[T]:
    """Call a read once and preserve its typed value or safe SDK failure metadata.

    Unexpected programming exceptions propagate. Existing Runtime recovery and
    transport retry still apply. This helper adds neither retries nor HTTP calls.
    """

    try:
        value = operation()
    except NotFoundError as exc:
        return AcquisitionResult(
            "EMPTY", data=DataAssessment("NOT_FOUND", 0, True), error=error_info(exc)
        )
    except SDKError as exc:
        return AcquisitionResult("ERROR", error=error_info(exc))
    data = assess_data(value)
    if isinstance(value, AuthCheckReport) and not value.ok:
        return AcquisitionResult(
            "ERROR",
            value=value,
            data=data,
            error=next(iter(data.issues), ErrorInfo("AUTH_READINESS_INCOMPLETE", "AUTHENTICATION")),
        )
    status = (
        "PARTIAL" if data.complete is False else "EMPTY" if data.availability == "EMPTY" else "OK"
    )
    return AcquisitionResult(status, value=value, data=data)


def _parsing_issues(codes: tuple[str, ...]) -> tuple[ErrorInfo, ...]:
    return tuple(ErrorInfo(code, "PARSE", phase="PARSE") for code in codes)


def _aligned_eye_table(table: NumericTable) -> bool:
    """A stale group colspan is recoverable only with an exact OD/OS cell grid."""

    if len(table.header_rows) != 2 or tuple(map(len, table.header_rows)) != (2, 2):
        return False
    first, second = table.header_rows
    if first[0] != "日期" or not first[1] or set(second) != {"OD", "OS"}:
        return False
    if table.column_paths != (("日期",), (first[1], second[0]), (first[1], second[1])):
        return False
    return bool(table.rows) and all(len(row) == 3 for row in table.rows)
