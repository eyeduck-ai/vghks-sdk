"""Progressive, PHI-free smoke checks for the internal environment."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import AuthenticationError, ConfigurationError, error_code
from ..local_io import read_mrns
from ..models import AuthCheckReport, VisitFilter
from ..services.protocols import SDKProtocol

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    check: str
    status: str
    error_code: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "check": self.check,
            "status": self.status,
            "error_code": self.error_code,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticProbeResult:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def failed_count(self) -> int:
        return sum(check.status in {"ERROR", "MISSING"} for check in self.checks)

    @property
    def status(self) -> str:
        return "OK" if self.failed_count == 0 else "COMPLETED_WITH_ERRORS"


def run_diagnostic_probe(
    sdk: SDKProtocol,
    recorder: DiagnosticRecorder,
    *,
    doctor_card: str | None = None,
    probe_date: date | None = None,
    mrn_input: Path | None = None,
    include_registration: bool = True,
    include_surgery: bool = False,
    include_unsigned: bool = False,
    range_start: date | None = None,
    range_end: date | None = None,
    visit_filter: VisitFilter | None = None,
) -> DiagnosticProbeResult:
    """Run independent checks without writing any clinical result values."""

    if (doctor_card is None) != (probe_date is None):
        raise ValueError("diagnose requires both doctor-card and date when either is used")
    if include_surgery or include_unsigned:
        if doctor_card is None:
            raise ValueError("surgery and unsigned checks require doctor-card")
        range_start = range_start or probe_date
        range_end = range_end or probe_date
        if range_start is None or range_end is None:
            raise ValueError("surgery and unsigned checks require a date or start/end range")
        if range_end < range_start:
            raise ValueError("diagnostic end date must not be before start date")

    visit_filter = visit_filter or VisitFilter(section_name_contains=("眼科",))
    checks: list[DiagnosticCheck] = []
    required_auth_targets = ["prq", "webmaas"]
    if include_surgery:
        required_auth_targets.append("oppl")
    if include_unsigned:
        required_auth_targets.append("audit")

    _, auth_error = _capture(
        recorder,
        checks,
        "auth_primary",
        lambda: _require_auth_ready(sdk, tuple(required_auth_targets)),
        lambda report: {
            "target_count": len(report.targets),
            "reauthenticated": report.reauthenticated,
        },
    )
    if auth_error is not None:
        return DiagnosticProbeResult(tuple(checks))

    opd_patients = None
    if doctor_card is not None and probe_date is not None:
        opd_patients, _ = _capture(
            recorder,
            checks,
            "opd_patient_list",
            lambda: sdk.opd.get_doctor_patients(doctor_card, probe_date),
            lambda rows: {"record_count": len(rows)},
        )

    candidate_mrn: str | None = None
    if mrn_input is not None:
        mrns, _ = _capture(
            recorder,
            checks,
            "mrn_input",
            lambda: _require_mrns(mrn_input),
            lambda rows: {"record_count": len(rows)},
        )
        if mrns:
            candidate_mrn = mrns[0]
    elif opd_patients:
        candidate_mrn = opd_patients[0].mrn

    cases = None
    latest_matching_case = None
    if candidate_mrn is not None:
        _, demographic_error = _capture(
            recorder,
            checks,
            "patient_demographics",
            lambda: sdk.patients.get_demographics(candidate_mrn),
            lambda result: {
                "name_present": bool(result.name),
                "mobile_present": bool(result.mobile_phone),
                "home_phone_present": bool(result.home_phone),
                "birthday_present": bool(result.birthday),
            },
        )
        if isinstance(demographic_error, AuthenticationError):
            return DiagnosticProbeResult(tuple(checks))

        if include_registration:
            _, registration_error = _capture(
                recorder,
                checks,
                "registration_history",
                lambda: sdk.patients.get_registration_history(candidate_mrn),
                lambda rows: {"record_count": len(rows)},
            )
            if isinstance(registration_error, AuthenticationError):
                return DiagnosticProbeResult(tuple(checks))

        cases, case_error = _capture(
            recorder,
            checks,
            "visit_cases",
            lambda: sdk.records.get_visit_cases(candidate_mrn),
            lambda rows: {
                "record_count": len(rows),
                "matching_outpatient_count": len(visit_filter.select(rows)),
            },
        )
        if isinstance(case_error, AuthenticationError):
            return DiagnosticProbeResult(tuple(checks))
        if cases:
            matching_cases = visit_filter.select(cases)
            if matching_cases:
                latest_matching_case = matching_cases[0]

    if latest_matching_case is not None:
        _, detail_error = _capture(
            recorder,
            checks,
            "case_detail",
            lambda: sdk.records.get_case_detail(latest_matching_case),
            lambda result: {
                "tab_id_count": len(result.tab_ids),
                "tab_title_count": len(result.tab_titles),
            },
        )
        if isinstance(detail_error, AuthenticationError):
            return DiagnosticProbeResult(tuple(checks))

        _, soap_error = _capture(
            recorder,
            checks,
            "soap",
            lambda: sdk.records.get_soap(latest_matching_case),
            lambda result: {
                "block_count": len(result.blocks),
                "character_count": sum(len(block) for block in result.blocks),
            },
            classify=lambda result: ("OK", "") if result.blocks else ("MISSING", "SOAP_MISSING"),
        )
        if isinstance(soap_error, AuthenticationError):
            return DiagnosticProbeResult(tuple(checks))

        _, numeric_error = _capture(
            recorder,
            checks,
            "numeric_report",
            lambda: sdk.records.get_numeric_report(latest_matching_case),
            lambda result: {
                "table_count": len(result.tables),
                "row_count": sum(len(table.rows) for table in result.tables),
            },
            classify=lambda result: ("OK", "") if result.tables else ("MISSING", "NUMERIC_MISSING"),
        )
        if isinstance(numeric_error, AuthenticationError):
            return DiagnosticProbeResult(tuple(checks))
    elif candidate_mrn is not None and cases is not None:
        _skip(recorder, checks, "case_detail", "NO_MATCHING_OUTPATIENT_CASE")
        _skip(recorder, checks, "soap", "NO_MATCHING_OUTPATIENT_CASE")
        _skip(recorder, checks, "numeric_report", "NO_MATCHING_OUTPATIENT_CASE")
    elif candidate_mrn is not None:
        _skip(recorder, checks, "case_detail", "VISIT_CASES_UNAVAILABLE")
        _skip(recorder, checks, "soap", "VISIT_CASES_UNAVAILABLE")
        _skip(recorder, checks, "numeric_report", "VISIT_CASES_UNAVAILABLE")
    elif mrn_input is not None or (doctor_card is not None and probe_date is not None):
        _skip(recorder, checks, "patient_demographics", "NO_PATIENT_AVAILABLE")
        if include_registration:
            _skip(recorder, checks, "registration_history", "NO_PATIENT_AVAILABLE")
        _skip(recorder, checks, "visit_cases", "NO_PATIENT_AVAILABLE")
        _skip(recorder, checks, "case_detail", "NO_PATIENT_AVAILABLE")
        _skip(recorder, checks, "soap", "NO_PATIENT_AVAILABLE")
        _skip(recorder, checks, "numeric_report", "NO_PATIENT_AVAILABLE")

    if include_surgery and doctor_card is not None and range_start and range_end:
        _, surgery_error = _capture(
            recorder,
            checks,
            "surgery_schedule",
            lambda: sdk.surgery.get_schedule(doctor_card, range_start, range_end),
            lambda rows: {"record_count": len(rows)},
        )
        if isinstance(surgery_error, AuthenticationError):
            return DiagnosticProbeResult(tuple(checks))

    if include_unsigned and doctor_card is not None and range_start and range_end:
        _capture(
            recorder,
            checks,
            "unsigned_records",
            lambda: sdk.audit.get_unsigned_records(doctor_card, range_start, range_end),
            lambda rows: {"record_count": len(rows)},
        )

    return DiagnosticProbeResult(tuple(checks))


def _require_auth_ready(sdk: SDKProtocol, targets: tuple[str, ...]) -> AuthCheckReport:
    report = sdk.auth.check(only=targets)
    if not report.ok:
        raise AuthenticationError("required authentication readiness targets are not OK")
    return report


def _capture(
    recorder: DiagnosticRecorder,
    checks: list[DiagnosticCheck],
    name: str,
    operation: Callable[[], T],
    summarize: Callable[[T], Mapping[str, Any]],
    classify: Callable[[T], tuple[str, str]] | None = None,
) -> tuple[T | None, Exception | None]:
    try:
        result = operation()
        details = summarize(result)
    except Exception as exc:
        check = DiagnosticCheck(name, "ERROR", error_code(exc), {})
        checks.append(check)
        recorder.record_probe_step(name=name, status="ERROR", error=exc)
        return None, exc
    status, result_error_code = classify(result) if classify is not None else ("OK", "")
    check = DiagnosticCheck(name, status, result_error_code, details)
    checks.append(check)
    recorder.record_probe_step(
        name=name,
        status=status,
        error_code_value=result_error_code,
        details=details,
    )
    return result, None


def _skip(
    recorder: DiagnosticRecorder,
    checks: list[DiagnosticCheck],
    name: str,
    reason: str,
) -> None:
    details = {"reason": reason}
    checks.append(DiagnosticCheck(name, "SKIPPED", "", details))
    recorder.record_probe_step(name=name, status="SKIPPED", details=details)


def _require_mrns(path: Path) -> list[str]:
    values = read_mrns(path)
    if not values:
        raise ConfigurationError("medical record number input did not contain any records")
    return values
