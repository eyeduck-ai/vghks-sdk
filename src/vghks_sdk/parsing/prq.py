"""Pure PRQ outpatient, visit, SOAP, and numeric parsers."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from ..core.errors import ParseError
from ..core.jsliteral import (
    decode_js_string,
    evaluate_expression,
    extract_quoted_strings,
    iter_active_constructor_calls,
    iter_constructor_calls,
    string_assignments,
)
from ..models import (
    CaseDetail,
    NumericReport,
    OutpatientPatient,
    VisitCase,
)
from .clinical import parse_numeric_tables
from .common import (
    normalize_inline_text,
    sort_visit_cases,
    strip_markup,
)
from .soap import parse_soap as parse_soap

_JS_LITERAL_PATTERN = r"(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_MRN_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")


def parse_opd_patients(
    html_text: str,
    *,
    visit_date: date,
    doctor_card: str,
) -> list[OutpatientPatient]:
    soup = BeautifulSoup(html_text, "html.parser")
    indexed = {
        "section": _parse_indexed_string_array(html_text, "aryOpdSec"),
        "room": _parse_indexed_string_array(html_text, "aryOpdRoom"),
        "doctor": _parse_indexed_string_array(html_text, "aryOpdDoc"),
    }
    patients: list[OutpatientPatient] = []
    seen: set[tuple[str, date, str, str]] = set()
    allowed_variables = {"labStr", "hnamecStr", "hvip", "numStr", "numStr2"}
    for script_index, script in enumerate(soup.find_all("script")):
        if script.get("src"):
            continue
        source = script.string if script.string is not None else script.get_text()
        if "new KSCase" not in source:
            continue
        variables = string_assignments(source, allowed_variables)
        room_index = _room_index(script)
        section = indexed["section"].get(room_index, "") if room_index is not None else ""
        room = indexed["room"].get(room_index, "") if room_index is not None else ""
        page_doctor = indexed["doctor"].get(room_index, "") if room_index is not None else ""
        for call in iter_constructor_calls(source, "KSCase"):
            if len(call.arguments) < 6:
                continue
            values = [evaluate_expression(arg, variables) for arg in call.arguments]
            mrn = normalize_inline_text(values[2]) if values[2] is not None else ""
            if values[2] is None or (mrn and not _MRN_RE.fullmatch(mrn)):
                continue
            # New registrations may not have an MRN yet. Keep each such row
            # for debugging, while deduplicating its alternate JS branches.
            identity = (mrn or f"missing-mrn-script-{script_index}", visit_date, section, room)
            if identity in seen:
                continue
            seen.add(identity)
            patients.append(
                OutpatientPatient(
                    mrn=mrn,
                    name=strip_markup(values[3] or ""),
                    visit_date=visit_date,
                    sex=strip_markup(values[4] or ""),
                    age=strip_markup(values[5] or ""),
                    section_code=section,
                    room=room,
                    # An explicitly blank room doctor is a shared list. Never
                    # fill it with the query account: that invents ownership.
                    doctor_card=normalize_inline_text(page_doctor),
                    doctor_label_present=room_index in indexed["doctor"],
                )
            )
    return patients


def require_patient_context(html_text: str) -> None:
    """Recognize the successful query frames before reading session state."""
    soup = BeautifulSoup(html_text, "html.parser")
    paths = {urlsplit(str(frame.get("src", ""))).path for frame in soup.find_all("frame")}
    if not (
        any(path.endswith("/Page/JSP/KS_Patient.jsp") for path in paths)
        and any(path.endswith("/QueryCaseList.do") for path in paths)
    ):
        raise ParseError(
            "patient lookup did not establish a recognizable patient context",
            code="PRQ_PATIENT_CONTEXT_UNCONFIRMED",
        )


def parse_patient_identity(html_text: str, *, expected_national_id: str = "") -> str:
    """Resolve the MRN from the recorded patient header, checking an ID if supplied."""
    soup = BeautifulSoup(html_text, "html.parser")
    nodes = soup.select("#pHistno")
    mrns = {normalize_inline_text(node.get_text()) for node in nodes}
    if len(mrns) != 1 or not _MRN_RE.fullmatch(next(iter(mrns), "")):
        raise ParseError(
            "patient header MRN is missing or ambiguous", code="PRQ_PATIENT_MRN_MISSING"
        )
    if expected_national_id:
        identifiers = set()
        for value in extract_quoted_strings(html_text):
            if "hidno=" not in value:
                continue
            decoded = html.unescape(value).strip()
            # The header concatenates a dynamic login URL with a literal
            # "&hidno=...&..." suffix. urlsplit treats that suffix as a path.
            # Read its complete query field without executing JavaScript or
            # accepting an ID merely because it occurs somewhere in the HTML.
            query_text = decoded[1:] if decoded.startswith("&") else urlsplit(decoded).query
            query = parse_qs(query_text, keep_blank_values=True)
            identifiers.update(
                normalize_inline_text(item).upper() for item in query.get("hidno", [])
            )
        if identifiers != {expected_national_id.upper()}:
            raise ParseError(
                "patient header did not confirm the requested national ID",
                code="PRQ_PATIENT_ID_MISMATCH",
            )
    return next(iter(mrns))


def _visit_links(html_text: str) -> Iterator[tuple[str, str | None]]:
    # JSP emits both rendering branches for each visit. Read the statically
    # active constructor before deduplication; the inactive one can omit the
    # physician card and detail context. Never run JavaScript or merge branches.
    soup = BeautifulSoup(html_text, "html.parser")
    scripts = soup.find_all("script")
    if not scripts:
        yield from _visit_source_links(html_text)
        return
    for script in scripts:
        yield from _visit_source_links(script.get_text())
        script.decompose()
    yield from _visit_source_links(str(soup))


def _visit_source_links(source: str) -> Iterator[tuple[str, str | None]]:
    active = {call.start: call for call in iter_active_constructor_calls(source, "KSCase")}
    # Mask ALL constructors, including inactive ones, so legacy literal-link
    # fallback cannot resurrect them or parse incomplete concatenated fragments.
    remaining = []
    cursor = 0
    for call in iter_constructor_calls(source, "KSCase"):
        remaining.append(source[cursor : call.start])
        cursor = call.end
        call = active.get(call.start)
        if call is None:
            continue
        if not call.arguments or "QueryCaseDetail.do?" not in call.arguments[0]:
            continue
        href = evaluate_expression(call.arguments[0], {})
        doctor = evaluate_expression(call.arguments[4], {}) if len(call.arguments) >= 5 else None
        if href is None or (len(call.arguments) >= 5 and doctor is None):
            raise ParseError(
                "visit link or physician expression is unsupported",
                code="PRQ_CASE_EXPRESSION_UNSUPPORTED",
            )
        yield href, doctor
    remaining.append(source[cursor:])
    for value in extract_quoted_strings(" ".join(remaining)):
        if "QueryCaseDetail.do?" in value:
            yield value, None


def parse_visit_cases(
    html_text: str, expected_mrn: str, *, expected_national_id: str = ""
) -> list[VisitCase]:
    expected = normalize_inline_text(expected_mrn)
    cases: list[VisitCase] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    allowed_detail_keys = {
        "hid",
        "dbSource",
        "index",
        "hhisnum",
        "caseType",
        "caseNo",
        "caseSec",
        "caseDT",
        "hdisDt",
        "caseSectC",
        "vsNo",
        "vsNm",
        "rsNo",
        "rsNm",
        "heramcas",
    }
    for value, doctor in _visit_links(html_text):
        decoded = html.unescape(value)
        if "QueryCaseDetail.do?" not in decoded or decoded.startswith("javascript:"):
            continue
        parsed = urlsplit(decoded)
        if not parsed.query or parsed.path.rsplit("/", 1)[-1] != "QueryCaseDetail.do":
            continue
        query = {
            key: values[-1]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        mrn = normalize_inline_text(query.get("hhisnum")) or expected
        if expected and mrn != expected:
            raise ParseError(
                "case list belongs to another patient", code="PRQ_CASE_PATIENT_MISMATCH"
            )
        if (
            expected_national_id
            and query.get("hidno")
            and normalize_inline_text(query["hidno"]).upper() != expected_national_id.upper()
        ):
            raise ParseError("case list national ID differs", code="PRQ_PATIENT_ID_MISMATCH")
        case = VisitCase(
            mrn=mrn,
            visit_date=_parse_iso_date(normalize_inline_text(query.get("caseDT"))),
            case_type=normalize_inline_text(query.get("caseType")),
            case_no=normalize_inline_text(query.get("caseNo")),
            section_code=normalize_inline_text(query.get("caseSec")),
            section_name=normalize_inline_text(query.get("caseSectC")),
            index=_safe_int(query.get("index")),
            detail_params={key: query[key] for key in allowed_detail_keys if key in query},
            doctor_name=strip_markup(doctor if doctor is not None else query.get("vsNm", "")),
            doctor_card=normalize_inline_text(query.get("vsNo")),
        )
        if not case.case_no or case.identity in seen:
            continue
        seen.add(case.identity)
        cases.append(case)
    return sort_visit_cases(cases)


def parse_case_detail(html_text: str, case: VisitCase) -> CaseDetail:
    soup = BeautifulSoup(html_text, "html.parser")
    container = soup.select_one("#tab_ul") or soup
    tab_ids: list[str] = []
    tab_titles: list[str] = []
    for item in container.find_all("li"):
        item_id = normalize_inline_text(item.get("id"))
        title = normalize_inline_text(item.get_text(" ", strip=True))
        if item_id:
            tab_ids.append(item_id)
        if title:
            tab_titles.append(title)
    return CaseDetail(case=case, tab_ids=tuple(tab_ids), tab_titles=tuple(tab_titles))


def parse_numeric_report(html_text: str, case: VisitCase) -> NumericReport:
    return NumericReport(case=case, tables=parse_numeric_tables(html_text))


def _parse_indexed_string_array(source: str, name: str) -> dict[int, str]:
    pattern = re.compile(
        rf"\b{re.escape(name)}\s*\[\s*(?P<index>\d+)\s*\]\s*=\s*{_JS_LITERAL_PATTERN}\s*;",
        re.DOTALL,
    )
    result: dict[int, str] = {}
    for match in pattern.finditer(source):
        decoded = decode_js_string(match.group("value"))
        if decoded is not None:
            result[int(match.group("index"))] = normalize_inline_text(decoded)
    return result


def _room_index(script: Tag) -> int | None:
    # In the live JSP, malformed surrounding markup can put the patient
    # scripts outside their room div. Each patient script still references
    # exactly one aryOpdSec[index]; use that explicit index, never DOM order.
    source = script.string if script.string is not None else script.get_text()
    indices = {int(value) for value in re.findall(r"\baryOpdSec\s*\[\s*(\d+)\s*\]", source)}
    parent = script.find_parent(id=re.compile(r"^room\d+$"))
    if parent is not None:
        match = re.fullmatch(r"room(\d+)", str(parent.get("id")))
        if match:
            indices.add(int(match.group(1)))
    return next(iter(indices)) if len(indices) == 1 else None


def _parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
