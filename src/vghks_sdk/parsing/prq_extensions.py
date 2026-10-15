"""Pure PRQ patient flags, report histories and uploaded-record parsers."""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs

from ..core.errors import ParseError
from ..models import OrderReportRef, PdfAttachmentRef, TextReportHistory, UploadHistory
from .clinical import _pdf_candidates
from .documents import parse_document
from .scanned_records import parse_history_scanned_records


def parse_patient_flags(key: str, value: Any, mrn: str) -> dict[str, Any]:
    expected = {
        "allergy": ("hhisnum", "allergy", "allergyMsg"),
        "advance_directives": ("hhisnum", "adSign", "adSignMsg"),
        "research_flags": ("hhisnum", "irbtype"),
        "bed_transfers": ("hhisnum", "frontBed", "nextBed"),
        "care_cases": ("status", "caseList"),
    }[key.removeprefix("prq.")]
    if not isinstance(value, dict) or not all(name in value for name in expected):
        raise ParseError("patient query JSON schema changed", code="PATIENT_JSON_SHAPE_INVALID")
    if "hhisnum" in value and str(value["hhisnum"]) != mrn:
        raise ParseError("patient query returned another context", code="PATIENT_CONTEXT_MISMATCH")
    return value


def parse_text_history(text: str, mrn: str, department: str) -> TextReportHistory:
    refs = []
    for match in re.finditer(r"QueryResText\.do\?([^\s'\"<>]+)", unescape(text)):
        query = parse_qs(match.group(1))
        # The page includes a comment documenting QueryResText.do?source=...
        # It is not a report link and carries no record identifiers.
        if not {"caseNo", "caseType", "seqNo"}.issubset(query):
            continue

        def item(key: str, query=query) -> str:
            return query.get(key, [""])[0]

        if item("hhisnum") and item("hhisnum") != mrn:
            raise ParseError(
                "report link belongs to another patient", code="PATIENT_CONTEXT_MISMATCH"
            )
        refs.append(
            OrderReportRef(
                mrn,
                item("caseNo"),
                item("caseType"),
                item("seqNo"),
                department=item("orDept"),
                source=item("source"),
            )
        )
    if not refs and "height_bd" not in text:
        raise ParseError("text history structure missing", code="TEXT_HISTORY_STRUCTURE_MISSING")
    return TextReportHistory(mrn, department, parse_document(text), tuple(dict.fromkeys(refs)))


def parse_upload_history(text: str, mrn: str) -> UploadHistory:
    from bs4 import BeautifulSoup

    if BeautifulSoup(text, "html.parser").find(id="tbObj") is None:
        raise ParseError(
            "upload history structure missing", code="UPLOAD_HISTORY_STRUCTURE_MISSING"
        )
    refs = tuple(dict.fromkeys(PdfAttachmentRef(mrn, path) for path in _pdf_candidates(text)))
    return UploadHistory(
        mrn,
        parse_document(text),
        refs,
        parse_history_scanned_records(text, mrn),
    )


def parse_upload_types(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or any(
        not isinstance(row, dict) or not {"maintp", "mainnm"}.issubset(row) for row in value
    ):
        raise ParseError("upload type schema changed", code="UPLOAD_TYPES_INVALID")
    return value
