"""Focused intranet validation of structured SOAP from an OPD roster."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import AuthenticationError, ErrorInfo
from ..core.operations import OPERATIONS
from ..local_io import write_json_atomic
from ..models import SoapRecord, to_jsonable
from ..queries import query_spec, run_query
from ..workflows.opd_soap import classify_opd_registration, select_registration_visits
from .atomic import _write_coverage
from .preflight import independent_readiness, run_network_checks
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)
from .scenarios import diverse_sample


def build_soap_plan(config: Any) -> dict[str, Any]:
    keys = ("prq.opd_patients", "prq.visit_cases", "prq.soap")
    return {
        "schema_version": 1,
        "profile": "soap",
        "roster_date": config.soap_date.isoformat(),
        "doctor_source": "portal_login_card",
        "ownership": "returned physician equals login card or login card + F",
        "patient_limit": config.max_cases,
        "visits_per_patient_limit": config.max_items,
        "selection": "different MRNs from dedicated registrations; vary section and room",
        "auth_targets": ["portal", "prq"],
        "network_checks": ["dns", "tcp", "https"],
        "continue_independent_checks": True,
        "excluded_write_operations": [spec.key for spec in OPERATIONS if spec.mutates],
        "operations": [
            {
                "key": key,
                "sdk_method": query_spec(key).sdk_method,
                "scope": query_spec(key).scope,
                "inputs": list(query_spec(key).inputs),
                "dependencies": list(query_spec(key).dependencies),
                "requested": True,
            }
            for key in keys
        ],
    }


def select_soap_patients(roster: list[Any], *, doctor_card: str, limit: int) -> list[Any]:
    """Select distinct patients only when the response identifies their physician."""
    dedicated = [
        row for row in roster
        if row.mrn.strip()
        and classify_opd_registration(row, doctor_card=doctor_card) == "DEDICATED"
    ]
    by_mrn: dict[str, Any] = {}
    for row in dedicated:
        by_mrn.setdefault(row.mrn, row)
    return diverse_sample(
        list(by_mrn.values()), limit, group=lambda row: (row.section_code, row.room)
    )


def _soap_status(record: SoapRecord) -> str:
    if record.parsing_issues:
        return "ERROR"
    return "OK" if record.blocks else "NO_SAMPLE"


def _soap_counts(record: SoapRecord) -> dict[str, Any]:
    return {
        "block_count": len(record.blocks),
        "present_sections": list(record.present_sections),
        "diagnosis_count": len(record.diagnoses),
        "order_count": len(record.orders),
        "medication_count": len(record.medications),
        "chronic_prescription_period_count": len(record.chronic_prescription_periods),
        "parsing_issues": list(record.parsing_issues),
    }


def run_soap_test(
    sdk: Any,
    config: Any,
    *,
    plan: dict[str, Any],
    output_dir: Path,
    login_card: str | None,
    settings: Any = None,
    raw_capture: Any = None,
    diagnostics: Any = None,
    run_id: str = "",
) -> LiveTestResult:
    root = output_dir.resolve()
    parsed = root / "parsed" / "soap"
    root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(root / "test_plan.json", plan)
    steps: list[LiveTestStep] = []
    common = {"root": root, "raw_capture": raw_capture, "diagnostics": diagnostics}
    if settings is not None:
        run_network_checks(
            settings, steps, only=("portal", "prq"),
            configure_connection=getattr(sdk, "configure_connection", None),
            allow_unverified_tls=config.allow_unverified_tls, **common,
        )
    readiness = independent_readiness(sdk, steps, only=("portal", "prq"), **common)
    fatal = not _target_ready(readiness, "portal")
    prq_ready = not fatal and _target_ready(readiness, "prq")
    roster: list[Any] | None = None
    selected: list[Any] = []
    parsed_records: list[SoapRecord] = []
    matched_patients = 0
    if prq_ready and login_card:
        roster, error = _run_step(
            steps, name="soap.roster", operation_key="prq.opd_patients",
            operation=lambda: run_query(
                sdk, "prq.opd_patients", doctor_card=login_card, visit_date=config.soap_date
            ),
            output_path=parsed / "roster.json",
            classify=lambda rows: "OK" if rows else "EMPTY",
            summarize=lambda rows: {"registration_count": len(rows)}, **common,
        )
        fatal = isinstance(error, AuthenticationError)
    else:
        steps.append(LiveTestStep(
            "soap.roster", "BLOCKED", operation="prq.opd_patients",
            issue=ErrorInfo("READINESS_FAILED", "DEPENDENCY"),
        ))

    if roster is not None:
        classified = [
            {
                "registration": to_jsonable(row),
                "ownership": classify_opd_registration(row, doctor_card=login_card or ""),
            }
            for row in roster
        ]
        selected = select_soap_patients(
            roster, doctor_card=login_card or "", limit=config.max_cases
        )
        write_json_atomic(parsed / "classified_roster.json", classified)
        write_json_atomic(parsed / "selection.json", {
            "roster_date": config.soap_date.isoformat(),
            "registered_count": len(roster),
            "dedicated_count": sum(row["ownership"] == "DEDICATED" for row in classified),
            "shared_count": sum(row["ownership"] == "SHARED" for row in classified),
            "unclassified_count": sum(row["ownership"] == "UNCLASSIFIED" for row in classified),
            "selected_count": len(selected),
            "selected": to_jsonable(selected),
            "patient_limit": config.max_cases,
            "visits_per_patient_limit": config.max_items,
        })
        steps.append(LiveTestStep(
            "soap.patient_selection", "OK" if len(selected) >= 2 else "NO_SAMPLE",
            output="parsed/soap/selection.json",
            details={"selected_count": len(selected), "minimum_target": 2},
        ))
        write_json_atomic(root / "step_results.json", to_jsonable(steps))

    for index, patient in enumerate(selected, 1):
        if fatal:
            break
        patient_dir = parsed / "patients" / f"{index:04d}"
        cases, error = _run_step(
            steps, name=f"soap.patient_{index:04d}.visits",
            operation_key="prq.visit_cases",
            operation=lambda mrn=patient.mrn: run_query(sdk, "prq.visit_cases", mrn=mrn),
            output_path=patient_dir / "visit_cases.json",
            classify=lambda rows: "OK" if rows else "EMPTY",
            summarize=lambda rows: {"visit_count": len(rows)}, **common,
        )
        fatal = isinstance(error, AuthenticationError)
        if cases is None:
            continue
        registrations = [row for row in roster or [] if row.mrn == patient.mrn]
        matching = select_registration_visits(
            registrations, cases, doctor_card=login_card or ""
        )
        write_json_atomic(patient_dir / "matching_visits.json", {
            "matching_count": len(matching),
            "selected": to_jsonable(matching[: config.max_items]),
            "omitted": max(0, len(matching) - config.max_items),
            "policy": "same MRN, registration date, section and outpatient type",
        })
        if not matching:
            steps.append(LiveTestStep(
                f"soap.patient_{index:04d}.same_day_visit", "NO_SAMPLE",
                output=(patient_dir / "matching_visits.json").relative_to(root).as_posix(),
                details={"reason": "REGISTERED_WITHOUT_MATCHING_OUTPATIENT_VISIT"},
            ))
            write_json_atomic(root / "step_results.json", to_jsonable(steps))
            continue
        matched_patients += 1
        for case_index, case in enumerate(matching[: config.max_items], 1):
            record, error = _run_step(
                steps, name=f"soap.patient_{index:04d}.case_{case_index:04d}",
                operation_key="prq.soap",
                operation=lambda case=case: run_query(sdk, "prq.soap", case=case),
                output_path=patient_dir / f"soap_{case_index:04d}.json",
                classify=_soap_status, summarize=_soap_counts,
                classified_error_code="SOAP_PARTIAL_PARSE",
                **common,
            )
            if record is not None:
                parsed_records.append(record)
            if isinstance(error, AuthenticationError):
                fatal = True
                break

    fields = {
        "subjective": sum(row.subjective is not None for row in parsed_records),
        "objective": sum(row.objective is not None for row in parsed_records),
        "assessment_plan": sum(row.assessment_plan is not None for row in parsed_records),
        "assessment": sum(row.assessment is not None for row in parsed_records),
        "plan": sum(row.plan is not None for row in parsed_records),
        "diagnoses": sum(bool(row.diagnoses) for row in parsed_records),
        "orders": sum(bool(row.orders) for row in parsed_records),
        "medications": sum(bool(row.medications) for row in parsed_records),
        "chronic_prescription_periods": sum(
            bool(row.chronic_prescription_periods) for row in parsed_records
        ),
    }
    coverage_path = parsed / "field_coverage.json"
    write_json_atomic(coverage_path, {
        "roster_date": config.soap_date.isoformat(),
        "selected_patient_count": len(selected),
        "matching_visit_patient_count": matched_patients,
        "soap_response_count": len(parsed_records),
        "nonempty_soap_count": sum(bool(row.blocks) for row in parsed_records),
        "records_with_parsing_issues": sum(bool(row.parsing_issues) for row in parsed_records),
        "field_record_counts": fields,
        "note": "Missing optional sections are sample gaps, not inferred absence of clinical data.",
    })
    if roster is not None and not fatal:
        for field in ("subjective", "objective", "assessment_plan", "diagnoses", "orders", "medications"):
            steps.append(LiveTestStep(
                f"soap.coverage.{field}", "OK" if fields[field] else "NO_SAMPLE",
                output=coverage_path.relative_to(root).as_posix(),
                details={"record_count": fields[field]},
            ))
    write_json_atomic(root / "step_results.json", to_jsonable(steps))
    status = _overall_status(steps, fatal_auth=fatal)
    _write_coverage(root, plan, steps, status)
    summary_path = root / "run_summary.json"
    write_json_atomic(summary_path, {
        "schema_version": LIVE_TEST_SCHEMA_VERSION,
        "run_id": run_id,
        "profile": "soap",
        "status": status,
        "roster_date": config.soap_date.isoformat(),
        "selected_patient_count": len(selected),
        "matching_visit_patient_count": matched_patients,
        "soap_response_count": len(parsed_records),
        "steps": to_jsonable(steps),
        "test_plan": "test_plan.json",
        "coverage": "coverage.json",
        "field_coverage": coverage_path.relative_to(root).as_posix(),
    })
    return LiveTestResult(status, tuple(steps), summary_path, run_id)
