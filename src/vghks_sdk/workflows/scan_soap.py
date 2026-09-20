"""Generic, local-only screening of selected outpatient SOAP text."""

from __future__ import annotations

import csv
from collections import Counter, OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from re import Pattern
from typing import Any

from ..core.errors import AuthenticationError, ConfigurationError, ErrorInfo
from ..local_io import write_json_atomic, write_jsonl_line
from ..models import VisitCase, VisitFilter, to_jsonable
from ..search import (
    DoctorOpdPatientSource,
    MrnPatientSource,
    SoapMatchEvidence,
    SoapScanMatch,
    SoapSearch,
    evaluate_soap_search,
)
from ..services.protocols import SDKProtocol
from .common import (
    check_output_targets,
    date_range,
    issue_from_code,
    issue_from_exception,
)

SOAP_SCAN_SCHEMA_VERSION = 4


@dataclass(frozen=True, slots=True)
class SoapScanResult:
    patient_count: int
    match_count: int
    error_count: int
    matches_path: Path
    index_path: Path
    status_path: Path
    manifest_path: Path


@dataclass(slots=True)
class _PatientCandidate:
    mrn: str
    fallback_name: str = ""
    source_opd_dates: set[date] | None = None

    def sorted_source_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self.source_opd_dates or ()))


@dataclass(frozen=True, slots=True)
class _MatchDraft:
    case: VisitCase
    evidence: SoapMatchEvidence


def scan_soap(
    sdk: SDKProtocol,
    *,
    source: DoctorOpdPatientSource | MrnPatientSource,
    search: SoapSearch,
    visit_filter: VisitFilter,
    output_dir: Path,
    all_matches: bool = False,
    max_patients: int | None = None,
    overwrite: bool = False,
) -> SoapScanResult:
    """Screen each selected patient's filtered SOAP and save local evidence.

    Source acquisition is completed before any output is opened.  In
    particular, a failed day in a doctor OPD range aborts the workflow instead
    of silently producing an incomplete cohort.
    """

    if not isinstance(source, (DoctorOpdPatientSource, MrnPatientSource)):
        raise ConfigurationError("scan-soap received an unsupported patient source")
    if not isinstance(search, SoapSearch):
        raise ConfigurationError("scan-soap requires a SoapSearch")
    if not isinstance(visit_filter, VisitFilter):
        raise ConfigurationError("scan-soap requires a VisitFilter")
    if max_patients is not None and (
        isinstance(max_patients, bool) or not isinstance(max_patients, int) or max_patients < 1
    ):
        raise ConfigurationError("max_patients must be positive")

    # Compilation is deliberately first: regex validation must precede source
    # requests even when this function is called directly instead of via CLI.
    matcher = search.compile()

    matches_path = output_dir / "soap_scan_matches.jsonl"
    index_path = output_dir / "soap_scan_index.csv"
    status_path = output_dir / "soap_scan_status.jsonl"
    manifest_path = output_dir / "soap_scan_manifest.json"
    check_output_targets(
        (matches_path, index_path, status_path, manifest_path), overwrite=overwrite
    )

    candidates = _resolve_candidates(sdk, source)
    if max_patients is not None:
        candidates = candidates[:max_patients]

    output_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    match_count = 0
    error_count = 0

    with (
        matches_path.open("w", encoding="utf-8", newline="\n") as matches_handle,
        status_path.open("w", encoding="utf-8", newline="\n") as status_handle,
    ):
        for candidate in candidates:
            status, matches = _scan_patient(
                sdk,
                candidate=candidate,
                source=source,
                search=search,
                matcher=matcher,
                visit_filter=visit_filter,
                all_matches=all_matches,
            )
            for match in matches:
                write_jsonl_line(matches_handle, match)
                index_rows.append(_index_row(match))
            write_jsonl_line(status_handle, status)
            match_count += len(matches)
            status_counts[str(status["status"])] += 1
            if status["status"] in {"ERROR", "INCOMPLETE", "MATCHED_WITH_ERRORS"}:
                error_count += 1

    _write_index(index_path, index_rows)
    write_json_atomic(
        manifest_path,
        _manifest(
            source=source,
            search=search,
            visit_filter=visit_filter,
            all_matches=all_matches,
            max_patients=max_patients,
            patient_count=len(candidates),
            match_count=match_count,
            error_count=error_count,
            status_counts=status_counts,
        ),
    )
    return SoapScanResult(
        patient_count=len(candidates),
        match_count=match_count,
        error_count=error_count,
        matches_path=matches_path,
        index_path=index_path,
        status_path=status_path,
        manifest_path=manifest_path,
    )


