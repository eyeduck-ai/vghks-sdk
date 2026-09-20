"""Shared text and table helpers for pure response parsers."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..models import VisitCase


def normalize_inline_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_multiline_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def strip_markup(value: str) -> str:
    if "<" not in value and "&" not in value:
        return normalize_inline_text(value)
    return normalize_inline_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def direct_rows(table: Tag) -> list[Tag]:
    return [row for row in table.find_all("tr") if row.find_parent("table") is table]


def unique_headers(headers: list[str]) -> list[str]:
    result: list[str] = []
    counts: dict[str, int] = {}
    for index, header in enumerate(headers, start=1):
        base = header or f"column_{index}"
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return result


def map_columns(headers: list[str], values: list[str]) -> Mapping[str, str]:
    if len(headers) != len(values):
        return {f"column_{index}": value for index, value in enumerate(values, start=1)}
    return dict(zip(headers, values))


def sort_visit_cases(cases: Iterable[VisitCase]) -> list[VisitCase]:
    return sorted(
        cases,
        key=lambda item: (
            item.visit_date or date.min,
            -(item.index if item.index is not None else 10**9),
        ),
        reverse=True,
    )
