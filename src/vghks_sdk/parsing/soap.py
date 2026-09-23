"""Parse PRQ's labelled SOAP and printed summaries without interpreting medicine."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import date

from bs4 import BeautifulSoup, Tag

from ..core.errors import ParseError
from ..models import (
    SoapChronicPrescriptionPeriod,
    SoapDiagnosis,
    SoapMedication,
    SoapOrder,
    SoapRecord,
    VisitCase,
)
from .common import direct_rows, normalize_multiline_text

_LABELS = {
    "S": "S",
    "SUBJECTIVE": "S",
    "O": "O",
    "OBJECTIVE": "O",
    "A+P": "AP",
    "A/P": "AP",
    "AP": "AP",
    "ASSESSMENT+PLAN": "AP",
    "A": "A",
    "ASSESSMENT": "A",
    "P": "P",
    "PLAN": "P",
}
_INLINE_LABEL = re.compile(
    r"^\s*(S|O|A\s*\+\s*P|A/P|AP|A|P|Subjective|Objective|Assessment|Plan)\s*[:\uff1a]",
    re.IGNORECASE,
)
_DIAGNOSIS_HEADER = re.compile(r"^\s*ICD\s*(?:-?\s*(9|10)(-CM)?)?\s*碼?\s*[:\uff1a]", re.I)
_DIAGNOSIS_ROW = re.compile(
    r"^\s*(?P<code>(?:[A-Z][0-9][A-Z0-9]{1,2}|[0-9]{3})(?:\.[A-Z0-9]{1,4})?)"
    r"(?:\s+(?P<name>\S.*))?\s*$",
    re.I,
)
_MEDICATION_COLUMNS = ("劑量", "單位", "途徑", "頻次", "天數", "總發藥量")
_ORDER_HEADER = re.compile(r"^\s*檢查驗項目\s+數量(?:\s+檢查驗項目\s+數量)*\s*$")
_QUANTITY = re.compile(r"\s*(?P<quantity>\d+(?:\.\d+)?)(?=\s|$)")
_EMPTY_TEXT = {"查無資料", "查無SOAP資料", "無SOAP資料"}
_CHRONIC_NOTICE = "慢性病連續處方箋處方"
_CHRONIC_PERIOD = re.compile(
    r"^\s*慢性病連續處方箋處方\s*服藥期限\s*[:\uff1a]\s*"
    r"(?P<start>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*[\u223c\uff5e~至]\s*"
    r"(?P<end>\d{4}[-/]\d{1,2}[-/]\d{1,2})(?=\s|◆|$)"
)


def _label(text: str) -> str | None:
    key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).rstrip(":").upper()
    return _LABELS.get(key)


def _table_labels(container: Tag) -> dict[int, str]:
    """Follow a label's rowspan in its own table, never into a nested table."""
    labels: dict[int, str] = {}
    for table in container.find_all("table"):
        active: str | None = None
        remaining = 0
        for row in direct_rows(table):
            cells = row.find_all(["td", "th"], recursive=False)
            explicit = [
                (cell, code)
                for cell in cells
                if not cell.select(".soap pre") and (code := _label(cell.get_text()))
            ]
            if explicit:
                active = explicit[0][1] if len(explicit) == 1 else None
                span = str(explicit[0][0].get("rowspan", "1"))
                remaining = min(int(span), 1000) if span.isdecimal() else 1
                remaining = max(remaining, 1)
            elif len(cells) > 1 and cells[0].get_text(strip=True):
                active, remaining = None, 0
            if active and remaining:
                for pre in row.select(".soap pre"):
                    if pre.find_parent("table") is table:
                        labels[id(pre)] = active
            remaining -= 1
            if remaining <= 0:
                active = None
    return labels


