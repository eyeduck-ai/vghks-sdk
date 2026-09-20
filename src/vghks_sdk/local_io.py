"""Local PHI-bearing input and output helpers."""

from __future__ import annotations

import contextlib
import csv
import json
import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from .core.errors import ConfigurationError
from .identifiers import normalize_mrn
from .models import AuthCheckReport, to_jsonable


def read_mrns(path: Path) -> list[str]:
    try:
        if not path.is_file():
            raise ConfigurationError(
                "medical record number input file does not exist",
                code="INPUT_FILE_NOT_FOUND",
                operation="local.read_mrns",
                app="local",
            )
        values = _read_mrns_csv(path) if path.suffix.lower() == ".csv" else _read_mrns_text(path)
    except ConfigurationError:
        raise
    except UnicodeError as exc:
        raise ConfigurationError(
            "medical record number input is not valid UTF-8",
            code="INPUT_ENCODING_ERROR",
            operation="local.read_mrns",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    except csv.Error as exc:
        raise ConfigurationError(
            "medical record number CSV could not be parsed",
            code="INPUT_FORMAT_ERROR",
            operation="local.read_mrns",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            "unable to read medical record number input",
            code="INPUT_READ_FAILED",
            operation="local.read_mrns",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    output: list[str] = []
    seen: set[str] = set()
    for line_number, raw in values:
        mrn = unicodedata.normalize("NFKC", raw).strip()
        if not mrn:
            continue
        if mrn.lower() in {"mrn", "病歷號"}:
            continue
        mrn = normalize_mrn(mrn, location=f"row {line_number}")
        if mrn not in seen:
            seen.add(mrn)
            output.append(mrn)
    return output


def write_jsonl_line(handle: Any, value: Any) -> None:
    try:
        handle.write(json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ConfigurationError(
            "unable to write JSONL output",
            code="OUTPUT_WRITE_FAILED",
            operation="local.write_jsonl",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc


def write_json_atomic(path: Path, value: Any) -> Path:
    """Atomically replace a UTF-8 JSON document."""

    destination = path.expanduser().resolve()
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
            json.dump(
                to_jsonable(value),
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ConfigurationError(
            "unable to write JSON output",
            code="OUTPUT_WRITE_FAILED",
            operation="local.write_json",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    finally:
        if temporary_name:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
    return destination


def write_bytes_atomic(path: Path, content: bytes) -> Path:
    """Atomically replace one binary asset without deriving its filename from PHI."""

    destination = path.expanduser().resolve()
    temporary_name = ""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except OSError as exc:
        raise ConfigurationError(
            "unable to write binary asset",
            code="OUTPUT_WRITE_FAILED",
            operation="local.write_binary",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    finally:
        if temporary_name:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
    return destination


def write_auth_check_report(path: Path, report: AuthCheckReport) -> Path:
    """Atomically replace the well-known, deliberately safe auth report."""

    destination = path.expanduser().resolve()
    safe_payload = {
        "schema_version": report.schema_version,
        "sdk_version": report.sdk_version,
        "generated_at": report.generated_at,
        "status": report.status,
        "reauthenticated": report.reauthenticated,
        "targets": [
            {
                "target": item.target,
                "capability": list(item.capability),
                "landing_path": item.landing_path,
                "hid_present": item.hid_present,
                "cookie_count": item.cookie_count,
                "duration_ms": item.duration_ms,
                "dependencies": list(item.dependencies),
                "status": item.status,
                "issue": to_jsonable(item.issue),
            }
            for item in report.targets
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
            json.dump(
                safe_payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ConfigurationError(
            "unable to write the auth-check JSON report",
            code="AUTH_REPORT_WRITE_FAILED",
            operation="local.write_auth_report",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    finally:
        if temporary_name:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
    return destination


def _read_mrns_csv(path: Path) -> list[tuple[int, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ConfigurationError("medical record number CSV does not have a header")
        mapping = {
            unicodedata.normalize("NFKC", name).strip().lower(): name for name in reader.fieldnames
        }
        selected = mapping.get("病歷號") or mapping.get("mrn")
        if not selected:
            raise ConfigurationError("medical record number CSV requires 病歷號 or mrn column")
        return [
            (row_number, str(row.get(selected, "")))
            for row_number, row in enumerate(reader, start=2)
        ]


def _read_mrns_text(path: Path) -> list[tuple[int, str]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return [(number, line.strip()) for number, line in enumerate(handle, start=1)]
