"""Reclassify recorded OPD lists by physician without inventing new SOAP results."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from ..contracts.har import HarEntry
from ..parsing.prq import parse_opd_patients
from ..workflows.opd_soap import classify_opd_registration
from .bundle import BundleReader
from .replay import request_parts


def review_weekly_physicians(reader: BundleReader) -> dict:
    root = "parsed/workflows/opd_soap_week/"
    manifest_path = root + "manifest.json"
    if manifest_path not in reader.names:
        return {"status": "NOT_TESTED"}
    manifest = reader.json(manifest_path)
    card = str(manifest.get("source", {}).get("card_no") or "")
    if not card:
        return {"status": "SOURCE_MISSING"}
    recorded = {}
    for row in reader.jsonl("capture_manifest.jsonl"):
        match = re.fullmatch(
            r"weekly_opd\.stage1_opd\.(\d{4}-\d{2}-\d{2})", str(row.get("live_test_step", ""))
        )
        if match and row.get("kind") == "HTTP_EXCHANGE":
            _, path, _, _ = request_parts(row.get("request") or {})
            if path.endswith("/QueryOPDPatList.do"):
                recorded[match[1]] = row
    counts: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    days = []
    new_registrations = 0
    missing_mrn = 0
    patients: set[str] = set()
    old_patients: set[str] = set()
    for day, capture in sorted(recorded.items()):
        old_path = root + f"stage1_opd/{day}.json"
        old_rows = (
            reader.json(old_path).get("registrations", []) if old_path in reader.names else []
        )
        old_keys = {
            (
                r["patient"].get("mrn", ""),
                r["patient"].get("section_code", ""),
                r["patient"].get("room", ""),
            )
            for r in old_rows
            if r.get("scope") == "DEDICATED"
        }
        old_patients.update(key[0] for key in old_keys if key[0])
        response = capture.get("response") or {}
        if response.get("status_code") != 200 or not capture.get("response_file"):
            days.append({"date": day, "status": "UNAVAILABLE"})
            continue
        mime = next(
            (str(v) for k, v in response.get("headers", []) if str(k).lower() == "content-type"), ""
        )
        content = reader.read(capture["response_file"])
        text = HarEntry("", "", frozenset(), frozenset(), {}, 200, content, mime).text()
        rows = parse_opd_patients(text, visit_date=date.fromisoformat(day), doctor_card=card)
        daily: Counter[str] = Counter()
        for patient in rows:
            scope = classify_opd_registration(patient, doctor_card=card)
            daily[scope] += 1
            counts[scope] += 1
            if scope == "DEDICATED":
                sections[patient.section_code or "UNKNOWN"] += 1
                missing_mrn += not bool(patient.mrn)
                if patient.mrn:
                    patients.add(patient.mrn)
                if (patient.mrn, patient.section_code, patient.room) not in old_keys:
                    new_registrations += 1
        days.append({"date": day, "status": "RECLASSIFIED", "counts": dict(daily)})
    return {
        "status": "RECLASSIFIED" if recorded else "NO_RAW_LISTS",
        "recorded_policy_version": manifest.get("schema_version", 1),
        "classification": "exact room physician matches account or account + F; blank physician is shared",
        "counts": dict(counts),
        "dedicated_section_counts": dict(sections),
        "dedicated_patients": len(patients),
        "dedicated_missing_mrn": missing_mrn,
        "additional_registrations": new_registrations,
        "additional_patients": len(patients - old_patients),
        "days": days,
        "scope": "Offline reclassification only. Original visit/SOAP coverage and matches are unchanged; newly included registrations need their own same-day visit/SOAP checks.",
    }
