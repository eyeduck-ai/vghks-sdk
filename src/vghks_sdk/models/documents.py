"""Typed documents data for the public SDK."""

from __future__ import annotations

from dataclasses import dataclass

from .assets import PdfAttachmentRef
from .orders import OrderReportRef


@dataclass(frozen=True, slots=True)
class HtmlTable:
    identifier: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class HtmlDocument:
    """Keep column order and raw HTML without guessing clinical fields."""

    tables: tuple[HtmlTable, ...]
    text: str
    html: str
    rendering_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FormSnapshot:
    """Server form only; JavaScript-derived clinical selections need explicit input."""

    identifier: str
    fields: tuple[tuple[str, str], ...]
    choices: dict[str, tuple[tuple[str, str], ...]]
    html: str


@dataclass(frozen=True, slots=True)
class TextReportHistory:
    mrn: str
    department: str
    document: HtmlDocument
    report_refs: tuple[OrderReportRef, ...]


@dataclass(frozen=True, slots=True)
class UploadHistory:
    mrn: str
    document: HtmlDocument
    pdf_refs: tuple[PdfAttachmentRef, ...]


@dataclass(frozen=True, slots=True)
class EarningsReportContext:
    kind: str
    generation: int
    form: FormSnapshot