def _resolve_candidates(
    sdk: SDKProtocol,
    source: DoctorOpdPatientSource | MrnPatientSource,
) -> list[_PatientCandidate]:
    if isinstance(source, MrnPatientSource):
        return [_PatientCandidate(mrn=mrn) for mrn in source.mrns]

    candidates: OrderedDict[str, _PatientCandidate] = OrderedDict()
    for query_date in date_range(source.start, source.end):
        # Do not catch non-authentication failures here.  Any incomplete date
        # makes the entire doctor-derived patient population unreliable.
        patients = sdk.opd.get_doctor_patients(source.card_no, query_date)
        for patient in patients:
            mrn = MrnPatientSource((patient.mrn,)).mrns[0]
            candidate = candidates.get(mrn)
            if candidate is None:
                candidate = _PatientCandidate(
                    mrn=mrn,
                    fallback_name=patient.name,
                    source_opd_dates=set(),
                )
                candidates[mrn] = candidate
            elif not candidate.fallback_name and patient.name:
                candidate.fallback_name = patient.name
            if candidate.source_opd_dates is None:
                candidate.source_opd_dates = set()
            candidate.source_opd_dates.add(query_date)
    return list(candidates.values())


def _scan_patient(
    sdk: SDKProtocol,
    *,
    candidate: _PatientCandidate,
    source: DoctorOpdPatientSource | MrnPatientSource,
    search: SoapSearch,
    matcher: Pattern[str],
    visit_filter: VisitFilter,
    all_matches: bool,
) -> tuple[dict[str, Any], list[SoapScanMatch]]:
    source_dates = candidate.sorted_source_dates()
    status: dict[str, Any] = {
        "schema_version": SOAP_SCAN_SCHEMA_VERSION,
        "mrn": candidate.mrn,
        "source_type": _source_type(source),
        "source_opd_dates": [item.isoformat() for item in source_dates],
        "status": "",
        "matching_case_count": 0,
        "soap_requested_count": 0,
        "soap_searched_count": 0,
        "soap_missing_count": 0,
        "soap_error_count": 0,
        "match_count": 0,
        "matches": [],
        "case_errors": [],
        "issues": [],
    }
    try:
        cases = visit_filter.select(sdk.records.find_visit_cases(candidate.mrn, visit_filter))
    except AuthenticationError:
        raise
    except Exception as exc:
        issue = issue_from_exception(exc)
        status["status"] = "ERROR"
        status["issues"] = [to_jsonable(issue)]
        return status, []

    status["matching_case_count"] = len(cases)
    if not cases:
        status["status"] = "NO_MATCHING_VISIT"
        return status, []

    drafts: list[_MatchDraft] = []
    issues: list[ErrorInfo] = []
    for case in cases:
        status["soap_requested_count"] += 1
        try:
            soap = sdk.records.get_soap(case)
        except AuthenticationError:
            raise
        except Exception as exc:
            issue = issue_from_exception(exc, prefix="SOAP")
            issues.append(issue)
            status["soap_error_count"] += 1
            status["case_errors"].append(_case_error(case, issue))
            continue

        try:
            evaluation = evaluate_soap_search(soap, search, compiled=matcher)
            if evaluation.status == "MISSING":
                issue = issue_from_code("SOAP_MISSING")
                issues.append(issue)
                status["soap_missing_count"] += 1
                status["case_errors"].append(_case_error(case, issue))
                continue
            if evaluation.status == "ERROR":
                issue = issue_from_code("SOAP_TEXT_TOO_LARGE")
                issues.append(issue)
                status["soap_error_count"] += 1
                status["case_errors"].append(_case_error(case, issue))
                continue

            status["soap_searched_count"] += 1
            if evaluation.status == "NO_MATCH":
                continue
            if evaluation.evidence is None:
                raise ConfigurationError("SOAP search returned no match evidence")
            evidence = evaluation.evidence
        except AuthenticationError:
            raise
        except Exception as exc:
            issue = issue_from_exception(exc, prefix="SOAP")
            issues.append(issue)
            status["soap_error_count"] += 1
            status["case_errors"].append(_case_error(case, issue))
            continue
        drafts.append(_MatchDraft(case=case, evidence=evidence))
        if not all_matches:
            break

    name = candidate.fallback_name
    telephone = ""
    if drafts:
        try:
            demographics = sdk.patients.get_demographics(candidate.mrn)
            name = demographics.name or name
            telephone = demographics.preferred_phone
        except AuthenticationError:
            raise
        except Exception as exc:
            issues.append(issue_from_exception(exc, prefix="DEMOGRAPHICS"))

    matches = [
        SoapScanMatch(
            schema_version=SOAP_SCAN_SCHEMA_VERSION,
            mrn=candidate.mrn,
            name=name,
            telephone=telephone,
            source_type=_source_type(source),
            source_opd_dates=source_dates,
            case=draft.case,
            search=search,
            evidence=draft.evidence,
        )
        for draft in drafts
    ]
    status["match_count"] = len(matches)
    status["matches"] = [
        {
            "visit_date": match.case.visit_date.isoformat()
            if match.case.visit_date is not None
            else "",
            "case_no": match.case.case_no,
            "section_code": match.case.section_code,
            "section_name": match.case.section_name,
        }
        for match in matches
    ]
    status["issues"] = [to_jsonable(item) for item in _unique_issues(issues)]
    if matches:
        status["status"] = "MATCHED_WITH_ERRORS" if issues else "MATCHED"
    else:
        status["status"] = "INCOMPLETE" if issues else "NO_MATCH"
    return status, matches


