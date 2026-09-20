"""Incremental live validation of patient-ID lookup and visit selection."""

from __future__ import annotations

import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from ..core.errors import ErrorInfo, ParseError
from ..core.operations import OPERATIONS
from ..core.readiness import resolve_auth_targets
from ..identifiers import normalize_mrn, normalize_national_id
from ..local_io import write_json_atomic
from ..models import VisitCase, VisitFilter, to_jsonable
from ..queries import query_spec, run_query
from .atomic import _classify, _counts, _write_coverage
from .preflight import independent_readiness, run_network_checks
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)


def build_visit_plan(config: Any, *, automatic_id: bool = True) -> dict[str, Any]:
    keys = ["prq.visit_cases", "prq.soap", "prq.case_orders"]
    if automatic_id:
        keys.insert(0, "webmaas.basic_info")
    targets = resolve_auth_targets(("prq", "webmaas") if automatic_id else ("prq",))
    return {
        "schema_version": 1,
        "profile": "visits",
        "auth_targets": [target.key for target in targets],
        "max_cases": min(config.max_cases or 3, 3),
        "max_items_per_operation": 0,
        "network_checks": ["dns", "tcp", "https"],
        "continue_independent_checks": True,
        "excluded_write_operations": [spec.key for spec in OPERATIONS if spec.mutates],
        "automatic_patient_id": automatic_id,
        "visit_checks": [
            "mrn_lookup",
            "national_id_lookup",
            "same_patient",
            "list_equivalence",
            "date",
            "category_O_A_E",
            "department",
            "physician",
            "combined_filter",
            "selected_outpatient_soap",
            "selected_outpatient_orders",
        ],
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


def compare_visits(by_mrn: list[VisitCase], by_id: list[VisitCase]) -> dict[str, Any]:
    left = {case.identity: case for case in by_mrn}
    right = {case.identity: case for case in by_id}
    fields = ("section_name", "doctor_name", "doctor_card")
    changed = [
        {
            "identity": key,
            "fields": [
                field for field in fields if getattr(left[key], field) != getattr(right[key], field)
            ],
        }
        for key in sorted(left.keys() & right.keys())
        if any(getattr(left[key], field) != getattr(right[key], field) for field in fields)
    ]
    return {
        "matches": left.keys() == right.keys() and not changed,
        "mrn_count": len(by_mrn),
        "national_id_count": len(by_id),
        "only_in_mrn": sorted(left.keys() - right.keys()),
        "only_in_national_id": sorted(right.keys() - left.keys()),
        "metadata_differences": changed,
    }


def filter_scenarios(cases: list[VisitCase]):
    """Choose real samples; expected identities are calculated independently."""
    for category in ("O", "A", "E"):
        yield (
            f"category_{category}",
            VisitFilter(all_sections=True, case_types=(category,)),
            [case for case in cases if case.case_type == category],
        )
    types = ("O", "A", "E")
    sample = next((case for case in cases if case.visit_date), None)
    if sample:
        day = sample.visit_date
        yield (
            "arrival_date",
            VisitFilter(all_sections=True, case_types=types, start_date=day, end_date=day),
            [case for case in cases if case.visit_date == day and case.case_type in types],
        )
    else:
        yield "arrival_date", None, []
    for label, field, filter_field in (
        ("department_code", "section_code", "section_codes"),
        ("department_name", "section_name", "section_name_contains"),
        ("doctor_name", "doctor_name", "doctor_names"),
    ):
        sample = next(
            (case for case in cases if case.case_type in types and getattr(case, field)), None
        )
        if sample is None:
            yield label, None, []
            continue
        value = getattr(sample, field)
        selector = VisitFilter(
            case_types=types,
            all_sections=not label.startswith("department"),
            **{filter_field: (value,)},
        )

        def normalized(text, field=field):
            text = unicodedata.normalize("NFKC", text)
            return (
                "".join(text.split()).upper()
                if field in {"section_code", "doctor_card"}
                else text.strip().casefold()
            )

        expected = [
            case
            for case in cases
            if case.case_type in types
            and (
                normalized(value) in normalized(getattr(case, field))
                if filter_field.endswith("contains")
                else normalized(value) == normalized(getattr(case, field))
            )
        ]
        yield label, selector, expected
    sample = next(
        (
            case
            for case in cases
            if case.case_type in types
            and case.visit_date
            and case.section_code
            and case.doctor_name
        ),
        None,
    )
    if sample:
        yield (
            "combined",
            VisitFilter(
                case_types=(sample.case_type,),
                section_codes=(sample.section_code,),
                start_date=sample.visit_date,
                end_date=sample.visit_date,
                doctor_names=(sample.doctor_name,),
            ),
            [
                case
                for case in cases
                if (case.visit_date, case.case_type, case.section_code, case.doctor_name)
                == (sample.visit_date, sample.case_type, sample.section_code, sample.doctor_name)
            ],
        )
    else:
        yield "combined", None, []


def run_visit_test(
    sdk: Any,
    config: Any,
    *,
    output_dir: Path,
    settings: Any = None,
    patient_national_id: str | None = None,
    raw_capture: Any = None,
    diagnostics: Any = None,
    run_id: str = "",
) -> LiveTestResult:
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    parsed = root / "parsed/visits"
    mrn = normalize_mrn(config.test_mrn)
    plan = build_visit_plan(config, automatic_id=not patient_national_id)
    write_json_atomic(root / "test_plan.json", plan)
    steps: list[LiveTestStep] = []
    common = {"root": root, "raw_capture": raw_capture, "diagnostics": diagnostics}

    def skipped(name, reason, *, status="BLOCKED", key=""):
        steps.append(
            LiveTestStep(name, status, operation=key, issue=ErrorInfo(reason, "DEPENDENCY"))
        )
        write_json_atomic(root / "step_results.json", to_jsonable(steps))
        print(f"{status} {name} {reason}", flush=True)

    if settings is not None:
        run_network_checks(
            settings,
            steps,
            only=tuple(plan["auth_targets"]),
            configure_connection=getattr(sdk, "configure_connection", None),
            allow_unverified_tls=config.allow_unverified_tls,
            **common,
        )
    readiness = independent_readiness(sdk, steps, only=tuple(plan["auth_targets"]), **common)
    fatal = not _target_ready(readiness, "portal")

    def query(label, key, arguments):
        name = "visits." + label.replace("/", ".")
        if fatal or not _target_ready(readiness, query_spec(key).app):
            skipped(name, "READINESS_FAILED", key=key)
            return None
        write_json_atomic(parsed / f"{label}.input.json", to_jsonable(arguments))
        value, _ = _run_step(
            steps,
            name=name,
            operation_key=key,
            operation=lambda: run_query(sdk, key, **arguments),
            output_path=parsed / f"{label}.json",
            classify=_classify,
            summarize=_counts,
            **common,
        )
        return value

    by_mrn = query("by_mrn", "prq.visit_cases", {"mrn": mrn})
    if not patient_national_id:
        basic = query("identity_source", "webmaas.basic_info", {"mrn": mrn})
        if basic is not None:

            def identity():
                if basic.mrn != mrn:
                    raise ParseError(
                        "identity source belongs to another patient", code="TEST_PATIENT_MISMATCH"
                    )
                return normalize_national_id(basic.national_id)

            patient_national_id, _ = _run_step(
                steps,
                name="visits.patient_id",
                operation=identity,
                output_path=parsed / "patient_id.json",
                **common,
            )
    by_id = None
    if patient_national_id:
        by_id = query("by_national_id", "prq.visit_cases", {"national_id": patient_national_id})
    else:
        skipped("visits.by_national_id", "PATIENT_ID_UNAVAILABLE", key="prq.visit_cases")

    matching_id = by_id is not None and all(case.mrn == mrn for case in by_id)
    if by_id is not None:
        _run_step(
            steps,
            name="visits.same_patient",
            operation=lambda: {"matches": matching_id, "record_count": len(by_id)},
            output_path=parsed / "same_patient.json",
            classify=lambda value: "ERROR"
            if not value["matches"]
            else "OK"
            if value["record_count"]
            else "NO_SAMPLE",
            classified_error_code="TEST_PATIENT_MISMATCH",
            **common,
        )
    if by_mrn is not None and by_id is not None:
        _run_step(
            steps,
            name="visits.list_equivalence",
            operation=lambda: compare_visits(by_mrn, by_id),
            output_path=parsed / "list_comparison.json",
            classify=lambda value: "ERROR"
            if not value["matches"]
            else "OK"
            if value["mrn_count"]
            else "NO_SAMPLE",
            classified_error_code="VISIT_LISTS_DIFFER",
            **common,
        )
    else:
        skipped("visits.list_equivalence", "VISIT_LOOKUP_FAILED")

    source = "national_id" if matching_id and by_id else "mrn_fallback"
    cases = by_id if source == "national_id" else by_mrn
    # Never send follow-up queries for an unexpected patient, even when a
    # manual national ID was mistyped or a custom adapter returned bad data.
    if cases is not None and any(case.mrn != mrn for case in cases):
        cases = None
        skipped("visits.selection", "TEST_PATIENT_MISMATCH")
    write_json_atomic(
        parsed / "selection_source.json", {"source": source, "record_count": len(cases or [])}
    )
    if cases is not None:
        write_json_atomic(
            parsed / "field_coverage.json",
            {
                "record_count": len(cases),
                "categories": dict(Counter(case.case_type for case in cases)),
                "with_date": sum(case.visit_date is not None for case in cases),
                "with_doctor_name": sum(bool(case.doctor_name) for case in cases),
                "with_doctor_card": sum(bool(case.doctor_card) for case in cases),
            },
        )
        for label, selector, expected in filter_scenarios(cases):
            if selector is None:
                skipped("visits.filters." + label, "FILTER_FIELD_UNAVAILABLE", status="NO_SAMPLE")
                continue

            def check(selector=selector, expected=expected):
                selected = selector.select(cases)
                return {
                    "filter": to_jsonable(selector),
                    "selected": to_jsonable(selected),
                    "expected_count": len(expected),
                    "actual_count": len(selected),
                    "matches": {case.identity for case in selected}
                    == {case.identity for case in expected},
                }

            _run_step(
                steps,
                name="visits.filters." + label,
                operation=check,
                output_path=parsed / "filters" / f"{label}.json",
                classify=lambda value: "ERROR"
                if not value["matches"]
                else "OK"
                if value["actual_count"]
                else "NO_SAMPLE",
                classified_error_code="VISIT_FILTER_MISMATCH",
                **common,
            )
    else:
        skipped("visits.filters", "VISIT_LOOKUP_FAILED")

    samples = VisitFilter(all_sections=True).select(cases or [])
    samples.sort(key=lambda case: "眼科" not in case.section_name)
    samples = samples[: plan["max_cases"]]
    write_json_atomic(parsed / "selected_outpatient.json", to_jsonable(samples))
    positive = {"soap": False, "orders": False}
    if not samples:
        skipped(
            "visits.followup",
            "NO_OUTPATIENT_SAMPLE",
            status="NO_SAMPLE" if cases is not None else "BLOCKED",
        )
    for index, case in enumerate(samples, 1):
        for label, key in (("soap", "prq.soap"), ("orders", "prq.case_orders")):
            if positive[label]:
                continue
            value = query(f"followup/{index:02d}_{label}", key, {"case": case})
            if value is not None and _counts(value).get("record_count", 0) > 0:
                positive[label] = True
        if all(positive.values()):
            break
    for label, succeeded in positive.items():
        if samples and not succeeded:
            skipped(f"visits.followup.{label}_sample", "NO_NONEMPTY_SAMPLE", status="NO_SAMPLE")

    status = _overall_status(steps, fatal_auth=fatal)
    _write_coverage(root, plan, steps, status)
    with (root / "RESULTS.txt").open("a", encoding="utf-8") as handle:
        handle.write("\nVisit search checks (NO_SAMPLE is missing evidence, not an error):\n")
        for step in steps:
            if step.name.startswith("visits."):
                handle.write(f"  {step.name}: {step.status} {step.error_code}\n")
        handle.write("Details and selected records: parsed/visits/\n")
    path = root / "run_summary.json"
    write_json_atomic(
        path,
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "run_id": run_id,
            "profile": "visits",
            "status": status,
            "steps": to_jsonable(steps),
            "test_plan": "test_plan.json",
            "coverage": "coverage.json",
            "visit_search": {
                "directory": "parsed/visits",
                "selection_source": source,
                "positive_followup": positive,
            },
        },
    )
    return LiveTestResult(status, tuple(steps), path, run_id)
