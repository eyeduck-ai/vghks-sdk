"""Pure Audit unsigned-record HTML parser."""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..models import UnsignedRecord
from .common import direct_rows, normalize_inline_text


def parse_unsigned_records(html_text: str) -> list[UnsignedRecord]:
    soup = BeautifulSoup(html_text, "html.parser")
    records: list[UnsignedRecord] = []
    for table_id in ("dataTbl", "pgnTbl", "pgnTb2"):
        table = soup.find("table", id=table_id)
        if table is None:
            continue
        headers = tuple(
            normalize_inline_text(cell.get_text(" ", strip=True)) for cell in table.find_all("th")
        )
        for row in direct_rows(table):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            values = tuple(normalize_inline_text(cell.get_text(" ", strip=True)) for cell in cells)
            if any(values):
                records.append(UnsignedRecord(category=table_id, headers=headers, values=values))
    return records