def _diagnoses(text: str) -> tuple[list[SoapDiagnosis], bool]:
    header = _DIAGNOSIS_HEADER.match(text)
    assert header is not None
    system = f"ICD-{header[1]}{(header[2] or '').upper()}" if header[1] else "ICD"
    result: list[SoapDiagnosis] = []
    failed = False
    can_continue = False
    for line in text[header.end() :].splitlines():
        if not line.strip():
            continue
        match = _DIAGNOSIS_ROW.fullmatch(line)
        if match:
            result.append(SoapDiagnosis(match["code"], (match["name"] or "").strip(), system, line))
            can_continue = True
        elif (
            can_continue
            and line[:1].isspace()
            and re.fullmatch(r"[^\W\d_]+[,:;-]?", line.split()[0])
        ):
            # A wrapped description can continue only a successfully parsed row.
            previous = result[-1]
            result[-1] = replace(
                previous,
                name=f"{previous.name}\n{line.strip()}".strip(),
                raw_text=f"{previous.raw_text}\n{line}",
            )
        else:
            failed, can_continue = True, False
    return result, failed


def _display_cells(text: str) -> list[str]:
    cells: list[str] = []
    for char in text.expandtabs(8):
        if unicodedata.combining(char) and cells:
            cells[-1] += char
        else:
            cells.append(char)
            if unicodedata.east_asian_width(char) in {"W", "F"}:
                cells.append("")
    return cells


def _medications(text: str) -> tuple[list[SoapMedication], bool]:
    lines = text.splitlines()
    header = lines[0]
    positions = [
        0,
        *(len(_display_cells(header[: header.index(key)])) for key in _MEDICATION_COLUMNS),
    ]
    if positions != sorted(set(positions)):
        return [], True
    result: list[SoapMedication] = []
    failed = False
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = _display_cells(line)
        # Cutting through a token means the layout changed or the name overflowed.
        if len(cells) <= positions[-1] or any(
            0 < pos < len(cells) and not cells[pos - 1].isspace() and not cells[pos].isspace()
            for pos in positions[1:]
        ):
            failed = True
            continue
        values = [
            "".join(cells[start:end]).strip()
            for start, end in zip(positions, [*positions[1:], len(cells)])
        ]
        if not values[0] or not re.fullmatch(r"\d+(?:\.\d+)?", values[-1]):
            failed = True
            continue
        result.append(SoapMedication(*values, raw_text=line))
    return result, failed


def _chronic_periods(
    preamble: str, source_block_index: int
) -> tuple[list[SoapChronicPrescriptionPeriod], bool]:
    periods: list[SoapChronicPrescriptionPeriod] = []
    failed = False
    for line in preamble.splitlines():
        if not line.lstrip().startswith(_CHRONIC_NOTICE):
            continue
        match = _CHRONIC_PERIOD.match(line)
        if match is None:
            failed = True
            continue
        try:
            start = date(*(int(part) for part in re.split(r"[-/]", match["start"])))
            end = date(*(int(part) for part in re.split(r"[-/]", match["end"])))
        except ValueError:
            failed = True
            continue
        if end < start:
            failed = True
            continue
        periods.append(SoapChronicPrescriptionPeriod(start, end, source_block_index, raw_text=line))
    return periods, failed


def _orders(text: str) -> tuple[list[SoapOrder], bool]:
    lines = text.splitlines()
    column_count = lines[0].count("檢查驗項目")
    name_width = len(_display_cells(lines[0][: lines[0].index("數量")]))
    result: list[SoapOrder] = []
    failed = False
    for line in lines[1:]:
        if not line.strip():
            continue
        remaining = line
        row: list[SoapOrder] = []
        while remaining.strip() and len(row) < column_count:
            remaining = remaining.lstrip()
            cells = _display_cells(remaining)
            # The data's column separator differs from the repeated header's.
            # Reuse the first name-column width, then consume a single quantity.
            if len(cells) <= name_width or (
                not cells[name_width - 1].isspace() and not cells[name_width].isspace()
            ):
                break
            printed_name = "".join(cells[:name_width])
            tail = "".join(cells[name_width:])
            match = _QUANTITY.match(tail)
            if not printed_name.strip() or match is None:
                break
            row.append(SoapOrder(printed_name.strip(), match["quantity"], printed_name + match[0]))
            remaining = tail[match.end() :]
        if remaining.strip():
            failed = True
        # A valid left column is useful even when the right column changed.
        result.extend(row)
    return result, failed