def _case_error(case: VisitCase, issue: ErrorInfo) -> dict[str, Any]:
    return {
        "visit_date": case.visit_date.isoformat() if case.visit_date else "",
        "case_no": case.case_no,
        "issue": to_jsonable(issue),
    }


def _source_type(source: DoctorOpdPatientSource | MrnPatientSource) -> str:
    return "DOCTOR_OPD" if isinstance(source, DoctorOpdPatientSource) else "MRN"


def _unique_issues(values: Iterable[ErrorInfo]) -> list[ErrorInfo]:
    return list({item.code: item for item in values}.values())


def _index_row(match: SoapScanMatch) -> dict[str, Any]:
    return {
        "病歷號": match.mrn,
        "姓名": match.name,
        "電話": match.telephone,
        "病人來源": match.source_type,
        "來源門診日期": ";".join(item.isoformat() for item in match.source_opd_dates),
        "命中就診日期": match.case.visit_date.isoformat()
        if match.case.visit_date is not None
        else "",
        "case編號": match.case.case_no,
        "科別代碼": match.case.section_code,
        "科別": match.case.section_name,
        "搜尋模式": match.search.mode,
        "忽略大小寫": str(match.search.ignore_case).lower(),
        "全文起點": match.evidence.start,
        "全文終點": match.evidence.end,
        "命中block索引": ";".join(str(value) for value in match.evidence.block_indices),
        "證據範圍": match.evidence.evidence_scope,
    }


def _write_index(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    fields = [
        "病歷號",
        "姓名",
        "電話",
        "病人來源",
        "來源門診日期",
        "命中就診日期",
        "case編號",
        "科別代碼",
        "科別",
        "搜尋模式",
        "忽略大小寫",
        "全文起點",
        "全文終點",
        "命中block索引",
        "證據範圍",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _manifest(
    *,
    source: DoctorOpdPatientSource | MrnPatientSource,
    search: SoapSearch,
    visit_filter: VisitFilter,
    all_matches: bool,
    max_patients: int | None,
    patient_count: int,
    match_count: int,
    error_count: int,
    status_counts: Counter[str],
) -> dict[str, Any]:
    if isinstance(source, DoctorOpdPatientSource):
        source_config: dict[str, Any] = {
            "type": "DOCTOR_OPD",
            "doctor_card": source.card_no,
            "start": source.start.isoformat(),
            "end": source.end.isoformat(),
        }
    else:
        source_config = {"type": "MRN", "input_count": len(source.mrns)}
    matched_patient_count = status_counts["MATCHED"] + status_counts["MATCHED_WITH_ERRORS"]
    return {
        "schema_version": SOAP_SCAN_SCHEMA_VERSION,
        "status": "COMPLETED_WITH_ERRORS" if error_count else "OK",
        "warning": (
            "Outputs contain unredacted clinical SOAP evidence and must be handled "
            "under the hospital medical-record data policy."
        ),
        "source": source_config,
        "search": to_jsonable(search),
        "visit_filter": to_jsonable(visit_filter),
        "match_strategy": "ALL_MATCHING_CASES" if all_matches else "FIRST_MATCHING_CASE",
        "max_patients": max_patients,
        "statistics": {
            "patient_count": patient_count,
            "matched_patient_count": matched_patient_count,
            "match_count": match_count,
            "error_count": error_count,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "outputs": {
            "matches": "soap_scan_matches.jsonl",
            "index": "soap_scan_index.csv",
            "status": "soap_scan_status.jsonl",
            "manifest": "soap_scan_manifest.json",
        },
    }
