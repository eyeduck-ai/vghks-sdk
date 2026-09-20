"""Pure parsers for OPPL's completed surgery log and note locator."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..core.errors import ConfigurationError, ParseError
from ..models.surgery_cases import SurgeryCase, SurgeryCaseRef, SurgeryNoteRef


def parse_surgery_departments(payload: Any) -> list[str]:
    values = payload.get("deptlist") if isinstance(payload, dict) else None
    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        raise ParseError("surgery department list was invalid", code="SURGERY_DEPARTMENTS_INVALID")
    return list(dict.fromkeys(v.strip() for v in values))


def parse_surgery_cases(payload: Any) -> list[SurgeryCase]:
    rows = payload.get("oprmlist") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ParseError("surgery case list was missing", code="SURGERY_CASES_INVALID")
    # The recorded API returns the entire DataTables dataset. Refuse a future
    # paged/truncated response instead of silently labelling it complete.
    for key in ("total", "totalCount", "recordsTotal", "recordsFiltered"):
        if (
            isinstance(payload.get(key), (int, str))
            and str(payload[key]).isdecimal()
            and int(payload[key]) > len(rows)
        ):
            raise ParseError("surgery response was truncated", code="SURGERY_CASES_TRUNCATED")
    output: list[SurgeryCase] = []
    seen: dict[SurgeryCaseRef, SurgeryCase] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ParseError("invalid surgery case row", code="SURGERY_CASE_ROW_INVALID")

        def text(key: str, row: dict = row) -> str:
            value = row.get(key)
            if value is None:
                return ""
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ParseError("invalid surgery field", code="SURGERY_CASE_ROW_INVALID")
            return str(value).strip()

        try:
            ref = SurgeryCaseRef(text("hhisnum"), text("opreqno"), text("opseqno"))
            day = date.fromisoformat(text("op_date"))
        except (ConfigurationError, ValueError) as exc:
            raise ParseError(
                "surgery identity or date was invalid", code="SURGERY_CASE_ROW_INVALID"
            ) from exc
        item = SurgeryCase(
            reference=ref,
            surgery_date=day,
            procedure=text("op_name"),
            procedure_codes=tuple(text(f"code{i}") for i in range(1, 5) if text(f"code{i}")),
            patient_name=text("hnamec"),
            display_name=text("blockHnamec"),
            case_type=text("hcasetyp"),
            case_no=text("hcaseno"),
            department=text("sect"),
            surgeon_card=text("opdoct1"),
            surgeon_name=text("opdoct1n"),
            supervising_card=text("svdoct"),
            supervising_name=text("svdoctn"),
            attending_card=text("vsdoct"),
            attending_name=text("vsdoctn"),
            assistant_cards=tuple(text(f"opdoct{i}") for i in range(2, 6)),
            assistant_names=tuple(text(f"opdoct{i}n") for i in range(2, 6)),
            record_status=text("rpstatus"),
            fields={k: v for k, v in row.items() if k.lower() != "hid"},
        )
        if ref in seen and seen[ref] != item:
            raise ParseError("conflicting surgery rows", code="SURGERY_CASE_DUPLICATE_CONFLICT")
        if ref not in seen:
            output.append(item)
            seen[ref] = item
    return output


def parse_surgery_note(payload: Any, reference: SurgeryCaseRef) -> SurgeryNoteRef | None:
    if not isinstance(payload, dict) or payload.get("rtnYN") not in ("Y", "N"):
        raise ParseError(
            "surgery note availability was invalid", code="SURGERY_NOTE_RESPONSE_INVALID"
        )
    if payload["rtnYN"] == "N":
        return None
    try:
        return SurgeryNoteRef(reference, payload.get("path"))
    except ConfigurationError as exc:
        raise ParseError("surgery note path could not be validated", code=exc.info.code) from exc
