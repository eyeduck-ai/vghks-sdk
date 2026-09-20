"""Export the newest outpatient case selected by a visit filter."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import AuthenticationError
from ..local_io import read_mrns, write_jsonl_line
from ..models import LatestRecordBundle, VisitFilter
from ..services.protocols import SDKProtocol
from .common import (
    check_output_targets,
    fetch_case_parts,
    issue_from_exception,
)

LATEST_RECORDS_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class LatestRecordsResult:
    record_count: int
    ok_count: int
    error_count: int
    records_path: Path
    index_path: Path


def export_latest_records(
    sdk: SDKProtocol,
    *,
    input_path: Path,
    visit_filter: VisitFilter,
    output_dir: Path,
    max_records: int | None = None,
    overwrite: bool = False,
) -> LatestRecordsResult:
    if max_records is not None and max_records < 1:
        raise ValueError("max_records must be positive")
    records_path = output_dir / "latest_records.jsonl"
    index_path = output_dir / "latest_records_index.csv"
    check_output_targets((records_path, index_path), overwrite=overwrite)
    mrns = read_mrns(input_path)
    if max_records is not None:
        mrns = mrns[:max_records]

    output_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    ok_count = 0
    error_count = 0

    with records_path.open("w", encoding="utf-8", newline="\n") as records_handle:
        for mrn in mrns:
            try:
                matching_cases = sdk.records.find_visit_cases(mrn, visit_filter)
            except AuthenticationError:
                raise
            except Exception as exc:
                error_count += 1
                bundle = LatestRecordBundle(
                    LATEST_RECORDS_SCHEMA_VERSION,
                    mrn,
                    "ERROR",
                    None,
                    None,
                    None,
                    (issue_from_exception(exc),),
                )
                write_jsonl_line(records_handle, bundle)
                index_rows.append(_latest_index_row(bundle))
                continue

            if not matching_cases:
                bundle = LatestRecordBundle(
                    LATEST_RECORDS_SCHEMA_VERSION,
                    mrn,
                    "NO_MATCHING_VISIT",
                    None,
                    None,
                    None,
                )
                write_jsonl_line(records_handle, bundle)
                index_rows.append(_latest_index_row(bundle))
                continue

            latest = matching_cases[0]
            parts = fetch_case_parts(sdk.records, latest)
            if "ERROR" in {parts.soap_status, parts.numeric_status}:
                error_count += 1
            if parts.status == "OK":
                ok_count += 1
            bundle = LatestRecordBundle(
                schema_version=LATEST_RECORDS_SCHEMA_VERSION,
                mrn=mrn,
                status=parts.status,
                case=latest,
                soap=parts.soap,
                numeric_report=parts.numeric,
                issues=parts.issues,
            )
            write_jsonl_line(records_handle, bundle)
            index_rows.append(_latest_index_row(bundle))

    _write_latest_index(index_path, index_rows)
    return LatestRecordsResult(
        record_count=len(mrns),
        ok_count=ok_count,
        error_count=error_count,
        records_path=records_path,
        index_path=index_path,
    )


def _latest_index_row(bundle: LatestRecordBundle) -> dict[str, Any]:
    return {
        "病歷號": bundle.mrn,
        "最新就診日期": bundle.case.visit_date.isoformat()
        if bundle.case is not None and bundle.case.visit_date is not None
        else "",
        "SOAP狀態": "PRESENT" if bundle.soap is not None and bundle.soap.blocks else "MISSING",
        "數值表數量": len(bundle.numeric_report.tables) if bundle.numeric_report is not None else 0,
        "狀態": bundle.status,
        "錯誤碼": bundle.error_code,
    }


def _write_latest_index(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "病歷號",
                "最新就診日期",
                "SOAP狀態",
                "數值表數量",
                "狀態",
                "錯誤碼",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
