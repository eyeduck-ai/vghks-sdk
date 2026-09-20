"""Collect completed surgery cases and notes with independent checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError, error_info
from ..local_io import write_json_atomic
from ..models import SurgeryCase, SurgeryCaseFilter, SurgeryCaseRef, to_jsonable
from ..services.protocols import SDKProtocol


@dataclass(frozen=True, slots=True)
class SurgeryCollectionResult:
    status: str
    cases: tuple[SurgeryCase, ...]
    records: tuple[dict[str, Any], ...]
    manifest_path: Path


def collect_surgery_records(
    sdk: SDKProtocol,
    filter: SurgeryCaseFilter,
    *,
    output_dir: Path,
    periods: tuple[str, ...] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    download: bool = True,
) -> SurgeryCollectionResult:
    """Collect every returned case, with no implicit record/sample limit.

    Default uses the filter's one server period. Pass ("24M", "2YB") for all
    recorded history partitions. Exact dates are inclusive local filters.
    An incomplete partition or note never becomes an empty successful result.
    """
    if not isinstance(filter, SurgeryCaseFilter):
        raise ConfigurationError("surgery collection requires SurgeryCaseFilter")
    if periods is not None and (not isinstance(periods, (tuple, list)) or not periods):
        raise ConfigurationError("surgery periods must be a nonempty array")
    selectors = tuple(replace(filter, period=p) for p in dict.fromkeys(periods or (filter.period,)))
    if any(d is not None and type(d) is not date for d in (start_date, end_date)):
        raise ConfigurationError("surgery dates must be date objects")
    if start_date and end_date and start_date > end_date:
        raise ConfigurationError("surgery date range is reversed")
    root = Path(output_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise ConfigurationError("surgery output directory must be empty", code="OUTPUT_NOT_EMPTY")
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "filters": to_jsonable(selectors),
        "start_date": to_jsonable(start_date),
        "end_date": to_jsonable(end_date),
        "download": download,
        "partitions": [],
        "records": [],
        "conflicts": [],
        "errors": 0,
        "case_count": 0,
    }
    write_json_atomic(manifest_path, manifest)
    by_ref: dict[SurgeryCaseRef, SurgeryCase] = {}
    for index, selector in enumerate(selectors, 1):
        destination = root / "stage1_cases" / f"{index:02d}-{selector.period}.json"
        entry: dict[str, Any] = {
            "input": to_jsonable(selector),
            "output": destination.relative_to(root).as_posix(),
        }
        try:
            rows = sdk.surgery.get_cases(selector)
        except Exception as exc:
            entry.update(status="ERROR", issue=to_jsonable(error_info(exc)))
            manifest["errors"] += 1
            write_json_atomic(destination, entry)
        else:
            entry.update(status="OK" if rows else "EMPTY", count=len(rows))
            write_json_atomic(destination, {**entry, "returned": to_jsonable(rows)})
            for row in rows:
                if row.reference in by_ref and by_ref[row.reference] != row:
                    manifest["conflicts"].append(to_jsonable(row.reference))
                by_ref.setdefault(row.reference, row)
        manifest["partitions"].append(entry)
        write_json_atomic(manifest_path, manifest)
    cases = tuple(
        row
        for row in by_ref.values()
        if (start_date is None or row.surgery_date >= start_date)
        and (end_date is None or row.surgery_date <= end_date)
    )
    manifest["case_count"] = len(cases)
    write_json_atomic(root / "cases.json", to_jsonable(cases))
    for index, case in enumerate(cases, 1):
        key = f"case-{index:05d}"
        entry = {"reference": to_jsonable(case.reference), "status": "RUNNING"}
        note_path = root / "stage2_notes" / f"{key}.json"
        try:
            note = sdk.surgery.get_record_ref(case.reference)
        except Exception as exc:
            entry.update(status="ERROR", stage="note", issue=to_jsonable(error_info(exc)))
            manifest["errors"] += 1
            write_json_atomic(note_path, entry)
        else:
            entry.update(
                status="AVAILABLE" if note is not None else "NOT_FOUND", note=to_jsonable(note)
            )
            write_json_atomic(note_path, entry)
            if download and note is not None:
                asset_path = root / "stage3_pdf" / f"{key}.pdf"
                try:
                    asset = sdk.surgery.download_record(note)
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    asset_path.write_bytes(asset.content)
                except Exception as exc:
                    entry.update(status="ERROR", stage="pdf", issue=to_jsonable(error_info(exc)))
                    manifest["errors"] += 1
                else:
                    entry.update(
                        status="DOWNLOADED",
                        pdf=asset_path.relative_to(root).as_posix(),
                        byte_count=asset.size,
                    )
                write_json_atomic(asset_path.with_suffix(".json"), entry)
        manifest["records"].append(entry)
        write_json_atomic(manifest_path, manifest)
    manifest["status"] = "INCOMPLETE" if manifest["errors"] or manifest["conflicts"] else "OK"
    manifest["downloaded_count"] = sum(r["status"] == "DOWNLOADED" for r in manifest["records"])
    manifest["not_found_count"] = sum(r["status"] == "NOT_FOUND" for r in manifest["records"])
    write_json_atomic(manifest_path, manifest)
    return SurgeryCollectionResult(
        manifest["status"], cases, tuple(manifest["records"]), manifest_path
    )
