"""Pure WebMAAS demographics and registration parsers."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from ..core.errors import NotFoundError, ParseError
from ..models import PatientBasicInfo, PatientDemographics, RegistrationRecord
from .common import direct_rows, map_columns, normalize_inline_text, unique_headers


def parse_patient_demographics(payload: Any, expected_mrn: str) -> PatientDemographics:
    if isinstance(payload, Mapping):
        rows = [payload]
    elif isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, Mapping)]
    else:
        raise ParseError(
            "patient response had an unexpected JSON shape",
            code="WEBMAAS_DEMOGRAPHICS_JSON_SHAPE_INVALID",
        )
    expected = normalize_inline_text(expected_mrn)
    exact = [row for row in rows if normalize_inline_text(row.get("patno")) == expected]
    if len(exact) == 1:
        row = exact[0]
    elif not exact and len(rows) == 1 and not normalize_inline_text(rows[0].get("patno")):
        row = rows[0]
    elif not exact:
        raise NotFoundError(
            "patient was not present in the demographic response",
            code="WEBMAAS_PATIENT_NOT_FOUND",
        )
    else:
        raise ParseError(
            "patient response contained multiple exact records",
            code="WEBMAAS_DEMOGRAPHICS_DUPLICATE",
        )
    known = {
        "patno",
        "patname",
        "hphonno",
        "homephon",
        "birthday",
        "hsex",
        "address",
        "patid",
        "hkinname",
    }
    extra = {
        str(key): value
        for key, value in row.items()
        if key not in known and key in {"returnCode", "returnValue", "hactstat", "hactstatText"}
    }
    mrn = normalize_inline_text(row.get("patno")) or expected
    if not mrn:
        raise ParseError(
            "patient response did not contain a medical record number",
            code="WEBMAAS_DEMOGRAPHICS_MRN_MISSING",
        )
    return PatientDemographics(
        mrn=mrn,
        name=normalize_inline_text(row.get("patname")),
        mobile_phone=normalize_inline_text(row.get("hphonno")),
        home_phone=normalize_inline_text(row.get("homephon")),
        birthday=normalize_inline_text(row.get("birthday")),
        sex=normalize_inline_text(row.get("hsex")),
        address=normalize_inline_text(row.get("address")),
        extra=extra or None,
    )


def parse_registration_records(html_text: str, expected_mrn: str = "") -> list[RegistrationRecord]:
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", id="row")
    if table is None:
        raise ParseError(
            "registration result table was missing",
            code="WEBMAAS_REGISTRATION_STRUCTURE_MISSING",
        )
    headers = unique_headers(
        [normalize_inline_text(cell.get_text(" ", strip=True)) for cell in table.find_all("th")]
    )
    records: list[RegistrationRecord] = []
    for row in direct_rows(table):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        values = [normalize_inline_text(cell.get_text(" ", strip=True)) for cell in cells]
        if not any(values):
            continue
        if len(cells) == 1 and cells[0].get("colspan") and _is_empty(values[0]):
            continue
        if len(headers) != len(values) or not headers:
            raise ParseError(
                "registration columns did not match their headers",
                code="WEBMAAS_REGISTRATION_COLUMNS_INVALID",
            )
        columns = map_columns(headers, values)
        normalized = {_label(key): value for key, value in columns.items()}
        mrn = normalized.get("病歷號", "")
        if expected_mrn and mrn != normalize_inline_text(expected_mrn):
            raise ParseError(
                "registration belonged to a different or unidentified patient",
                code="WEBMAAS_PATIENT_MISMATCH",
            )
        section = normalized.get("科別", "").split(maxsplit=1)
        records.append(
            RegistrationRecord(
                columns=columns,
                mrn=mrn,
                name=normalized.get("姓名", ""),
                visit_date=_date(normalized.get("看診日期", "")),
                section_code=section[0] if section else "",
                section_name=section[1] if len(section) > 1 else "",
                room=normalized.get("診別", ""),
                sequence_no=normalized.get("序號", ""),
                expected_time=normalized.get("預計看診", ""),
                status=normalized.get("狀態", ""),
                registered_at=normalized.get("掛號時間", ""),
                registered_by=normalized.get("掛號人員", ""),
                cancelled_at=normalized.get("取消時間", ""),
                cancelled_by=normalized.get("取消人員", ""),
                notes=normalized.get("註記", ""),
            )
        )
    return records


def find_next_displaytag_href(html_text: str) -> str | None:
    soup = BeautifulSoup(html_text, "html.parser")
    for link in soup.find_all("a", href=True):
        href = str(link.get("href"))
        text = normalize_inline_text(link.get_text(" ", strip=True)).lower()
        title = normalize_inline_text(link.get("title")).lower()
        rel = " ".join(link.get("rel") or []).lower()
        if not re.search(r"(?:[?&]|&amp;)d-\w+-p=", href):
            continue
        if (
            "next" in rel.split()
            or text in {">", "\u203a"}
            or any(marker in text or marker in title for marker in ("下一", "next"))
        ):
            return html.unescape(href)
    return None


def parse_query_form(html_text: str, form_id: str) -> dict[str, str]:
    """Collect fresh hidden form state without executing page scripts."""
    form = BeautifulSoup(html_text, "html.parser").find("form", id=form_id)
    if form is None:
        raise ParseError("WebMAAS query form was missing", code="WEBMAAS_QUERY_FORM_MISSING")
    fields = {
        str(node["name"]): str(node.get("value", ""))
        for node in form.select('input[type="hidden"][name]')
    }
    if not fields.get("org.apache.struts.taglib.html.TOKEN", "").strip():
        raise ParseError("WebMAAS query token was missing", code="WEBMAAS_QUERY_TOKEN_MISSING")
    fields.pop("buttonName", None)
    return fields


def parse_patient_basic_info(html_text: str, expected_mrn: str) -> PatientBasicInfo:
    """Parse the recorded QUY15 labelled detail panel, keeping unknown fields."""
    soup = BeautifulSoup(html_text, "html.parser")
    detail = soup.find(id="DETAIL")
    if detail is None:
        result = soup.find(id="LIST")
        if result is not None and _is_empty(result.get_text(" ", strip=True)):
            raise NotFoundError("patient detail was unavailable", code="WEBMAAS_PATIENT_NOT_FOUND")
        raise ParseError(
            "patient detail panel was missing", code="WEBMAAS_BASIC_INFO_STRUCTURE_MISSING"
        )
    fields: dict[str, str] = {}
    last_label = ""
    for group in detail.select(".form-group"):
        label_node = group.find("label")
        if label_node is None:
            continue
        label = _label(label_node.get_text(" ", strip=True))
        values = group.select("span.label")
        # An unlabelled following group is the second diagnosis line in this HAR.
        if not label:
            if last_label != "入院診斷" or not values:
                continue
            label = last_label
        last_label = label
        key, number = label, 1
        while key in fields:
            number += 1
            key = f"{label}_{number}"
        fields[key] = normalize_inline_text(" ".join(v.get_text(" ", strip=True) for v in values))
    mrn = fields.get("病歷號", "")
    if not mrn:
        if _is_empty(detail.get_text(" ", strip=True)):
            raise NotFoundError("patient detail was unavailable", code="WEBMAAS_PATIENT_NOT_FOUND")
        raise ParseError("patient identity was missing", code="WEBMAAS_BASIC_INFO_IDENTITY_MISSING")
    if mrn != normalize_inline_text(expected_mrn):
        raise ParseError("patient detail identity did not match", code="WEBMAAS_PATIENT_MISMATCH")
    sex_age = fields.get("性別", "")
    sex, _, age = sex_age.partition("(")
    return PatientBasicInfo(
        mrn=mrn,
        name=fields.get("姓名", ""),
        national_id=fields.get("身分證號", ""),
        birthday=_date(fields.get("生日", "")),
        sex=sex.strip(),
        age=age.rstrip(") ").strip(),
        blood_type=fields.get("血型", ""),
        height=fields.get("身高", ""),
        weight=fields.get("體重", ""),
        nationality=fields.get("國籍", ""),
        phone=fields.get("電話", ""),
        address=fields.get("地址", ""),
        contact=fields.get("聯絡人", ""),
        ward_bed=fields.get("病房床位", ""),
        section=fields.get("科別", ""),
        insurance=fields.get("身分", ""),
        admitted_at=fields.get("入院日期", ""),
        discharge_notified_at=fields.get("通知出院時間", ""),
        discharged_at=fields.get("出院時間", ""),
        case_no=fields.get("CASE", ""),
        admission_diagnosis=tuple(
            value
            for key, value in fields.items()
            if (key == "入院診斷" or key.startswith("入院診斷_")) and value
        ),
        notices=tuple(
            normalize_inline_text(node.get_text(" ", strip=True))
            for node in detail.select(".alert")
        ),
        fields=fields,
        raw_html=html_text,
    )


def _label(value: str) -> str:
    return "".join(normalize_inline_text(value).split())


def _is_empty(value: str) -> bool:
    text = _label(value).lower()
    return any(
        marker in text for marker in ("查無資料", "無此病人", "查無病人", "nothingfoundtodisplay")
    )


def _date(value: str) -> date | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d{2,4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return date(year + 1911 if len(match[1]) < 4 else year, month, day)
        except ValueError:
            pass
    raise ParseError("WebMAAS returned an invalid date", code="WEBMAAS_DATE_INVALID")
