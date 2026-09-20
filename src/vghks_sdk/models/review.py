"""Read-only preauthorization cases; submission and review outcomes are distinct."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..core.errors import ConfigurationError

REVIEW_DECISIONS = {
    "0": "審查中",
    "1": "同意備查",
    "2": "不予同意",
    "3": "部分同意",
    "4": "補件",
    "5": "退件",
    "7": "改核",
}


@dataclass(frozen=True, slots=True)
class ReviewCaseRef:
    apply_seq: str

    def __post_init__(self) -> None:
        if not isinstance(self.apply_seq, str) or not re.fullmatch(r"[0-9]{1,32}", self.apply_seq):
            raise ConfigurationError(
                "invalid review application reference", code="REVIEW_REF_INVALID"
            )


@dataclass(frozen=True, slots=True)
class ReviewCaseFilter:
    doctor_card: str = ""
    department: str = ""
    mrn: str = ""
    verify_code: str = ""
    apply_mode: str = ""
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        for name in ("doctor_card", "department", "mrn", "verify_code", "apply_mode"):
            value = getattr(self, name)
            if not isinstance(value, str) or (
                value.strip() and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value.strip())
            ):
                raise ConfigurationError("invalid review filter", code="REVIEW_FILTER_INVALID")
            object.__setattr__(self, name, value.strip().upper())
        if any(
            value is not None and type(value) is not date
            for value in (self.start_date, self.end_date)
        ):
            raise ConfigurationError("review dates require date objects")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ConfigurationError("review date range is reversed")
        if not self.to_form():
            raise ConfigurationError(
                "at least one review filter is required", code="REVIEW_FILTER_REQUIRED"
            )

    def to_form(self) -> dict[str, str]:
        values = {
            "VSDrID": self.doctor_card,
            "InsuSectNo": self.department,
            "PatNo": self.mrn,
            "VerifyCode": self.verify_code,
            "ApplyMode": self.apply_mode,
            "ApplyDateS": self.start_date.strftime("%Y%m%d") if self.start_date else "",
            "ApplyDateE": self.end_date.strftime("%Y%m%d") if self.end_date else "",
        }
        return {k: v for k, v in values.items() if v}


@dataclass(frozen=True, slots=True)
class ReviewCase:
    reference: ReviewCaseRef
    application_date: str
    mrn: str
    patient_name: str
    doctor_card: str
    department: str
    verify_code: str
    application_status: str
    processing_status: str
    fields: Mapping[str, Any] = field(repr=False)
    review_label: str = field(init=False)
    approved: bool | None = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "review_label", REVIEW_DECISIONS.get(self.verify_code, "未辨識"))
        object.__setattr__(self, "approved", {"1": True, "2": False}.get(self.verify_code))


@dataclass(frozen=True, slots=True)
class ReviewCasePart:
    """One independently fetched case grid; rows preserve all returned fields."""

    reference: ReviewCaseRef
    kind: str
    rows: tuple[Mapping[str, Any], ...] = field(repr=False)
    total: int


@dataclass(frozen=True, slots=True)
class ReviewLoginInfo:
    authentication_mode: str
    fields: Mapping[str, Any] = field(repr=False)
