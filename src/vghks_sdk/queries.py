"""Discoverable, composable query units over the existing typed Services.

A unit owns one result, not one HTTP request: adapters retain responsibility
for session setup, patient/case context and the required UI preflights.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .core.errors import ConfigurationError
from .core.operations import operation_spec


@dataclass(frozen=True, slots=True)
class QuerySpec:
    key: str
    service: str
    method: str
    scope: str
    inputs: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    alternative_inputs: tuple[tuple[str, ...], ...] = ()

    @property
    def app(self) -> str:
        return operation_spec(self.key).app

    @property
    def sdk_method(self) -> str:
        return f"{self.service}.{self.method}"


QUERY_SPECS = (
    QuerySpec("webmaas.demographics", "patients", "get_demographics", "patient", ("mrn",)),
    QuerySpec("webmaas.basic_info", "patients", "get_basic_info", "patient", ("mrn",)),
    QuerySpec(
        "webmaas.registration_query", "patients", "get_registration_history", "patient", ("mrn",)
    ),
    QuerySpec(
        "prq.visit_cases",
        "records",
        "get_visit_cases",
        "patient",
        ("mrn",),
        alternative_inputs=(("national_id",),),
    ),
    QuerySpec(
        "prq.case_detail", "records", "get_case_detail", "case", ("case",), ("prq.visit_cases",)
    ),
    QuerySpec("prq.soap", "records", "get_soap", "case", ("case",), ("prq.visit_cases",)),
    QuerySpec(
        "prq.numeric", "records", "get_numeric_report", "case", ("case",), ("prq.visit_cases",)
    ),
    QuerySpec(
        "prq.case_orders", "orders", "get_case_orders", "case", ("case",), ("prq.visit_cases",)
    ),
    QuerySpec(
        "prq.case_medications",
        "medications",
        "get_case_medications",
        "case",
        ("case",),
        ("prq.visit_cases",),
    ),
    QuerySpec("prq.consults", "records", "get_consults", "case", ("case",), ("prq.visit_cases",)),
    QuerySpec(
        "prq.treatments", "records", "get_treatments", "case", ("case",), ("prq.visit_cases",)
    ),
    QuerySpec("prq.order_history", "orders", "get_order_history", "history", ("mrn", "filter")),
    QuerySpec(
        "prq.medication_history",
        "medications",
        "get_medication_history",
        "history",
        ("mrn", "filter"),
    ),
    QuerySpec(
        "prq.numeric_history", "records", "get_numeric_history", "history", ("mrn", "filter")
    ),
    QuerySpec(
        "prq.surgery_history", "records", "get_surgery_history", "history", ("mrn", "filter")
    ),
    QuerySpec(
        "prq.order_detail", "orders", "get_order_detail", "detail", ("ref",), ("prq.order_history",)
    ),
    QuerySpec(
        "prq.order_report",
        "orders",
        "get_order_report",
        "report",
        ("ref",),
        ("prq.order_history", "prq.order_detail"),
    ),
    QuerySpec(
        "prq.pacs_study",
        "orders",
        "get_pacs_study",
        "study",
        ("ref",),
        ("prq.order_history", "prq.order_detail", "prq.order_report"),
    ),
    QuerySpec(
        "prq.pacs_image", "orders", "download_pacs_image", "image", ("ref",), ("prq.pacs_study",)
    ),
    QuerySpec(
        "prq.pdf_attachment",
        "orders",
        "download_pdf",
        "pdf",
        ("ref",),
        ("prq.order_report", "prq.upload_history", "prq.text_report"),
    ),
    QuerySpec(
        "prq.opd_patients",
        "opd",
        "get_doctor_patients",
        "doctor_day",
        ("doctor_card", "visit_date"),
    ),
    QuerySpec(
        "oppl.surgery_schedule",
        "surgery",
        "get_schedule",
        "doctor_range",
        ("doctor_card", "start", "end"),
    ),
    QuerySpec(
        "audit.unsigned_records",
        "audit",
        "get_unsigned_records",
        "doctor_range",
        ("doctor_card", "start", "end"),
    ),
)
QUERY_SPECS += (
    *(
        QuerySpec(f"prq.{key}", "records", f"get_{key}", "patient", ("mrn",))
        for key in (
            "allergy",
            "advance_directives",
            "research_flags",
            "bed_transfers",
            "care_cases",
            "upload_history",
        )
    ),
    QuerySpec("prq.upload_types", "records", "get_upload_types", "catalog", ()),
    QuerySpec(
        "prq.text_report_history",
        "records",
        "get_text_report_history",
        "text_history",
        ("mrn", "department", "days"),
    ),
    QuerySpec(
        "prq.text_report",
        "records",
        "get_text_report",
        "text_report",
        ("ref",),
        ("prq.text_report_history",),
    ),
    *(
        QuerySpec(f"oppl.{key}", "surgery", f"get_{key}", "patient", ("mrn",))
        for key in (
            "patient_info",
            "patient_consents",
            "request_numbers",
            "anticoagulants",
            "sglt2",
        )
    ),
    *(
        QuerySpec(f"oppl.{key}", "surgery", f"get_{key}", "catalog", ())
        for key in ("procedure_catalog", "holidays", "consent_catalog")
    ),
    QuerySpec(
        "oppl.consent_doctor", "surgery", "resolve_consent_doctor", "doctor", ("doctor_card",)
    ),
    QuerySpec(
        "oppl.consent_template",
        "surgery",
        "get_consent_template",
        "consent_template",
        ("name", "department"),
        ("oppl.consent_catalog",),
    ),
    QuerySpec(
        "oppl.schedule_form",
        "surgery",
        "open_schedule_form",
        "schedule_form",
        ("fields",),
        ("oppl.patient_info",),
    ),
)
QUERY_SPECS += (
    QuerySpec("oppl_records.departments", "surgery", "get_case_departments", "catalog", ()),
    QuerySpec("oppl_records.cases", "surgery", "get_cases", "doctor_surgery", ("filter",)),
    QuerySpec(
        "oppl_records.note",
        "surgery",
        "get_record_ref",
        "surgery_note",
        ("ref",),
        ("oppl_records.cases",),
    ),
    QuerySpec(
        "oppl_records.pdf",
        "surgery",
        "download_record",
        "surgery_pdf",
        ("ref",),
        ("oppl_records.note",),
    ),
)
QUERY_SPECS += (
    QuerySpec("review.login_info", "reviews", "get_login_info", "catalog", ()),
    QuerySpec("review.options", "reviews", "get_options", "catalog", ()),
    QuerySpec(
        "review.doctors",
        "reviews",
        "get_doctors",
        "review_department",
        ("department",),
        ("review.options",),
    ),
    QuerySpec(
        "review.cases", "reviews", "get_cases", "doctor_review", ("filter",), ("review.options",)
    ),
    *(
        QuerySpec(f"review.{key}", "reviews", method, "review_case", ("ref",), ("review.cases",))
        for key, method in (
            ("case_detail", "get_case"),
            ("orders", "get_orders"),
            ("attachments", "get_attachments"),
            ("pacs", "get_pacs"),
        )
    ),
)
QUERY_BY_KEY = MappingProxyType({spec.key: spec for spec in QUERY_SPECS})


def query_spec(key: str) -> QuerySpec:
    try:
        return QUERY_BY_KEY[key]
    except (KeyError, TypeError) as exc:
        raise ConfigurationError("unknown query operation", code="QUERY_UNKNOWN") from exc


def resolve_queries(keys: tuple[str, ...]) -> tuple[QuerySpec, ...]:
    """Expand the test harness's data-discovery dependencies, in stable order."""
    result: list[QuerySpec] = []
    visited: set[str] = set()
    active: set[str] = set()

    def add(key: str) -> None:
        if key in active:
            raise ConfigurationError("query dependency cycle", code="QUERY_DEPENDENCY_CYCLE")
        if key in visited:
            return
        spec = query_spec(key)
        active.add(key)
        for dependency in spec.dependencies:
            add(dependency)
        active.remove(key)
        visited.add(key)
        result.append(spec)

    for key in keys:
        add(key)
    return tuple(result)


