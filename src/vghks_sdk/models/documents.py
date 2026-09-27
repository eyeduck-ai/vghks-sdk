"""Typed documents data for the public SDK."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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
class ScannedRecord:
    """One PDF entry with the source subtype and its displayed table context."""

    record_type: str | None
    pdf_ref: PdfAttachmentRef
    category_label: str | None = None
    record_date: date | None = None
    section_label: str | None = None

    @property
    def is_ophthalmology_record(self) -> bool:
        """Match the recorded outpatient eye-record category, not the PDF subtype."""
        if self.section_label not in {None, "病歷類別"} or not self.category_label:
            return False
        parts = tuple(part.strip() for part in self.category_label.split("-"))
        return parts[:3] == ("門診", "記錄", "眼科紀錄")


@dataclass(frozen=True, slots=True)
class UploadHistory:
    mrn: str
    document: HtmlDocument
    pdf_refs: tuple[PdfAttachmentRef, ...]
    scanned_records: tuple[ScannedRecord, ...] = ()

    @property
    def scanned_categories(self) -> tuple[str, ...]:
        """Distinct displayed category labels in source order."""
        return tuple(dict.fromkeys(
            row.category_label for row in self.scanned_records if row.category_label
        ))

    def select_scanned_records(
        self,
        category_label: str | None = None,
        *,
        section_label: str | None = None,
    ) -> tuple[ScannedRecord, ...]:
        """Return all entries or those with an exact displayed category/section."""
        return tuple(
            row for row in self.scanned_records
            if (category_label is None or row.category_label == category_label)
            and (section_label is None or row.section_label == section_label)
        )


@dataclass(frozen=True, slots=True)
class EarningsReportContext:
    kind: str
    generation: int
    form: FormSnapshot
