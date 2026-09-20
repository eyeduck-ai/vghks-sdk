"""Offline command support for semantic HAR contract checking."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ..core.errors import ConfigurationError
from .har import HarCheckReport, evaluate_har_contracts, load_har

DEFAULT_HAR_REPORT_PATH = Path("output/har-check/har_check_report.json")


def run_har_check(
    *, input_path: Path | None = None, output_path: Path = DEFAULT_HAR_REPORT_PATH
) -> tuple[HarCheckReport, Path]:
    files, require_baseline = discover_har_files(input_path)
    archives = tuple(load_har(path) for path in files)
    report = evaluate_har_contracts(archives, require_baseline=require_baseline)
    destination = write_har_check_report(output_path, report)
    return report, destination


def discover_har_files(input_path: Path | None) -> tuple[tuple[Path, ...], bool]:
    source = (input_path or Path.cwd()).expanduser()
    if source.is_dir() and (source / "data" / "har").is_dir():
        source = source / "data" / "har"
    if source.is_file():
        if source.suffix.casefold() != ".har":
            raise ConfigurationError("har-check input file must use the .har extension")
        return (source,), False
    if not source.exists():
        raise ConfigurationError("har-check input does not exist")
    if not source.is_dir():
        raise ConfigurationError("har-check input must be a HAR file or directory")
    try:
        top_level = tuple(
            path for path in source.iterdir() if path.is_file() and path.suffix.casefold() == ".har"
        )
        files = tuple(
            sorted(
                top_level,
                key=lambda path: path.name.casefold(),
            )
        )
    except OSError as exc:
        raise ConfigurationError("unable to scan HAR input directory") from exc
    if not files:
        raise ConfigurationError("har-check input directory does not contain HAR files")
    return files, True


def write_har_check_report(path: Path, report: HarCheckReport) -> Path:
    """Atomically replace a report containing only the documented safe fields."""

    destination = path.expanduser().resolve()
    payload = {
        "schema_version": report.schema_version,
        "status": report.status,
        "archive_count": report.archive_count,
        "entry_count": report.entry_count,
        "contracts": [
            {
                "contract": item.contract,
                "matched_count": item.matched_count,
                "passed_count": item.passed_count,
                "status": item.status,
                "error_codes": list(item.error_codes),
            }
            for item in report.contracts
        ],
    }
    temporary_name = ""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except OSError as exc:
        raise ConfigurationError("unable to write the har-check JSON report") from exc
    finally:
        if temporary_name:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
    return destination
