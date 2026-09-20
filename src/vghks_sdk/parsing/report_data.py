"""Extract report text from known content cells, independently of attachments."""

from __future__ import annotations

import re
from itertools import pairwise

from bs4 import BeautifulSoup, Tag

from ..core.errors import ParseError
from ..core.jsliteral import static_document_writes

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
                    markup = static_document_writes(script.string or script.get_text())
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