def parse_soap(html_text: str, case: VisitCase) -> SoapRecord:
    soup = BeautifulSoup(html_text, "html.parser")
    container = soup.find(id="data")
    if container is None:
        raise ParseError(
            "SOAP response did not contain its data container", code="PRQ_SOAP_CONTAINER_MISSING"
        )
    nodes = container.select(".soap pre")
    if not nodes:
        visible = re.sub(r"\s+", "", container.get_text()).strip("!\uff01。")
        if visible and visible not in _EMPTY_TEXT:
            raise ParseError(
                "SOAP response structure was unrecognized", code="PRQ_SOAP_STRUCTURE_UNRECOGNIZED"
            )
        return SoapRecord(case, ())

    labels = _table_labels(container)
    texts: dict[str, list[str]] = {}
    blocks: list[str] = []
    unclassified: list[str] = []
    present: list[str] = []
    issues: list[str] = []
    diagnoses: list[SoapDiagnosis] = []
    orders: list[SoapOrder] = []
    medications: list[SoapMedication] = []
    chronic_periods: list[SoapChronicPrescriptionPeriod] = []
    for node in nodes:
        text = normalize_multiline_text(node.get_text("", strip=False))
        if text.strip():
            blocks.append(text)
        code = labels.get(id(node))
        inline = _INLINE_LABEL.match(text) if code is None else None
        if inline:
            code = _label(inline[1])
        if code:
            present.append(code)
            texts.setdefault(code, []).append(text[inline.end() :] if inline else text)
            continue
        if not text.strip():
            continue
        stripped = text.strip()
        lines = text.lstrip().splitlines()
        header = lines[0]
        if _DIAGNOSIS_HEADER.match(stripped):
            present.append("DIAGNOSES")
            rows, failed = _diagnoses(text)
            diagnoses.extend(rows)
            if failed:
                issues.append("SOAP_DIAGNOSIS_ROW_UNRECOGNIZED")
        elif (medication_header_index := next(
            (index for index, line in enumerate(lines) if re.match(r"^\s*藥\s*名", line)),
            None,
        )) is not None:
            # Some SOAP printouts put a prescription notice above the column
            # header inside the same <pre>. Keep that notice, then parse the
            # explicitly labelled table; never infer medication from prose.
            preamble = "\n".join(lines[:medication_header_index]).strip()
            if preamble:
                unclassified.append(preamble)
                periods, failed = _chronic_periods(preamble, len(blocks) - 1)
                chronic_periods.extend(periods)
                if failed:
                    issues.append("SOAP_CHRONIC_PRESCRIPTION_PERIOD_UNRECOGNIZED")
            medication_text = "\n".join(lines[medication_header_index:]).lstrip()
            medication_header = medication_text.splitlines()[0]
            present.append("MEDICATIONS")
            if not all(medication_header.count(key) == 1 for key in _MEDICATION_COLUMNS):
                issues.append("SOAP_MEDICATION_HEADER_UNRECOGNIZED")
                unclassified.append(medication_text)
                continue
            prescriptions, failed = _medications(medication_text)
            medications.extend(prescriptions)
            if failed:
                issues.append("SOAP_MEDICATION_ROW_UNRECOGNIZED")
        elif header.startswith("檢查驗項目"):
            present.append("ORDERS")
            if not _ORDER_HEADER.fullmatch(header):
                issues.append("SOAP_ORDER_HEADER_UNRECOGNIZED")
                unclassified.append(text)
                continue
            summaries, failed = _orders(text.lstrip())
            orders.extend(summaries)
            if failed:
                issues.append("SOAP_ORDER_ROW_UNRECOGNIZED")
        else:
            unclassified.append(text)
    if blocks and not present:
        issues.append("SOAP_SECTIONS_UNRECOGNIZED")

    def section(code: str) -> str | None:
        if code not in texts:
            return None
        return "\n\n".join(part.strip() for part in texts[code] if part.strip())

    return SoapRecord(
        case=case,
        blocks=tuple(blocks),
        subjective=section("S"),
        objective=section("O"),
        assessment_plan=section("AP"),
        assessment=section("A"),
        plan=section("P"),
        diagnoses=tuple(diagnoses),
        orders=tuple(orders),
        medications=tuple(medications),
        present_sections=tuple(dict.fromkeys(present)),
        unclassified_blocks=tuple(unclassified),
        parsing_issues=tuple(dict.fromkeys(issues)),
        chronic_prescription_periods=tuple(chronic_periods),
    )
