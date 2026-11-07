"""Commit records shared by registration batches and offline verification."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

REGISTRATION_ROOT = "parsed/registrations/"
REGISTRATION_QUERY = "webmaas.registration_query"
PATIENT_STATUSES = ("OK", "EMPTY", "ERROR", "BLOCKED")
COUNT_FIELDS = ("record_count", "no_show_count", "attendance_unknown_count")
JOURNAL_VERSION = 1


def patient_index(mrns: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"index": index, "mrn": mrn, "output": f"patients/{index:06d}.json"}
        for index, mrn in enumerate(mrns, 1)
    ]


def read_patient_journal(
    lines: Iterable[bytes], mrns: Sequence[str],
) -> tuple[list[dict], str]:
    """Read the contiguous prefix of complete commits and report the first bad line."""
    entries: list[dict] = []
    for index, line in enumerate(lines, 1):
        if not line.endswith(b"\n"):
            return entries, "REGISTRATION_JOURNAL_TRUNCATED"
        try:
            entry = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            return entries, "REGISTRATION_JOURNAL_INVALID"
        if not isinstance(entry, dict) or not isinstance(entry.get("step"), dict):
            return entries, "REGISTRATION_JOURNAL_INVALID"
        step = entry["step"]
        if (
            index > len(mrns)
            or type(entry.get("index")) is not int or entry["index"] != index
            or entry.get("mrn") != mrns[index - 1]
            or step.get("name") != f"registrations.patient_{index:06d}"
            or step.get("operation") != REGISTRATION_QUERY
        ):
            return entries, "REGISTRATION_JOURNAL_MISMATCH"
        if not _valid_patient_counts(step, entry.get("registered_by_codes")):
            return entries, "REGISTRATION_JOURNAL_INVALID"
        entries.append(entry)
    return entries, ""


def _valid_patient_counts(step: dict, codes: Any) -> bool:
    status = step.get("status")
    if status not in PATIENT_STATUSES or not isinstance(codes, dict):
        return False
    if status in ("ERROR", "BLOCKED"):
        return not codes
    details = step.get("details")
    if not isinstance(details, dict) or any(
        type(details.get(field)) is not int or details[field] < 0 for field in COUNT_FIELDS
    ):
        return False
    return (
        all(type(count) is int and count > 0 for count in codes.values())
        and sum(codes.values()) == details["record_count"]
        and details["no_show_count"] + details["attendance_unknown_count"] <= details["record_count"]
        and bool(details["record_count"]) == (status == "OK")
    )


def registration_batch_snapshot(
    entries: Sequence[dict], *, patient_count: int, status: str,
) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    for entry in entries:
        step = entry["step"]
        statuses[step["status"]] += 1
        if step["status"] in ("OK", "EMPTY"):
            counts.update({key: step["details"][key] for key in COUNT_FIELDS})
            codes.update(entry["registered_by_codes"])
    return {
        "schema_version": 1, "patient_journal_version": JOURNAL_VERSION,
        "status": status, "requested_patient_count": patient_count,
        "completed_patient_count": statuses["OK"] + statuses["EMPTY"] + statuses["ERROR"],
        "blocked_patient_count": statuses["BLOCKED"],
        "pending_patient_count": patient_count - len(entries),
        "patient_status_counts": {key: statuses[key] for key in PATIENT_STATUSES},
        **{key: counts[key] for key in COUNT_FIELDS},
        "registered_by_codes": dict(codes),
        "patients": "patients.json", "patient_results": "patient_results.jsonl",
    }
