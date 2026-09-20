"""Pure DDPortal parsing. Scripts, detail links and SMS actions are never executed."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from ..models.personnel import PersonnelOption, PersonnelOptions, PersonnelRecord
from .common import direct_rows, normalize_inline_text


def _error() -> ParseError:
    return ParseError("personnel response schema changed", code="PERSONNEL_SCHEMA_CHANGED")


def parse_personnel_options(text: str) -> PersonnelOptions:
    soup = BeautifulSoup(text, "html.parser")
    forms = [
        form
        for form in soup.find_all("form")
        if urlsplit(str(form.get("action", ""))).path == "/DDPortal/dRDoctor.do"
        and str(form.get("method", "")).lower() == "post"
    ]
    if len(forms) != 1:
        raise _error()
    form = forms[0]
    for key, value in (("reqCode", "showAllDoctors"), ("value(source)", "DR")):
        node = form.find("input", attrs={"name": key})
        if node is None or node.get("value") != value:
            raise _error()
    if not all(form.find("input", attrs={"name": key}) for key in ("value(name)", "value(usrId)")):
        raise _error()
    subunits = form.find("input", attrs={"name": "value(subOU)"})
    if subunits is None or subunits.get("value") != "Y":
        raise _error()

    def options(key: str) -> tuple[PersonnelOption, ...]:
        select = form.find("select", attrs={"name": key})
        if select is None:
            raise _error()
        result: dict[str, PersonnelOption] = {}
        for item in select.find_all("option"):
            if item.has_attr("disabled"):
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            option = PersonnelOption(value, normalize_inline_text(item.get_text(" ", strip=True)))
            if value in result and result[value] != option:
                raise _error()
            result[value] = option
        if not result:
            raise _error()
        return tuple(result.values())

    return PersonnelOptions(options("value(title)"), options("value(costId)"))


def parse_personnel_records(text: str) -> list[PersonnelRecord]:
    soup = BeautifulSoup(text, "html.parser")
    tables = soup.find_all("table", id="drlistTb")
    if len(tables) != 1:
        raise _error()
    rows = direct_rows(tables[0])
    if not rows:
        raise _error()
    headers = [
        re.sub(r"\s+", "", c.get_text()) for c in rows[0].find_all(["td", "th"], recursive=False)
    ]
    required = {"姓名", "醫師章號", "醫師類別", "單位", "院內分機", "電話一", "電話二", "電話三"}
    if not required.issubset(headers) or len(set(headers)) != len(headers):
        raise _error()
    records: list[PersonnelRecord] = []
    ids: set[str] = set()
    totals: list[int] = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) == 1 and str(cells[0].get("colspan")) == str(len(headers)):
            label = normalize_inline_text(cells[0].get_text(" ", strip=True))
            if row.get("id") == "tfoot" and label.startswith("查詢結果"):
                visible = re.search(r"共有\s*(\d{1,8})\s*筆", label)
                script = " ".join(node.get_text() for node in cells[0].find_all("script"))
                literal_sum = re.search(r"\beval\(\s*(\d{1,8})\s*\+\s*(\d{1,8})\s*\)", script)
                # Recognize the recorded counter's two integer literals only;
                # never evaluate arbitrary JavaScript to obtain a total.
                if visible:
                    totals.append(int(visible[1]))
                if literal_sum:
                    totals.append(int(literal_sum[1]) + int(literal_sum[2]))
            # Two recorded footer rows: result count and a hidden SMS button.
            if (
                (row.get("id") == "tfoot" and label.startswith("查詢結果"))
                or (
                    not label
                    and cells[0].find("input", attrs={"name": "action", "value": "傳呼簡訊"})
                )
                or label in {"查無資料", "查無資料!"}
            ):
                continue
            raise _error()
        if len(cells) != len(headers):
            raise _error()
        fields = {
            key: normalize_inline_text(cell.get_text(" ", strip=True))
            for key, cell in zip(headers, cells)
        }
        if all(fields[key] == key for key in required):  # repeated table header
            continue
        checkbox = row.find("input", attrs={"name": "valueA(usrId)"})
        detail = row.find("img", attrs={"onclick": re.compile(r"^\s*listDetail\(\);?\s*$")})
        identifiers = {
            str(node.get(attr, "")).strip().upper()
            for node, attr in ((checkbox, "value"), (detail, "name"))
            if node is not None
        }
        if len(identifiers) != 1:
            raise _error()
        employee_id = identifiers.pop()
        if (
            not re.fullmatch(r"[A-Z0-9_-]{1,6}", employee_id)
            or not fields["姓名"]
            or employee_id in ids
        ):
            raise _error()
        ids.add(employee_id)
        records.append(
            PersonnelRecord(
                employee_id=employee_id,
                name=fields["姓名"],
                doctor_stamp_no=fields["醫師章號"],
                title=fields["醫師類別"],
                unit=fields["單位"],
                extension=fields["院內分機"],
                phone_numbers=tuple(fields[key] for key in ("電話一", "電話二", "電話三")),
                fields=fields,
                unrendered_fields=tuple(
                    key for key, cell in zip(headers, cells) if cell.find("script")
                ),
            )
        )
    if any(total != len(records) for total in totals):
        raise ParseError("personnel result count mismatch", code="PERSONNEL_COUNT_MISMATCH")
    return records
