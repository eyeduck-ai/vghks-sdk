"""Completed surgery queries and references, separate from schedule commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..core.errors import ConfigurationError
from ._validation import _fully_unquote, _require_navigation_values, _validate_navigation_ref

SURGERY_PERIODS = ("3", "7", "14", "1M", "4M", "6M", "12M", "24M", "2YB")


@dataclass(frozen=True, slots=True)
class SurgeryCaseFilter:
    """One recorded server query; 24M plus 2YB cover both history partitions."""

    surgeon_card: str = ""
    supervising_card: str = ""
    assistant_cards: tuple[str, ...] = ()
    department: str = "ALL"
    procedure_code: str = ""
    period: str = "24M"

    def __post_init__(self) -> None:
        for name in ("surgeon_card", "supervising_card", "department", "procedure_code", "period"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ConfigurationError(
                    "surgery filters must be strings", code="SURGERY_FILTER_INVALID"
                )
            value = value.strip().upper()
            if value and not re.fullmatch(r"[A-Z0-9_.-]{1,40}", value):
                raise ConfigurationError("invalid surgery filter", code="SURGERY_FILTER_INVALID")
            object.__setattr__(self, name, value)
        if not isinstance(self.assistant_cards, (tuple, list)) or len(self.assistant_cards) > 4:
            raise ConfigurationError(
                "up to four assistant positions are supported", code="SURGERY_FILTER_INVALID"
            )
        assistants = []
        for value in self.assistant_cards:
            if not isinstance(value, str) or (
                value.strip() and not re.fullmatch(r"[A-Za-z0-9-]{1,32}", value.strip())
            ):
                raise ConfigurationError("invalid assistant card", code="SURGERY_FILTER_INVALID")
            assistants.append(value.strip().upper())
        object.__setattr__(self, "assistant_cards", tuple(assistants))
        if not any((self.surgeon_card, self.supervising_card, *assistants)):
            raise ConfigurationError(
                "at least one physician card is required", code="SURGERY_DOCTOR_REQUIRED"
            )
        if self.period not in SURGERY_PERIODS:
            raise ConfigurationError(
                "unknown recorded surgery period", code="SURGERY_PERIOD_INVALID"
            )
        if not self.department:
            object.__setattr__(self, "department", "ALL")

    def to_form(self) -> dict[str, str]:
        assistants = (*self.assistant_cards, "", "", "", "")
        return {
            "doctVId": self.surgeon_card,
            "guiDoctId": self.supervising_card,
            "opDept": self.department,
            "opdate": self.period,
            "opCode": self.procedure_code,
            **{f"assDoct{i + 1}": assistants[i] for i in range(4)},
        }


@dataclass(frozen=True, slots=True)
class SurgeryCaseRef:
    mrn: str
    request_no: str
    sequence_no: str

    def __post_init__(self) -> None:
        _validate_navigation_ref(self.mrn, self.request_no)
        _require_navigation_values(self.request_no)
        if isinstance(self.sequence_no, bool) or not re.fullmatch(
            r"\d{1,10}", str(self.sequence_no)
        ):
            raise ConfigurationError("invalid surgery sequence", code="SURGERY_REF_INVALID")
        object.__setattr__(self, "sequence_no", str(self.sequence_no))


@dataclass(frozen=True, slots=True)
class SurgeryCase:
    reference: SurgeryCaseRef
    surgery_date: date
    procedure: str
    procedure_codes: tuple[str, ...] = ()
    patient_name: str = ""
    display_name: str = ""
    case_type: str = ""
    case_no: str = ""
    department: str = ""
    surgeon_card: str = ""
    surgeon_name: str = ""
    supervising_card: str = ""
    supervising_name: str = ""
    attending_card: str = ""
    attending_name: str = ""
    assistant_cards: tuple[str, ...] = ()
    assistant_names: tuple[str, ...] = ()
    record_status: str = ""
    fields: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class SurgeryNoteRef:
    reference: SurgeryCaseRef
    file_path: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reference, SurgeryCaseRef) or not isinstance(self.file_path, str):
            raise ConfigurationError(
                "invalid surgery note reference", code="SURGERY_NOTE_REF_INVALID"
            )
        path = _fully_unquote(self.file_path)
        parts = tuple(p for p in re.split(r"[/\\]+", path) if p)
        if (
            not re.match(r"^(?:\\\\|//)hfs01_1a0(?:\.vghks\.gov\.tw)?[/\\]+OPG[/\\]+", path, re.I)
            or any(c in path for c in "\x00\r\n?#:")
            or any(p in {".", ".."} for p in parts)
            or not parts
            or not parts[-1].lower().endswith(".pdf")
        ):
            raise ConfigurationError("unknown surgery note path", code="SURGERY_NOTE_PATH_INVALID")
        if self.reference.mrn not in parts or self.reference.request_no not in parts:
            raise ConfigurationError(
                "surgery note identity did not match", code="SURGERY_NOTE_IDENTITY_MISMATCH"
            )
        object.__setattr__(self, "file_path", path)
