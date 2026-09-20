"""Pure form/document extraction. Never execute JavaScript from a response."""

from __future__ import annotations

from copy import copy

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from ..core.jsliteral import static_document_writes
from ..models import FormSnapshot, HtmlDocument, HtmlTable
from .common import direct_rows


def parse_document(html: str, *, require_table: bool = False) -> HtmlDocument:
    soup = BeautifulSoup(html, "html.parser")
    notes: list[str] = []
    for script in list(soup.find_all("script")):
        if not script.get("src"):
            try:
                markup = static_document_writes(
                    script.string or script.get_text(), allow_unknown_branches=True
                )
                script.insert_before(BeautifulSoup(markup, "html.parser"))
            except ParseError as exc:
                # Retain source and explicitly mark partial UI text extraction.
                notes.append(exc.info.code)
        script.decompose()
    tables = []
    for table in soup.find_all("table"):
        nested = table.find("table") is not None
        rows = []
        for row in direct_rows(table):
            cells = []
            for cell in row.find_all(["th", "td"], recursive=False):
                own = copy(cell)
                for child in own.find_all("table"):
                    child.decompose()
                cells.append(own.get_text(" ", strip=True))
            # Layout wrappers have no own data. A report's main table may
            # contain nested navigation tables and must still be retained.
            if not nested or any(cells):
                rows.append(tuple(cells))
        if rows or not nested:
            tables.append(HtmlTable(str(table.get("id", "")), tuple(rows)))
    if require_table and not tables:
        raise ParseError("report did not contain tables", code="REPORT_TABLES_MISSING")
    return HtmlDocument(tuple(tables), soup.get_text(" ", strip=True), html, tuple(dict.fromkeys(notes)))


def parse_form(html: str, identifier: str = "") -> FormSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id=identifier) if identifier else soup.find("form")
    if form is None:
        raise ParseError("expected form missing", code="FORM_MISSING")
    fields: list[tuple[str, str]] = []
    choices: dict[str, tuple[tuple[str, str], ...]] = {}
    for node in form.find_all(["input", "select", "textarea"]):
        name = str(node.get("name", ""))
        kind = str(node.get("type", "text")).lower()
        if (
            not name
            or node.has_attr("disabled")
            or kind in {"button", "submit", "reset", "file", "image"}
        ):
            continue
        if node.name == "select":
            options = [x for x in node.find_all("option") if not x.has_attr("disabled")]
            choices[name] = tuple(
                (str(x.get("value", x.get_text())), x.get_text(strip=True)) for x in options
            )
            selected = [x for x in options if x.has_attr("selected")]
            if not selected and not node.has_attr("multiple"):
                selected = options[:1]
            fields.extend((name, str(x.get("value", x.get_text()))) for x in selected)
        elif kind not in {"checkbox", "radio"} or node.has_attr("checked"):
            value = (
                node.get_text()
                if node.name == "textarea"
                else str(node.get("value", "on" if kind in {"checkbox", "radio"} else ""))
            )
            fields.append((name, value))
    return FormSnapshot(str(form.get("id", "")), tuple(fields), choices, html)