def run_query(sdk: Any, key: str, **inputs: Any) -> Any:
    """Run exactly one Service method with caller-owned, typed input objects.

    Dependencies are metadata for discovery/testing; this call never fetches
    other records implicitly. A caller can reuse refs from any prior result.
    """
    spec = query_spec(key)
    if any(set(inputs) == set(variant) for variant in spec.alternative_inputs):
        return getattr(getattr(sdk, spec.service), spec.method)(**inputs)
    if set(inputs) != set(spec.inputs):
        raise ConfigurationError(
            "query inputs do not match the catalog", code="QUERY_INPUTS_INVALID"
        )
    service = getattr(sdk, spec.service)
    return getattr(service, spec.method)(*(inputs[name] for name in spec.inputs))


class Queries:
    """Optional catalog facade; typed ``sdk.records``/``sdk.orders`` remain public."""

    catalog = QUERY_SPECS

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def run(self, key: str, **inputs: Any) -> Any:
        return run_query(self._sdk, key, **inputs)


if len(QUERY_BY_KEY) != len(QUERY_SPECS):
    raise RuntimeError("duplicate query key")
for _spec in resolve_queries(tuple(QUERY_BY_KEY)):
    if operation_spec(_spec.key).mutates:
        raise RuntimeError("a clinical write cannot be included in the query catalog")
