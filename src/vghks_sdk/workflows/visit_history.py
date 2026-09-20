"""Export every outpatient case selected by a cross-specialty visit filter."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import AuthenticationError, ConfigurationError
from ..identifiers import normalize_mrns
from ..local_io import write_jsonl_line
from ..models import (
    VisitCase,
    VisitFilter,
    VisitHistoryRecord,
)
from ..services.protocols import SDKProtocol
from .common import check_output_targets, fetch_case_parts, issue_from_exception

VISIT_HISTORY_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class VisitHistoryResult:
    patient_count: int
    case_count: int
    ok_count: int
    error_count: int
    records_path: Path
    index_path: Path


def export_visit_history(
    sdk: SDKProtocol,
    *,
    mrns: Sequence[str],
    visit_filter: VisitFilter,
    output_dir: Path,
    overwrite: bool = False,
    preloaded_cases: Mapping[str, Sequence[VisitCase]] | None = None,
) -> VisitHistoryResult:
    normalized_mrns = normalize_mrns(mrns)
    if not normalized_mrns:
        raise ConfigurationError("visit-history input did not contain any records")

    records_path = output_dir / "visit_history_records.jsonl"
    index_path = output_dir / "visit_history_index.csv"
    check_output_targets((records_path, index_path), overwrite=overwrite)
    output_dir.mkdir(parents=True, exist_ok=True)

    index_rows: list[dict[str, Any]] = []
    case_count = 0
    ok_count = 0
    error_count = 0

    with records_path.open("w", encoding="utf-8", newline="\n") as records_handle:
        for mrn in normalized_mrns:
            try:
                if preloaded_cases is not None and mrn in preloaded_cases:
                    cases = visit_filter.select(preloaded_cases[mrn])
                else:
                    cases = sdk.records.find_visit_cases(mrn, visit_filter)
            except AuthenticationError:
                raise
            except Exception as exc:
                error_count += 1
                record = VisitHistoryRecord(
                    schema_version=VISIT_HISTORY_SCHEMA_VERSION,
                    mrn=mrn,
                    status="ERROR",
                    case=None,
                    soap_status="ERROR",
                    numeric_status="ERROR",
                    soap=None,
                    numeric_report=None,
                    issues=(issue_from_exception(exc),),
                )
                _write_record(records_handle, index_rows, record)
                continue

            if not cases:
                record = VisitHistoryRecord(
                    schema_version=VISIT_HISTORY_SCHEMA_VERSION,
                    mrn=mrn,
                    status="NO_MATCHING_VISIT",
                    case=None,
                    soap_status="NO_MATCHING_VISIT",
                    numeric_status="NO_MATCHING_VISIT",
                    soap=None,
                    numeric_report=None,
                )
                _write_record(records_handle, index_rows, record)
                continue

            for case in cases:
                case_count += 1
                parts = fetch_case_parts(sdk.records, case)
                if parts.status == "OK":
                    ok_count += 1
                if "ERROR" in {parts.soap_status, parts.numeric_status}:
                    error_count += 1
                record = VisitHistoryRecord(
                    schema_version=VISIT_HISTORY_SCHEMA_VERSION,
                    mrn=mrn,
                    status=parts.status,
                    case=case,
                    soap_status=parts.soap_status,
                    numeric_status=parts.numeric_status,
                    soap=parts.soap,
                    numeric_report=parts.numeric,
                    issues=parts.issues,
                )
                _write_record(records_handle, index_rows, record)

    _write_index(index_path, index_rows)
    return VisitHistoryResult(
        patient_count=len(normalized_mrns),
        case_count=case_count,
        ok_count=ok_count,
        error_count=error_count,
        records_path=records_path,
        index_path=index_path,
    )


def _write_record(
    handle: Any,
    index_rows: list[dict[str, Any]],
    record: VisitHistoryRecord,
) -> None:
    write_jsonl_line(handle, record)
    index_rows.append(_index_row(record))


def _index_row(record: VisitHistoryRecord) -> dict[str, Any]:
    case = record.case
    return {
        "病歷號": record.mrn,
        "就診日期": case.visit_date.isoformat()
        if case is not None and case.visit_date is not None
        else "",
        "case編號": case.case_no if case is not None else "",
        "科別代碼": case.section_code if case is not None else "",
        "科別": case.section_name if case is not None else "",
        "SOAP狀態": record.soap_status,
        "SOAP區塊數": len(record.soap.blocks) if record.soap is not None else 0,
        "數值狀態": record.numeric_status,
        "數值表數量": len(record.numeric_report.tables) if record.numeric_report is not None else 0,
        "狀態": record.status,
        "錯誤碼": record.error_code,
    }


def _write_index(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "病歷號",
                "就診日期",
                "case編號",
                "科別代碼",
                "科別",
                "SOAP狀態",
                "SOAP區塊數",
                "數值狀態",
                "數值表數量",
                "狀態",
                "錯誤碼",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
