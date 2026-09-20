"""Typed patients data for the public SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class PatientDemographics:
    mrn: str
    name: str
    mobile_phone: str = ""
    home_phone: str = ""
    birthday: str = ""
    sex: str = ""
    address: str = ""
    extra: Mapping[str, Any] | None = None

    @property
    def preferred_phone(self) -> str:
        return self.mobile_phone.strip() or self.home_phone.strip()


@dataclass(frozen=True, slots=True)
class RegistrationRecord:
    """One registration, with source columns retained for unknown future fields.

    A registration does not establish that the patient actually attended.
    Empty status/cancellation fields remain unknown rather than inferred.
    """

    columns: Mapping[str, str]
    mrn: str = ""
    name: str = ""
    visit_date: date | None = None
    section_code: str = ""
    section_name: str = ""
    room: str = ""
    sequence_no: str = ""
    expected_time: str = ""
    status: str = ""
    registered_at: str = ""
    registered_by: str = ""
    cancelled_at: str = ""
    cancelled_by: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class PatientBasicInfo:
    """QUY15 detail page, including the encounter context shown by the server.

    The recorded selector is A (inpatient). Ward/admission fields can refer
    to a discharged encounter; notices and raw fields preserve that context.
    Values with no recorded unit remain strings rather than guessed numbers.
    """

    mrn: str
    name: str
    national_id: str = ""
    birthday: date | None = None
    sex: str = ""
    age: str = ""
    blood_type: str = ""
    height: str = ""
    weight: str = ""
    nationality: str = ""
    phone: str = ""
    address: str = ""
    contact: str = ""
    ward_bed: str = ""
    section: str = ""
    insurance: str = ""
    admitted_at: str = ""
    discharge_notified_at: str = ""
    discharged_at: str = ""
    case_no: str = ""
    admission_diagnosis: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    query_type: str = "A"
    raw_html: str = field(default="", repr=False)
