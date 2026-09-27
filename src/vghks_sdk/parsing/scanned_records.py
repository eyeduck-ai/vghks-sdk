"""Extract PRQ scanned-PDF references without running the page's JavaScript."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..core.errors import ConfigurationError, ParseError
from ..core.jsliteral import decode_js_string, split_top_level, strip_js_comments
from ..models import PdfAttachmentRef, ScannedRecord

_JS_STRING = r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
_FILEPATH = re.compile(
    rf"var\s+filepath\s*=\s*encodeURIComponent\(\s*(?P<value>{_JS_STRING})\s*\)",
    re.DOTALL,
)
_SUBTYPE = re.compile(rf"var\s+subtype\s*=\s*(?P<value>{_JS_STRING})", re.DOTALL)
_FILEPATH_ASSIGNMENT = re.compile(r"\bvar\s+filepath\s*=")


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
    """Preserve RECORD and OPG source types from the historical scan list."""
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
        if len(subtypes) != 1:
            raise ParseError("scan subtype was missing", code="SCAN_SUBTYPE_INVALID")
        kind = decode_js_string(subtypes[0])
        if kind is None or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", kind.strip()):
            raise ParseError("scan subtype was invalid", code="SCAN_SUBTYPE_INVALID")
        records.append(ScannedRecord(kind.strip().upper(), _pdf_ref(mrn, paths[0])))
    return tuple(dict.fromkeys(records))
