"""Typed surgery data for the public SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ._validation import _history_lookback
from .assets import PdfAttachmentRef


@dataclass(frozen=True, slots=True)
class SurgeryHistoryFilter:
    lookback_days: int | str = "all"

    def __post_init__(self) -> None:
        object.__setattr__(self, "lookback_days", _history_lookback(self.lookback_days, 20000))


@dataclass(frozen=True, slots=True)
class PatientSurgeryRecord:
    mrn: str
    surgery_date: str = ""
    procedure: str = ""
    surgery_record_available: bool = False
    anesthesia_record_available: bool = False
    anesthesia_consent_available: bool = False
    preoperative_record_available: bool = False
    postoperative_record_available: bool = False
    gamma_knife_record_available: bool = False
    extra: Mapping[str, Any] | None = None
    surgery_record_refs: tuple[PdfAttachmentRef, ...] = ()
    surgery_record_issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SurgeryRecord:
    patient_mrn: str = ""
    case_no: str = ""
    surgery_date: str = ""
    start_time: str = ""
    end_time: str = ""
    room: str = ""
    doctor_card: str = ""
    doctor_name: str = ""
    procedure: str = ""
    status: str = ""
    extra: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SurgeryCommand:
    """Reviewable immutable payload; preparing this object sends no requests."""

    operation: str
    fields: tuple[tuple[str, str], ...]
    command_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    operation: str
    command_id: str
    status: str
    response: Any
