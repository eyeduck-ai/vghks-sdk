"""Extract report text from known content cells, independently of attachments."""

from __future__ import annotations

import re
from itertools import pairwise

from bs4 import BeautifulSoup, Tag

from ..core.errors import ParseError
from ..core.jsliteral import (
    evaluate_expression,
    split_top_level,
    static_document_writes,
    strip_js_comments,
)

_BODY_LABELS = {"report", "report content", "result", "results", "findings", "impression"}
_POINTERS = {
    "詳見附件",
    "請參閱附件",
    "報告附件",
    "詳見報告附件",
    "附件",
    "詳見pdf",
    "seeattachment",
    "seeattachedreport",
    "seepdf",
}


def extract_report_text(soup: BeautifulSoup) -> tuple[str, tuple[str, ...]]:
    """Do not mistake patient/order metadata or viewer controls for results.

    Original fields and HTTP responses remain available separately. Unknown
    scripts are never executed; notes retain the limit of text extraction.
    """
    bodies: list[Tag] = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        for label, body in pairwise(cells):
            if body.name != "td":
                continue
            title = " ".join(label.get_text(" ", strip=True).split()).rstrip(":\uff1a").casefold()
            if title.startswith("報告內容") or title in _BODY_LABELS:
                bodies.append(body)
    if not bodies:
        container = soup.find(id="rptDiv")
        if container is not None:
            bodies.extend(container.find_all("pre"))
    # Lab results use a separate table or a short result block, without a
    # "report content" label. Exclude the surrounding Title metadata table.
    data = soup.find(id="data")
    if data is not None:
        for table in data.select("table.labtable"):
            headers = [re.sub(r"\s+", "", cell.get_text()) for cell in table.find_all("th")]
            if headers == ["檢驗項目", "檢驗結果", "單位", "參考區間"] and table.find("td"):
                bodies.append(table)
        bodies.extend(data.select("div.cmbSty > pre"))
    sections: list[str] = []
    notes: list[str] = []
    for body in bodies:
        fragment = BeautifulSoup(str(body), "html.parser")
        for script in list(fragment.find_all("script")):
            if not script.get("src"):
                try:
                    source = script.string or script.get_text()
                    markup = static_document_writes(source, _viewer_variables(source))
                    script.insert_before(BeautifulSoup(markup, "html.parser"))
                except ParseError as exc:
                    notes.append(exc.info.code)
            script.decompose()
        for control in fragment.find_all(
            ["style", "a", "button", "input", "select", "iframe", "embed", "object", "img"]
        ):
            control.decompose()
        for control in fragment.select("[reqno]"):
            control.decompose()
        text = "\n".join(
            line.strip()
            for line in fragment.get_text("\n", strip=True).splitlines()
            if line.strip()
        )
        normalized = re.sub(r"[\s。.!\uff01:\uff1a]", "", text).casefold()
        if text and normalized not in _POINTERS:
            sections.append(text)
    return "\n\n".join(dict.fromkeys(sections)), tuple(dict.fromkeys(notes))


def _viewer_variables(source: str) -> dict[str, str]:
    """Resolve the recorded PDF-button variable from one top-level assignment.

    Only encodeURIComponent of static content is supported. Multiple writes to
    the variable, callbacks, unknown branches and dynamic expressions fail closed.
    """

    assignments = [
        statement for statement in split_top_level(strip_js_comments(source), ";")
        if re.search(r"\burlStr\s*(?:\+=|=(?!=))", statement)
    ]
    if not assignments:
        return {}
    match = re.fullmatch(
        r"(?:var\s+)?urlStr\s*=\s*(encodeURIComponent\s*\(.*\))",
        assignments[0], re.DOTALL,
    ) if len(assignments) == 1 else None
    value = evaluate_expression(match[1], {}) if match else None
    if value is None:
        raise ParseError(
            "report viewer variable was not in the static allow-list",
            code="JS_DOCUMENT_WRITE_UNSUPPORTED",
        )
    return {"urlStr": value}
