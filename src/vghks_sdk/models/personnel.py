"""Recorded physician-directory filters and data; employee IDs are not stamp numbers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..core.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class PersonnelFilter:
    name: str = ""
    employee_id: str = ""
    title: str = ""
    unit: str = ""
    include_subunits: bool = False
    allow_all: bool = False

    def __post_init__(self) -> None:
        for key, limit in (("name", 10), ("employee_id", 6), ("title", 32), ("unit", 32)):
            value = getattr(self, key)
            if not isinstance(value, str):
                raise ConfigurationError(
                    "invalid personnel filter", code="PERSONNEL_FILTER_INVALID"
                )
            value = unicodedata.normalize("NFKC", value).strip()
            if len(value) > limit or any(ord(c) < 32 for c in value):
                raise ConfigurationError(
                    "invalid personnel filter", code="PERSONNEL_FILTER_INVALID"
                )
            if key != "name" and value and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ConfigurationError("invalid personnel code", code="PERSONNEL_FILTER_INVALID")
            object.__setattr__(self, key, value if key == "name" else value.upper())
        if type(self.include_subunits) is not bool or type(self.allow_all) is not bool:
            raise ConfigurationError(
                "personnel flags require booleans", code="PERSONNEL_FILTER_INVALID"
            )
        if self.include_subunits and not self.unit:
            raise ConfigurationError("subunits require a unit", code="PERSONNEL_UNIT_REQUIRED")
        if not any((self.name, self.employee_id, self.title, self.unit)) and not self.allow_all:
            raise ConfigurationError(
                "at least one personnel filter is required", code="PERSONNEL_FILTER_REQUIRED"
            )

    def to_form(self) -> dict[str, str]:
        """Only the recorded read action; never submit the result page's SMS form."""
        fields = {
            "reqCode": "showAllDoctors",
            "value(source)": "DR",
            "value(name)": self.name,
            "value(usrId)": self.employee_id,
            "value(title)": self.title,
            "value(costId)": self.unit,
            "action": "開始搜尋",
        }
        if self.include_subunits:
            fields["value(subOU)"] = "Y"
        return fields


@dataclass(frozen=True, slots=True)
class PersonnelOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class PersonnelOptions:
    titles: tuple[PersonnelOption, ...]
    units: tuple[PersonnelOption, ...]


@dataclass(frozen=True, slots=True)
class PersonnelRecord:
    employee_id: str
    name: str
    doctor_stamp_no: str
    title: str
    unit: str
    extension: str
    phone_numbers: tuple[str, ...] = field(repr=False)
    fields: Mapping[str, str] = field(repr=False)
    unrendered_fields: tuple[str, ...] = ()


def personnel_employee_id(card_no: str) -> str:
    """Accept an employee account or the observed four-character account + F alias.

    This does not convert medical stamp numbers or arbitrary identifier suffixes.
    """
    value = PersonnelFilter(employee_id=card_no).employee_id
    return value[:-1] if re.fullmatch(r"[A-Z0-9]{4}F", value) else value
