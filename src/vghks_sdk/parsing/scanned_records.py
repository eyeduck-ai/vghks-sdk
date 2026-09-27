"""Extract PRQ scanned-PDF references without running the page's JavaScript."""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup, Tag

from ..core.errors import ConfigurationError, ParseError
from ..core.jsliteral import decode_js_string, split_top_level, strip_js_comments
from ..models import PdfAttachmentRef, ScannedRecord
from .common import direct_rows, normalize_inline_text

_JS_STRING = r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
_FILEPATH = re.compile(
    rf"var\s+filepath\s*=\s*encodeURIComponent\(\s*(?P<value>{_JS_STRING})\s*\)",
    re.DOTALL,
)
_SUBTYPE = re.compile(rf"var\s+subtype\s*=\s*(?P<value>{_JS_STRING})", re.DOTALL)
_FILEPATH_ASSIGNMENT = re.compile(r"\bvar\s+filepath\s*=")
_DISPLAY_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def _scan_assignments(source: str) -> tuple[list[str], list[str]]:
    """Read only top-level literal assignments, ignoring strings and callbacks."""
    cleaned = strip_js_comments(source)
    paths: list[str] = []
    subtypes: list[str] = []
    for statement in split_top_level(cleaned, ";"):
        statement = statement.strip()
        if match := _FILEPATH.fullmatch(statement):
            paths.append(match["value"])
        elif _FILEPATH_ASSIGNMENT.match(statement):
            raise ParseError("scan PDF expression changed", code="SCAN_PATH_UNSUPPORTED")
        elif match := _SUBTYPE.fullmatch(statement):
            subtypes.append(match["value"])
    code_only = re.sub(_JS_STRING, lambda match: " " * len(match.group()), cleaned)
    if len(_FILEPATH_ASSIGNMENT.findall(code_only)) > len(paths):
        raise ParseError("scan PDF assignment was not top-level", code="SCAN_PATH_UNSUPPORTED")
    return paths, subtypes


def _pdf_ref(mrn: str, literal: str) -> PdfAttachmentRef:
    value = decode_js_string(literal)
    if value is None:
        raise ParseError("scan PDF path was not a JavaScript literal", code="SCAN_PATH_UNSUPPORTED")
    try:
        return PdfAttachmentRef(mrn, value)
    except ConfigurationError as exc:
        raise ParseError("scan PDF path was unsafe", code=exc.info.code) from exc


def _table_context(script: Tag) -> tuple[str | None, str | None]:
    row = script.find_parent("tr")
    table = row.find_parent("table") if row else None
    if table is None:
        return None, None
    section_label = None
    for candidate in direct_rows(table):
        headers = candidate.find_all("th", recursive=False)
        if len(headers) < 2:
            continue
        first, second = (normalize_inline_text(cell.get_text(" ", strip=True)) for cell in headers[:2])
        if first.startswith("病歷類別") and second == "病歷日期":
            section_label = first
            break
    if section_label is None:
        return None, None
    cells = row.find_all("td", recursive=False)
    if len(cells) != 2 or script.find_parent("td") is not cells[1]:
        raise ParseError("scan category row changed", code="SCAN_CATEGORY_UNALIGNED")
    category_label = normalize_inline_text(cells[0].get_text(" ", strip=True))
    if not category_label:
        raise ParseError("scan category was missing", code="SCAN_CATEGORY_MISSING")
    return section_label, category_label


def _record_date(source: str) -> date | None:
    """Read the displayed date literal; dates inside PDF paths are not labels."""
    dates: set[str] = set()
    for match in re.finditer(_JS_STRING, strip_js_comments(source)):
        value = decode_js_string(match.group())
        if value is None or "<" not in value or ">" not in value:
            continue
        visible = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
        dates.update(_DISPLAY_DATE.findall(visible))
    if not dates:
        return None
    if len(dates) != 1:
        raise ParseError("scan displayed date was ambiguous", code="SCAN_DATE_AMBIGUOUS")
    try:
        return date.fromisoformat(next(iter(dates)))
    except ValueError as exc:
        raise ParseError("scan displayed date was invalid", code="SCAN_DATE_INVALID") from exc


def parse_case_scanned_pdf_refs(html_text: str, mrn: str) -> tuple[PdfAttachmentRef, ...]:
    """Read the PDF embedded next to one outpatient SOAP record, if present."""
    soup = BeautifulSoup(html_text, "html.parser")
    refs: list[PdfAttachmentRef] = []
    for script in soup.find_all("script"):
        source = script.string or script.get_text()
        if "showPDF.jsp" not in source:
            continue
        paths, _ = _scan_assignments(source)
        for path in paths:
            refs.append(_pdf_ref(mrn, path))
    return tuple(dict.fromkeys(refs))


def parse_history_scanned_records(html_text: str, mrn: str) -> tuple[ScannedRecord, ...]:
    """List every PDF entry with its source subtype, category and shown date."""
    soup = BeautifulSoup(html_text, "html.parser")
    records: list[ScannedRecord] = []
    for script in soup.find_all("script"):
        source = script.string or script.get_text()
        if "showPDF.jsp" not in source:
            continue
        paths, subtypes = _scan_assignments(source)
        if not paths:
            continue
        if len(paths) != 1:
            raise ParseError("scan PDF path was ambiguous", code="SCAN_PATH_UNSUPPORTED")
        if len(subtypes) > 1:
            raise ParseError("scan subtype was ambiguous", code="SCAN_SUBTYPE_INVALID")
        kind = decode_js_string(subtypes[0]) if subtypes else None
        if kind is not None and not re.fullmatch(r"[A-Za-z0-9_]{1,16}", kind.strip()):
            raise ParseError("scan subtype was invalid", code="SCAN_SUBTYPE_INVALID")
        section, category = _table_context(script)
        records.append(
            ScannedRecord(
                kind.strip().upper() if kind is not None else None,
                _pdf_ref(mrn, paths[0]),
                category_label=category,
                record_date=_record_date(source),
                section_label=section,
            )
        )
    return tuple(dict.fromkeys(records))
