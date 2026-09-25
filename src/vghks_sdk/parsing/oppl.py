"""Pure OPPL surgery JSON parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..core.errors import ParseError
from ..models import SurgeryRecord, SurgeryScheduleProcedure
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


def _schedule_time_status(schedule_time: str) -> str:
    """Classify the displayed OPPL time without treating TF as a clock time."""

    if re.fullmatch(r"TF\d*", schedule_time, re.IGNORECASE):
        return "UNCONFIRMED"
    if re.fullmatch(r"(?:[01]\d|2[0-3])[0-5]\d", schedule_time):
        return "CLOCK_TIME"
    return "UNKNOWN"


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

        patient = row.get("patient")
        patient_data = patient if isinstance(patient, Mapping) else {}
        patient_mrn = value("orhisnum")
        nested_mrn = value("hhisnum", source=patient_data)
        if patient_mrn and nested_mrn and nested_mrn != patient_mrn:
            raise ParseError(
                "surgery row patient context mismatch",
                code="OPPL_SURGERY_PATIENT_MISMATCH",
            )
        ward_code = value("hnursta", source=patient_data)
        bed_no = value("hbedno", source=patient_data)
        ward = (
            f"{ward_code}-{bed_no}"
            if ward_code and ward_code != "OPD" and bed_no
            else ward_code or "OPD"
        )
        schedule_time = value("optime")
        time_status = _schedule_time_status(schedule_time)
        procedures = []
        for index in range(1, 5):
            code = value(f"oropnc{index}")
            name = value(f"oropnm{index}")
            if code or name:
                procedures.append(SurgeryScheduleProcedure(position=index, code=code, name=name))
        diagnosis_codes = tuple(code for index in range(1, 5) if (code := value(f"oropicd{index}")))
        source_fields = deepcopy({str(key): item for key, item in row.items()})

        output.append(
            SurgeryRecord(
                patient_mrn=patient_mrn,
                case_no=value("orcaseno"),
                surgery_date=value("orbgndt", "ordate"),
                # OPPL stores 23:59:00 in orbgntm for TF slots. It is a
                # placeholder, not the time shown to the user.
                start_time="" if time_status == "UNCONFIRMED" else value("orbgntm"),
                end_time=value("orendtm"),
                room=value("oproom", "oroproom"),
                doctor_card=value("ordocno", "ordocnum"),
                doctor_name=value("ordocnm", "ordocnam"),
                procedure=value("oropnm1", "oropmnm"),
                # Older callers may already read orstatus from extra; retain it
                # there while exposing the normalized status field as well.
                status=value("ornstats", "orstatus"),
                extra={str(key): item for key, item in row.items() if key not in selected} or None,
                ward=ward,
                anesthesia=value("oropamed"),
                category=value("orfreqnc"),
                patient_name=value("hnamec", source=patient_data),
                patient_sex=value("hsexc", source=patient_data),
                department=value("orcatgy"),
                schedule_time=schedule_time,
                time_status=time_status,
                request_no=value("orreqno"),
                sequence_no=value("ordseqno"),
                case_type=value("orcasetp"),
                internal_room_code=value("oroproom"),
                procedures=tuple(procedures),
                diagnosis_codes=diagnosis_codes,
                diagnosis_text=value("ordiag"),
                source_fields=source_fields,
            )
        )
    return output
