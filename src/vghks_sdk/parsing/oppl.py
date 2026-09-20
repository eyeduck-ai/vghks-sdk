"""Pure OPPL surgery JSON parser."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.errors import ParseError
from ..models import SurgeryRecord
from .common import normalize_inline_text

OPPL_JSON_FIELDS = {
    "patient_info": ("patient", "surgs"),
    "patient_consents": ("patient", "surgs", "consents"),
    "request_numbers": ("reqs",),
    "anticoagulants": ("status",),
    "sglt2": ("status",),
    "procedure_catalog": ("pfiles", "chargenos"),
    "holidays": ("holidays", "maxDate"),
    "supply_model": ("items", "status"),
    "consent_catalog": ("forms", "status"),
    "consent_template": ("status", "diseasename", "opname1"),
    "consent_doctor": ("status", "upuser"),
}


def parse_oppl_payload(key: str, payload: Any, mrn: str = "") -> dict[str, Any]:
    expected = OPPL_JSON_FIELDS[key.removeprefix("oppl.")]
    if not isinstance(payload, dict) or not all(key in payload for key in expected):
        raise ParseError("OPPL JSON schema changed", code="OPPL_JSON_SHAPE_INVALID")
    for key in ("surgs", "consents", "reqs", "pfiles", "chargenos", "items"):
        if key in expected and not isinstance(payload[key], list):
            raise ParseError("OPPL list schema changed", code="OPPL_JSON_SHAPE_INVALID")
    for key in ("patient", "forms", "upuser"):
        if key in expected and not isinstance(payload[key], dict):
            raise ParseError("OPPL object schema changed", code="OPPL_JSON_SHAPE_INVALID")
    if mrn and "patient" in expected and str(payload["patient"].get("hhisnum", "")) != mrn:
        raise ParseError("OPPL patient context mismatch", code="PATIENT_CONTEXT_MISMATCH")
    return payload


def parse_surgery_records(payload: Any) -> list[SurgeryRecord]:
    if not isinstance(payload, Mapping):
        raise ParseError(
            "surgery response had an unexpected JSON shape",
            code="OPPL_SURGERY_JSON_SHAPE_INVALID",
        )
    rows = payload.get("surgs")
    if not isinstance(rows, list):
        raise ParseError(
            "surgery response did not contain a list",
            code="OPPL_SURGERY_LIST_MISSING",
        )
    output: list[SurgeryRecord] = []
    selected = {
        "orhisnum",
        "orcaseno",
        "ordate",
        "orbgndt",
        "orbgntm",
        "orendtm",
        "oroproom",
        "oproom",
        "ordocno",
        "ordocnum",
        "ordocnam",
        "ordocnm",
        "oropnm1",
        "oropmnm",
        "ornstats",
    }
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise ParseError("invalid surgery row", code="OPPL_SURGERY_ROW_INVALID")
        row = raw_row

        def value(*keys: str, source: Mapping[Any, Any] = row) -> str:
            return next(
                (
                    normalized
                    for key in keys
                    if (
                        normalized := normalize_inline_text(
                            source[key].get("dts", "")
                            if isinstance(source.get(key), Mapping)
                            else source.get(key)
                        )
                    )
                ),
                "",
            )

        output.append(
            SurgeryRecord(
                patient_mrn=value("orhisnum"),
                case_no=value("orcaseno"),
                surgery_date=value("orbgndt", "ordate"),
                start_time=value("orbgntm"),
                end_time=value("orendtm"),
                room=value("oroproom", "oproom"),
                doctor_card=value("ordocno", "ordocnum"),
                doctor_name=value("ordocnam", "ordocnm"),
                procedure=value("oropnm1", "oropmnm"),
                status=value("ornstats"),
                extra={str(key): item for key, item in row.items() if key not in selected} or None,
            )
        )
    return output
