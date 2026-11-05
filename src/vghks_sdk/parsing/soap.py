"""Parse PRQ's labelled SOAP and printed summaries without interpreting medicine."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
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
from .scanned_records import parse_case_scanned_pdf_refs

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
_MEDICATION_HEADER = re.compile(r"^\s*藥\s*名")
_ORDER_HEADER = re.compile(r"^\s*檢查驗項目\s+數量(?:\s+檢查驗項目\s+數量)*\s*$")
_QUANTITY = re.compile(r"\s*(?P<quantity>\d+(?:\.\d+)?)(?=\s|$)")
_EMPTY_TEXT = {"查無資料", "查無SOAP資料", "無SOAP資料"}
_CHRONIC_NOTICE = "慢性病連續處方箋處方"
_CHRONIC_PERIOD = re.compile(
    r"^[\s◆]*服藥期限\s*:\s*"
    r"(?P<start>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*[\u223c\uff5e~至]\s*"
    r"(?P<end>\d{4}[-/]\d{1,2}[-/]\d{1,2})(?=\s|◆|[(),;。]|健保(?:卡)?號碼|$)"
)
_PRINTED_MONTHS = re.compile(r"(?<!\w)(?:處方(?:期間|月數)\s*:\s*|共\s*)([1-9]\d?)\s*(?:個)?月")
_PRINTED_PERIODS = re.compile(r"(?<!\w)(?:(?:處方)?期數\s*:\s*|(?:共|分)\s*)([1-9]\d?)\s*期")
_PRINTED_DISPENSES = re.compile(r"(?<!\w)(?:領藥次數\s*:\s*|(?:共|分)\s*)([1-9]\d?)\s*次")
_PRINTED_COUNT_PATTERNS = (_PRINTED_MONTHS, _PRINTED_PERIODS, _PRINTED_DISPENSES)


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


def _chronic_period(text: str, block_index: int) -> SoapChronicPrescriptionPeriod | None:
    normalized = unicodedata.normalize("NFKC", text).removeprefix(_CHRONIC_NOTICE)
    match = _CHRONIC_PERIOD.match(normalized)
    if match is None:
        return None
    try:
        start = date(*(int(part) for part in re.split(r"[-/]", match["start"])))
        end = date(*(int(part) for part in re.split(r"[-/]", match["end"])))
    except ValueError:
        return None
    if end < start:
        return None

    def printed(pattern: re.Pattern[str]) -> int | None:
        counts = {int(count) for count in pattern.findall(normalized[match.end() :])}
        return counts.pop() if len(counts) == 1 else None

    return SoapChronicPrescriptionPeriod(
        start, end, block_index, raw_text=text, printed_months=printed(_PRINTED_MONTHS),
        printed_period_count=printed(_PRINTED_PERIODS),
        printed_dispense_count=printed(_PRINTED_DISPENSES),
    )


class _MedicationParser:
    def __init__(self, unclassified: list[str], issues: list[str]):
        self.unclassified, self.issues = unclassified, issues
        self.rows: list[SoapMedication] = []
        self.periods: list[SoapChronicPrescriptionPeriod] = []
        self.incomplete: set[int] = set()
        self.pending: tuple[str, int | None] | None = None

    def block(self, text: str, block_index: int, *, allow_preamble: bool = False) -> bool | None:
        """Return table presence, or None if the block was not a medication summary."""
        lines = text.lstrip().splitlines()
        starts = [i for i, line in enumerate(lines) if _MEDICATION_HEADER.match(line)
                  or line.lstrip().startswith(_CHRONIC_NOTICE)]
        pending, self.pending = self.pending, None
        if not starts or (starts[0] and not allow_preamble):
            return None
        if starts[0]:
            self.unclassified.append("\n".join(lines[: starts[0]]).strip())
        kind, period_index = pending if pending is not None and not starts[0] else ("regular", None)
        has_table = False
        for start, stop in zip(starts, [*starts[1:], len(lines)]):
            if lines[start].lstrip().startswith(_CHRONIC_NOTICE):
                notice = "\n".join(lines[start:stop]).strip()
                self.unclassified.append(notice)
                period = _chronic_period(notice, block_index)
                kind, period_index = "chronic", None
                if period is None:
                    self.issues.append("SOAP_CHRONIC_PRESCRIPTION_PERIOD_UNRECOGNIZED")
                else:
                    period_index = len(self.periods)
                    self.periods.append(period)
                # A notice in its own <pre> applies only to the next adjacent table.
                if stop == len(lines):
                    self.pending = (kind, period_index)
                continue

            has_table = True
            table = "\n".join(lines[start:stop]).lstrip()
            header = table.splitlines()[0]
            if all(header.count(key) == 1 for key in _MEDICATION_COLUMNS):
                rows, failed = _medications(table)
                self.rows.extend(replace(row, prescription_type=kind, chronic_period_index=period_index)
                                 for row in rows)
                if failed:
                    self.issues.append("SOAP_MEDICATION_ROW_UNRECOGNIZED")
            else:
                failed = True
                self.unclassified.append(table)
                self.issues.append("SOAP_MEDICATION_HEADER_UNRECOGNIZED")
            if failed and period_index is not None:
                self.incomplete.add(period_index)
        return has_table

    def with_estimates(self) -> tuple[SoapChronicPrescriptionPeriod, ...]:
        day_values: dict[int, set[str]] = {}
        for row in self.rows:
            if row.chronic_period_index is not None:
                day_values.setdefault(row.chronic_period_index, set()).add(
                    unicodedata.normalize("NFKC", row.days)
                )
        periods = []
        for index, period in enumerate(self.periods):
            days = day_values.get(index, set())
            has_printed_count = any(pattern.search(unicodedata.normalize("NFKC", period.raw_text))
                                    for pattern in _PRINTED_COUNT_PATTERNS)
            if index not in self.incomplete and len(days) == 1 and not has_printed_count:
                value = next(iter(days))
                if re.fullmatch(r"[0-9]{2}", value) and 28 <= (cycle_days := int(value)) <= 31:
                    cycles, remainder = divmod((period.end_date - period.start_date).days, cycle_days)
                    if cycles in {2, 3} and remainder == 0:
                        period = replace(period, estimated_cycles=cycles, estimated_cycle_days=cycle_days)
            periods.append(period)
        return tuple(periods)


def parse_soap_medications(blocks: Iterable[str]) -> tuple[
    tuple[SoapMedication, ...], tuple[SoapChronicPrescriptionPeriod, ...], tuple[str, ...]
]:
    """Reparse saved SOAP text without requests; only use blocks with explicit leading headings.

    The original HTML section labels are unavailable here. Use parse_soap for HTML
    and retain prior row order if projecting metadata onto an existing snapshot.
    """
    issues: list[str] = []
    parser = _MedicationParser([], issues)
    for index, text in enumerate(blocks):
        if text.strip():
            parser.block(text, index)
    return tuple(parser.rows), parser.with_estimates(), tuple(dict.fromkeys(issues))


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
    scanned_pdf_refs = parse_case_scanned_pdf_refs(html_text, case.mrn)
    nodes = container.select(".soap pre")
    if not nodes:
        visible = re.sub(r"\s+", "", container.get_text()).strip("!\uff01。")
        if visible and visible not in _EMPTY_TEXT:
            raise ParseError(
                "SOAP response structure was unrecognized", code="PRQ_SOAP_STRUCTURE_UNRECOGNIZED"
            )
        return SoapRecord(case, (), scanned_pdf_refs=scanned_pdf_refs)

    labels = _table_labels(container)
    texts: dict[str, list[str]] = {}
    blocks: list[str] = []
    unclassified: list[str] = []
    present: list[str] = []
    issues: list[str] = []
    diagnoses: list[SoapDiagnosis] = []
    orders: list[SoapOrder] = []
    medication_parser = _MedicationParser(unclassified, issues)
    for node in nodes:
        text = normalize_multiline_text(node.get_text("", strip=False))
        if text.strip():
            blocks.append(text)
        code = labels.get(id(node))
        inline = _INLINE_LABEL.match(text) if code is None else None
        if inline:
            code = _label(inline[1])
        if code:
            medication_parser.pending = None
            present.append(code)
            texts.setdefault(code, []).append(text[inline.end() :] if inline else text)
            continue
        if not text.strip():
            continue
        stripped = text.strip()
        lines = text.lstrip().splitlines()
        header = lines[0]
        if _DIAGNOSIS_HEADER.match(stripped):
            medication_parser.pending = None
            present.append("DIAGNOSES")
            rows, failed = _diagnoses(text)
            diagnoses.extend(rows)
            if failed:
                issues.append("SOAP_DIAGNOSIS_ROW_UNRECOGNIZED")
        elif header.startswith("檢查驗項目"):
            medication_parser.pending = None
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
            has_table = medication_parser.block(text, len(blocks) - 1, allow_preamble=True)
            if has_table:
                present.append("MEDICATIONS")
            elif has_table is None:
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
        medications=tuple(medication_parser.rows),
        present_sections=tuple(dict.fromkeys(present)),
        unclassified_blocks=tuple(unclassified),
        parsing_issues=tuple(dict.fromkeys(issues)),
        chronic_prescription_periods=medication_parser.with_estimates(),
        scanned_pdf_refs=scanned_pdf_refs,
    )
