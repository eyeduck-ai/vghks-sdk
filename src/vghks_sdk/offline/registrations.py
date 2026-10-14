"""Audit registration batches against saved results and captured pages, without HTTP."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..contracts.har import HarEntry
from ..core.errors import ConfigurationError, ParseError
from ..identifiers import normalize_mrns
from ..models import RegistrationRecord, to_jsonable
from ..parsing.webmaas import (
    find_next_displaytag_href,
    parse_registration_records,
    registration_counts,
    registration_identity,
)
from .bundle import BundleReader
from .replay import request_parts

_ROOT = "parsed/registrations/"
_PATH = "/webmaas/RSV/RSV11W001.do"
_KEY = "webmaas.registration_query"
_STATUSES = ("OK", "EMPTY", "ERROR", "BLOCKED")


def _request_identity(request: dict) -> tuple:
    address = urlsplit(str(request.get("url", "")))
    _, _, query, _ = request_parts(request)
    return address.scheme, address.netloc, address.path, tuple(sorted(query.items()))


def _page_request(row: dict) -> bool:
    method, path, query, form = request_parts(row.get("request") or {})
    return path == _PATH and (
        (method == "POST" and form.get("QRY") == "QRY")
        or (method == "GET" and any(re.fullmatch(r"d-\w+-p", key) for key in query))
    )


def _replay_patient(reader: BundleReader, captures: list[dict], step: dict, mrn: str) -> tuple[list, int]:
    first, last = step.get("first_capture_id"), step.get("last_capture_id")
    if not all(isinstance(value, str) and value.isascii() and value.isdigit() for value in (first, last)):
        raise ParseError("capture range missing", code="REGISTRATION_CAPTURE_MISSING")
    pages = [row for row in captures if row.get("kind") == "HTTP_EXCHANGE"
             and isinstance(row.get("capture_id"), str) and row["capture_id"].isascii()
             and row["capture_id"].isdigit()
             and int(first) <= int(row["capture_id"]) <= int(last) and _page_request(row)]
    # If Runtime rebuilt a read operation, only its final submitted query and
    # subsequent pages form the saved result. Earlier failures stay in replay.
    starts = [i for i, row in enumerate(pages) if request_parts(row["request"])[0] == "POST"]
    if not starts:
        raise ParseError("query capture missing", code="REGISTRATION_CAPTURE_MISSING")
    pages = pages[starts[-1]:]
    records: list[RegistrationRecord] = []
    seen = set()
    expected_next = None
    for position, row in enumerate(pages):
        request = row["request"]
        _, _, query, form = request_parts(request)
        if (form.get("patno") if position == 0 else query.get("patno", mrn)) != mrn:
            raise ParseError("capture patient differs", code="REGISTRATION_CAPTURE_PATIENT_MISMATCH")
        if position and (expected_next is None or _request_identity(request) != expected_next):
            raise ParseError("page capture differs", code="REGISTRATION_PAGINATION_INCOMPLETE")
        response = row.get("response") or {}
        if response.get("status_code") != 200 or not row.get("response_file"):
            raise ParseError("query response missing", code="REGISTRATION_CAPTURE_MISSING")
        mime = next((str(value) for key, value in response.get("headers", [])
                     if str(key).lower() == "content-type"), "")
        text = HarEntry("", "", frozenset(), frozenset(), {}, 200,
                        reader.read(row["response_file"]), mime).text()
        for record in parse_registration_records(text, mrn):
            identity = registration_identity(record)
            if identity not in seen:
                seen.add(identity)
                records.append(record)
        href = find_next_displaytag_href(text)
        expected_next = _request_identity({"url": urljoin(str(request.get("url", "")), href)}) if href else None
    if expected_next is not None:
        raise ParseError("last page missing", code="REGISTRATION_PAGINATION_INCOMPLETE")
    return records, len(pages)


def registration_batch_summary(reader: BundleReader, steps: list[dict]) -> dict[str, Any]:
    """Return only counts and fixed issue codes; retain original live status."""
    run = reader.json("run_summary.json")
    if run.get("profile") != "registrations" and _ROOT + "batch_summary.json" not in reader.names:
        return {"status": "NOT_RUN", "issues": []}
    issues = []

    def issue(code: str) -> None:
        value = {"operation": _KEY, "code": code}
        if value not in issues:
            issues.append(value)

    def read(name: str, kind: type, code: str):
        try:
            value = reader.json(name)
        except ConfigurationError:
            value = None
        if not isinstance(value, kind):
            issue(code)
            return kind()
        return value

    config = read("run_config.json", dict, "REGISTRATION_INPUTS_INVALID")
    values = config.get("patient_mrns")
    try:
        if not isinstance(values, list):
            raise ValueError
        mrns = normalize_mrns(values)
        if mrns != values:
            raise ValueError
    except (ConfigurationError, ValueError):
        issue("REGISTRATION_INPUTS_INVALID")
        mrns = []
    recorded = read(_ROOT + "batch_summary.json", dict, "REGISTRATION_SUMMARY_INVALID")
    if recorded.get("schema_version") != 1:
        issue("REGISTRATION_SUMMARY_INVALID")
    index = read(_ROOT + "patients.json", list, "REGISTRATION_INDEX_INVALID")
    expected_index = [{"index": i, "mrn": mrn, "output": f"patients/{i:06d}.json"}
                      for i, mrn in enumerate(mrns, 1)]
    if index != expected_index:
        issue("REGISTRATION_INDEX_MISMATCH")
    try:
        journal = reader.jsonl(_ROOT + "patient_results.jsonl")
    except ConfigurationError:
        issue("REGISTRATION_JOURNAL_INVALID")
        journal = []
    patient_steps = [step for step in steps if re.fullmatch(r"registrations\.patient_\d{6}", str(step.get("name", "")))]
    by_name = {step["name"]: step for step in patient_steps}
    if len(by_name) != len(patient_steps) or len(journal) > len(mrns):
        issue("REGISTRATION_JOURNAL_MISMATCH")
    captures = reader.jsonl("capture_manifest.jsonl")
    statuses: Counter = Counter()
    totals: Counter = Counter()
    codes: Counter = Counter()
    expected_files = set()
    for i, mrn in enumerate(mrns, 1):
        name = f"registrations.patient_{i:06d}"
        step = by_name.pop(name, None)
        entry = journal[i - 1] if i <= len(journal) else None
        if step is None:
            if entry is not None:
                issue("REGISTRATION_JOURNAL_MISMATCH")
            continue
        if not entry or entry.get("index") != i or entry.get("mrn") != mrn or entry.get("step") != step:
            issue("REGISTRATION_JOURNAL_MISMATCH")
        status = step.get("status")
        if status not in _STATUSES or step.get("operation") != _KEY:
            issue("REGISTRATION_STEP_INVALID")
            continue
        statuses[status] += 1
        path = _ROOT + f"patients/{i:06d}.json"
        if status not in {"OK", "EMPTY"}:
            continue
        expected_files.add(path)
        if step.get("output") != path:
            issue("REGISTRATION_RESULT_PATH_MISMATCH")
        values = read(path, list, "REGISTRATION_RESULT_INVALID" if path in reader.names else "REGISTRATION_RESULT_MISSING")
        saved = []
        for value in values:
            try:
                if not isinstance(value, dict) or "no_show" not in value or not (
                    value["no_show"] is True or value["no_show"] is None
                ) or not isinstance(value.get("registered_by"), str) or not isinstance(value.get("columns"), dict):
                    raise ValueError
                if not all(isinstance(k, str) and isinstance(v, str) for k, v in value["columns"].items()):
                    raise ValueError
                record = RegistrationRecord(**value)
            except (TypeError, ValueError):
                issue("REGISTRATION_RESULT_INVALID")
                continue
            if record.mrn != mrn:
                issue("REGISTRATION_RESULT_PATIENT_MISMATCH")
            saved.append(record)
        counts = registration_counts(saved)
        totals.update({key: counts[key] for key in ("record_count", "no_show_count", "attendance_unknown_count")})
        codes.update(counts["registered_by_codes"])
        details = step.get("details") or {}
        if status != ("OK" if saved else "EMPTY") or any(
            type(details.get(key)) is not int or details[key] != counts[key]
            for key in ("record_count", "no_show_count", "attendance_unknown_count")
        ):
            issue("REGISTRATION_STEP_MISMATCH")
        try:
            replayed, page_count = _replay_patient(reader, captures, step, mrn)
            totals["raw_response_count"] += page_count
            if to_jsonable(replayed) != values:
                issue("REGISTRATION_REPLAY_MISMATCH")
            elif path in reader.names:
                totals["verified_patient_count"] += 1
        except ParseError as exc:
            issue(exc.info.code if exc.info.code in {
                "REGISTRATION_CAPTURE_MISSING", "REGISTRATION_CAPTURE_PATIENT_MISMATCH",
                "REGISTRATION_PAGINATION_INCOMPLETE",
            } else "REGISTRATION_REPLAY_INCOMPLETE")
        except (ConfigurationError, UnicodeError, ValueError):
            issue("REGISTRATION_REPLAY_INCOMPLETE")
    if by_name:
        issue("REGISTRATION_JOURNAL_MISMATCH")
    actual_files = {name for name in reader.names if name.startswith(_ROOT + "patients/") and name.endswith(".json")}
    if actual_files - expected_files:
        issue("REGISTRATION_RESULT_UNEXPECTED")
    counts = {
        "requested_patient_count": len(mrns),
        "completed_patient_count": sum(statuses[key] for key in ("OK", "EMPTY", "ERROR")),
        "blocked_patient_count": statuses["BLOCKED"],
        "pending_patient_count": len(mrns) - sum(statuses.values()),
        **{key: totals[key] for key in ("record_count", "no_show_count", "attendance_unknown_count")},
    }
    if any(type(recorded.get(key)) is not int or recorded[key] != value for key, value in counts.items()) or (
        recorded.get("patient_status_counts") != {key: statuses[key] for key in _STATUSES}
        or recorded.get("registered_by_codes") != dict(codes)
        or recorded.get("patients") != "patients.json" or recorded.get("patient_results") != "patient_results.jsonl"
        or (counts["pending_patient_count"] and recorded.get("status") not in {"RUNNING", "INTERRUPTED"})
        or (run.get("status") == "OK" and (counts["pending_patient_count"] or statuses["ERROR"] or statuses["BLOCKED"]))
    ):
        issue("REGISTRATION_SUMMARY_MISMATCH")
    if run.get("patient_count") not in (None, len(mrns)):
        issue("REGISTRATION_SUMMARY_MISMATCH")
    no_show_steps = [step for step in steps if step.get("name") == "registrations.coverage.no_show"]
    successful = statuses["OK"] + statuses["EMPTY"]
    if successful and recorded.get("status") not in {"RUNNING", "INTERRUPTED"} and (len(no_show_steps) != 1 or no_show_steps[0].get("status") != (
        "OK" if totals["no_show_count"] else "NO_SAMPLE"
    )):
        issue("REGISTRATION_COVERAGE_MISMATCH")
    original_status = recorded.get("status")
    return {
        "status": "ERROR" if issues else "OBSERVED",
        "recorded_status": original_status if original_status in {
            "OK", "COMPLETED_WITH_GAPS", "COMPLETED_WITH_ERRORS", "AUTHENTICATION_FAILED", "HTTP_FAILED",
            "RUNNING", "INTERRUPTED",
        } else "UNKNOWN",
        **counts, "patient_status_counts": {key: statuses[key] for key in _STATUSES},
        "verified_patient_count": totals["verified_patient_count"], "raw_response_count": totals["raw_response_count"],
        "distinct_registered_by_code_count": len(codes),
        "no_show_sample_status": "OBSERVED" if totals["no_show_count"] else "NO_SAMPLE" if successful else "BLOCKED",
        "issues": issues,
        "scope": "Configured MRN order, journal, saved rows, counts and complete captured pagination; no new HTTP. Raw source codes and patient identifiers remain local; original live status unchanged.",
    }
