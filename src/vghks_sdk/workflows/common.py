from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from ..core.errors import AuthenticationError, ErrorInfo, error_info
from ..models import NumericReport, SoapRecord, VisitCase
from ..services.protocols import RecordsServiceProtocol


@dataclass(frozen=True, slots=True)
class CaseParts:
    soap: SoapRecord | None
    numeric: NumericReport | None
    soap_status: str
    numeric_status: str
    status: str
    issues: tuple[ErrorInfo, ...]


def fetch_case_parts(records: RecordsServiceProtocol, case: VisitCase) -> CaseParts:
    """Fetch SOAP and numeric data independently with shared status semantics."""

    soap: SoapRecord | None = None
    numeric: NumericReport | None = None
    issues: list[ErrorInfo] = []
    try:
        soap = records.get_soap(case)
    except AuthenticationError:
        raise
    except Exception as exc:
        soap_status = "ERROR"
        issues.append(issue_from_exception(exc, prefix="SOAP"))
    else:
        soap_status = "OK" if soap.blocks else "SOAP_MISSING"
        if soap_status != "OK":
            issues.append(issue_from_code("SOAP_MISSING"))

    try:
        numeric = records.get_numeric_report(case)
    except AuthenticationError:
        raise
    except Exception as exc:
        numeric_status = "ERROR"
        issues.append(issue_from_exception(exc, prefix="NUMERIC"))
    else:
        numeric_status = "OK" if numeric.tables else "NUMERIC_MISSING"
        if numeric_status != "OK":
            issues.append(issue_from_code("NUMERIC_MISSING"))

    return CaseParts(
        soap=soap,
        numeric=numeric,
        soap_status=soap_status,
        numeric_status=numeric_status,
        status=case_parts_status(
            soap_status=soap_status,
            numeric_status=numeric_status,
        ),
        issues=tuple(issues),
    )


def case_parts_status(*, soap_status: str, numeric_status: str) -> str:
    if soap_status == "ERROR" and numeric_status == "ERROR":
        return "ERROR"
    if "ERROR" in {soap_status, numeric_status}:
        return "PARTIAL_ERROR"
    if soap_status == "SOAP_MISSING" and numeric_status == "NUMERIC_MISSING":
        return "SOAP_AND_NUMERIC_MISSING"
    if soap_status == "SOAP_MISSING":
        return "SOAP_MISSING"
    if numeric_status == "NUMERIC_MISSING":
        return "NUMERIC_MISSING"
    return "OK"


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def check_output_targets(paths: Iterable[Path], *, overwrite: bool) -> None:
    if overwrite:
        return
    if any(path.exists() for path in paths):
        raise ValueError("output files already exist; choose a new directory or pass --overwrite")


def issue_from_exception(exc: BaseException, *, prefix: str = "") -> ErrorInfo:
    info = error_info(exc)
    code = f"{prefix}_{info.code}" if prefix else info.code
    return replace(info, code=code[:80])


def issue_from_code(code: str, *, category: str = "DATA") -> ErrorInfo:
    return ErrorInfo(code=code, category=category)


def issue_codes(issues: Iterable[ErrorInfo]) -> str:
    return ";".join(dict.fromkeys(item.code for item in issues))
