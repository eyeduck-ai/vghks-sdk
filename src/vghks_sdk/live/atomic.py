"""Bounded, independently testable Service queries with discovery dependencies."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..core.config import EarningsCredentials, SDKSettings
from ..core.errors import AuthenticationError, ErrorInfo
from ..core.operations import OPERATIONS
from ..core.readiness import AUTH_CHECK_REGISTRY, resolve_auth_targets
from ..local_io import write_json_atomic
from ..models import (
    BinaryAsset,
    MedicationHistoryFilter,
    NumericHistoryFilter,
    OrderHistoryFilter,
    OrderReport,
    PatientSurgeryRecord,
    ReviewCaseFilter,
    ReviewCasePart,
    SurgeryCaseFilter,
    SurgeryHistoryFilter,
    TextReportHistory,
    UploadHistory,
    to_jsonable,
)
from ..queries import QUERY_SPECS, QuerySpec, resolve_queries, run_query
from ..search import DoctorOpdPatientSource
from ..workflows.opd_soap import scan_opd_soap
from ..workflows.patient_records import select_asset_orders
from .config import LiveTestConfig
from .preflight import independent_readiness, run_network_checks
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)
from .scenarios import diverse_sample, history_scenarios, reference_group

OPHTHALMOLOGY_QUERIES = (
    "prq.visit_cases",
    "prq.order_history",
    "prq.case_orders",
    "prq.order_detail",
    "prq.order_report",
    "prq.pacs_study",
    "prq.pdf_attachment",
    "prq.pacs_image",
)


def build_test_plan(config: LiveTestConfig) -> dict[str, Any]:
    config.validate_for_execution()
    requested = config.only_operations or tuple(
        spec.key
        for spec in QUERY_SPECS
        if (
            config.profile == "comprehensive"
            or config.doctor_card
            or (spec.key == "oppl_records.cases" and config.has_surgery_physician())
            or (spec.key == "review.cases" and config.review_query)
            or not spec.scope.startswith("doctor_")
        )
        and (config.download_assets or spec.scope not in {"image", "pdf", "surgery_pdf"})
    )
    if config.profile == "ophthalmology":
        requested = tuple(
            key
            for key in OPHTHALMOLOGY_QUERIES
            if config.download_assets or key not in {"prq.pdf_attachment", "prq.pacs_image"}
        )
    specs = (
        resolve_queries(requested)
        if config.profile in {"atomic", "comprehensive", "ophthalmology"}
        else ()
    )
    targets = tuple(dict.fromkeys(spec.app for spec in specs)) or ("prq", "webmaas")
    if config.profile == "auth" or (
        config.profile == "comprehensive" and not config.only_operations
    ):
        targets = tuple(spec.key for spec in AUTH_CHECK_REGISTRY)
    elif config.profile == "comprehensive":
        if config.weekly_opd_soap:
            targets = (*targets, "prq")
        targets = tuple(spec.key for spec in resolve_auth_targets(targets))
    if config.profile == "ophthalmology":
        targets = ("portal", "prq")
    network = config.profile in {"comprehensive", "ophthalmology"}
    return {
        "schema_version": 1,
        "profile": config.profile,
        "auth_targets": list(targets),
        "max_cases": config.max_cases,
        "max_items_per_operation": config.max_items,
        "review_cases": {
            "enabled": any(spec.key.startswith("review.") for spec in specs),
            "query": to_jsonable(config.review_query),
            "case_limit": None,
            "detail_sample_limit": config.max_items,
            "default_physician": "login_account_when_no_filter_is_set",
            "parts": ["case_detail", "orders", "attachments", "pacs"],
            "attachment_download_recorded": False,
        },
        "surgery_cases": {
            "enabled": any(spec.key.startswith("oppl_records.") for spec in specs),
            "query": to_jsonable(config.surgery_query),
            "case_limit": None,
            "note_sample_limit": config.max_items,
            "default_physician": "login_account_when_no_role_filter_is_set",
            "pdf_response_verified_in_har": False,
        },
        "network_checks": ["dns", "tcp", "https"] if network else [],
        "network_checks_on_https_failure": [
            "https_tls12",
            "https_tls12_compat_if_tls12_failed",
            "https_direct_if_proxy",
            "https_direct_tls12_if_proxy",
            "https_direct_tls12_compat_if_needed",
            "https_unverified_comparison_if_all_verified_profiles_failed",
            "https_schannel_if_all_verified_profiles_failed_on_windows",
        ]
        if network
        else [],
        "continue_independent_checks": network,
        "ophthalmology_orders": {
            "enabled": config.profile == "ophthalmology",
            "entry_path": "order_list",
            "terms": list(config.asset_terms),
            "order_limit_per_term": config.max_items,
            "unexecuted_orders": "record_and_skip_without_consuming_sample_budget",
            "all_images_of_selected_studies": True,
            "independent_branches": ["report_text", "pdf", "jpg"],
        },
        "report_data_validation": "Report text is assessed independently of binary attachment downloads.",
        "excluded_write_operations": [spec.key for spec in OPERATIONS if spec.mutates],
        "earnings_reports": {
            "enabled": config.include_earnings,
            "requires_secondary_password": True,
        },
        "apply_verified_connection_before_login": network,
        "unverified_comparison_sends_credentials": False,
        "apply_unverified_connection": network and config.allow_unverified_tls,
        "weekly_opd_soap": {
            "enabled": config.profile == "comprehensive" and config.weekly_opd_soap,
            "doctor_source": "login_account",
            "end_date": (config.weekly_opd_end or date.today()).isoformat(),
            "inclusive_days": 7,
            "ownership": "returned physician equals login card or login card + F",
            "shared": "explicit empty physician label",
            "search": "arrange CATA",
            "ignore_case": True,
            "requires_same_registration_date": True,
            "patient_limit": None,
            "case_limit": None,
        },
        "operations": [
            {
                "key": spec.key,
                "sdk_method": spec.sdk_method,
                "scope": spec.scope,
                "inputs": list(spec.inputs),
                "dependencies": list(spec.dependencies),
                "requested": spec.key in requested,
                "input_available": bool(
                    config.doctor_card
                    or (spec.key == "oppl_records.cases" and config.has_surgery_physician())
                )
                if spec.scope.startswith("doctor_")
                else None,
            }
            for spec in specs
        ],
    }


def run_atomic_test(
    sdk: Any,
    config: LiveTestConfig,
    *,
    output_dir: Path,
    raw_capture: Any = None,
    diagnostics: Any = None,
    run_id: str = "",
    settings: SDKSettings | None = None,
    login_card: str | None = None,
    earnings_credentials: EarningsCredentials | None = None,
) -> LiveTestResult:
    plan = build_test_plan(config)
    if config.profile == "ophthalmology":
        from .ophthalmology import run_ophthalmology_test

        return run_ophthalmology_test(
            sdk,
            config,
            plan=plan,
            output_dir=output_dir,
            settings=settings,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
            run_id=run_id,
        )
    root = output_dir.resolve()
    parsed = root / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    write_json_atomic(root / "test_plan.json", plan)
    steps: list[LiveTestStep] = []
    values: dict[str, list[Any]] = {}
    common = {"root": root, "raw_capture": raw_capture, "diagnostics": diagnostics}
    comprehensive = config.profile == "comprehensive"
    if comprehensive:
        if settings is not None:
            run_network_checks(
                settings,
                steps,
                configure_connection=getattr(sdk, "configure_connection", None),
                allow_unverified_tls=config.allow_unverified_tls,
                include_mis=config.include_earnings,
                only=tuple(plan["auth_targets"]),
                **common,
            )
        report = independent_readiness(sdk, steps, only=tuple(plan["auth_targets"]), **common)
        fatal = not _target_ready(report, "portal")
    else:
        report, error = _run_step(
            steps,
            name="auth_check",
            operation=lambda: sdk.auth.check(only=tuple(plan["auth_targets"])),
            output_path=parsed / "readiness.json",
            classify=lambda value: "OK" if value.ok else "ERROR",
            **common,
        )
        fatal = isinstance(error, AuthenticationError) or not _target_ready(report, "portal")
    specs = resolve_queries(tuple(row["key"] for row in plan["operations"]))
    for spec in specs:
        values[spec.key] = []
        if fatal or not _target_ready(report, spec.app):
            steps.append(
                LiveTestStep(
                    spec.key,
                    "BLOCKED",
                    issue=ErrorInfo("READINESS_FAILED", "DEPENDENCY"),
                    operation=spec.key,
                )
            )
        else:
            try:
                inputs = _query_inputs(spec, config, values)
                write_json_atomic(parsed / "inputs" / f"{spec.key}.json", to_jsonable(inputs))
            except Exception as exc:
                # Input preparation is also a test boundary. A malformed
                # discovery object must not abort unrelated modules.
                _run_step(
                    steps,
                    name=f"{spec.key}.prepare",
                    operation=lambda exc=exc: _raise(exc),
                    operation_key=spec.key,
                    output_path=parsed / "inputs" / f"{spec.key}.json",
                    **common,
                )
                continue
            if not inputs:
                dependency_failed = any(
                    step.operation in spec.dependencies and step.status in {"ERROR", "BLOCKED"}
                    for step in steps
                )
                no_sample = bool(spec.dependencies) and not dependency_failed and all(
                    any(step.operation == key and step.status in {"OK", "EMPTY", "NO_SAMPLE"}
                        for step in steps)
                    for key in spec.dependencies
                ) and not spec.scope.startswith("doctor_")
                steps.append(
                    LiveTestStep(
                        spec.key,
                        "BLOCKED" if dependency_failed else "NO_SAMPLE" if no_sample else "MISSING",
                        issue=None if no_sample else ErrorInfo(
                            "DOCTOR_INPUT_MISSING"
                            if spec.scope.startswith("doctor_")
                            else "QUERY_INPUT_UNAVAILABLE",
                            "DEPENDENCY" if dependency_failed else "DATA",
                        ),
                        operation=spec.key,
                        details={"reason": "NO_MATCHING_REFERENCE"} if no_sample else None,
                    )
                )
            for index, arguments in enumerate(inputs, 1):
                destination = parsed / "atomic" / spec.key / f"{index:04d}.json"

                def invoke(
                    arguments: dict[str, Any] = arguments,
                    spec: QuerySpec = spec,
                    destination: Path = destination,
                ) -> Any:
                    value = run_query(sdk, spec.key, **arguments)
                    if isinstance(value, BinaryAsset):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        suffix = ".pdf" if value.media_type == "application/pdf" else ".jpg"
                        destination.with_suffix(suffix).write_bytes(value.content)
                    return value

                value, error = _run_step(
                    steps,
                    name=f"{spec.key}.{index:04d}",
                    operation=invoke,
                    operation_key=spec.key,
                    output_path=destination,
                    summarize=_counts,
                    classify=_classify,
                    missing_code="QUERY_RESULT_EMPTY",
                    classified_error_code="SURGERY_PDF_LINKS_INCOMPLETE"
                    if spec.key == "prq.surgery_history"
                    else "",
                    **common,
                )
                if value is not None:
                    values[spec.key].append(value)
                if isinstance(error, AuthenticationError):
                    if not comprehensive:
                        fatal = True
                    # The SDK has already tried one re-login. Skip more inputs
                    # for this operation, but comprehensive mode still attempts
                    # other operations and apps whose readiness succeeded.
                    for remaining in range(index + 1, len(inputs) + 1):
                        steps.append(
                            LiveTestStep(
                                f"{spec.key}.{remaining:04d}",
                                "BLOCKED",
                                issue=ErrorInfo("OPERATION_AUTH_FAILED", "DEPENDENCY"),
                                operation=spec.key,
                            )
                        )
                    break
        write_json_atomic(root / "step_results.json", to_jsonable(steps))
    if comprehensive and config.weekly_opd_soap:
        end = date.fromisoformat(plan["weekly_opd_soap"]["end_date"])
        workflow_dir = parsed / "workflows" / "opd_soap_week"

        def execute_query(name, key, arguments, destination):
            return _run_step(
                steps,
                name=name,
                operation=lambda: run_query(sdk, key, **arguments),
                operation_key=key,
                output_path=destination,
                summarize=_counts,
                classify=_classify,
                **common,
            )

        _run_step(
            steps,
            name="workflow.opd_soap_week",
            operation=lambda: scan_opd_soap(
                sdk,
                source=DoctorOpdPatientSource(
                    login_card or config.doctor_card or "", end - timedelta(days=6), end
                ),
                output_dir=workflow_dir,
                query_runner=execute_query,
                blocked_reason="PRQ_READINESS_FAILED"
                if fatal or not _target_ready(report, "prq")
                else "",
            ),
            output_path=workflow_dir / "result.json",
            classify=lambda value: (
                "OK"
                if value.status == "OK"
                else "BLOCKED"
                if value.status == "BLOCKED"
                else "ERROR"
            ),
            classified_error_code="WEEKLY_OPD_INCOMPLETE",
            summarize=lambda value: {"workflow_status": value.status, **value.counts},
            **common,
        )
    if config.include_earnings:
        for kind, method in (("performance", "open_performance"), ("payroll", "open_bonus")):
            key = f"mis.{kind}"
            if fatal or not earnings_credentials:
                steps.append(
                    LiveTestStep(
                        key,
                        "BLOCKED" if fatal else "MISSING",
                        issue=ErrorInfo(
                            "PORTAL_NOT_READY" if fatal else "EARNINGS_CREDENTIALS_MISSING",
                            "DEPENDENCY" if fatal else "DATA",
                        ),
                        operation=key,
                    )
                )
                continue
            context, error = _run_step(
                steps,
                name=f"{key}.open",
                operation=lambda method=method: getattr(sdk.earnings, method)(earnings_credentials),
                operation_key=key,
                output_path=parsed / "earnings" / f"{kind}-context.json",
                **common,
            )
            if error is None and context is not None:
                if not any(step.name == "auth_check.mis" for step in steps):
                    steps.append(LiveTestStep("auth_check.mis", "OK"))
                _run_step(
                    steps,
                    name=f"{key}.report",
                    operation=lambda context=context: sdk.earnings.get_report(context),
                    operation_key=key,
                    output_path=parsed / "earnings" / f"{kind}-report.json",
                    summarize=_counts,
                    **common,
                )
            write_json_atomic(
                root / "checkpoint.json", {"status": "RUNNING", "steps": to_jsonable(steps)}
            )
    status_steps = (
        [step for step in steps if step.name.startswith("auth_check")]
        if comprehensive and fatal
        else steps
    )
    status = _overall_status(status_steps, fatal_auth=fatal)
    # Readiness errors can be isolated to a selected subsystem.
    if report is not None and not report.ok and status == "OK":
        status = "COMPLETED_WITH_ERRORS"
    _write_coverage(root, plan, steps, status)
    summary_path = root / "run_summary.json"
    write_json_atomic(
        summary_path,
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "run_id": run_id,
            "profile": config.profile,
            "status": status,
            "steps": to_jsonable(steps),
            "test_plan": "test_plan.json",
            "coverage": "coverage.json",
        },
    )
    return LiveTestResult(status, tuple(steps), summary_path, run_id)


def _query_inputs(
    spec: QuerySpec, config: LiveTestConfig, values: dict[str, list[Any]]
) -> list[dict[str, Any]]:
    if spec.key == "review.cases":
        fields = dict(config.review_query)
        if not fields:
            if not config.doctor_card:
                return []
            fields["doctor_card"] = config.doctor_card
        departments = [
            item["Value"]
            for options in values.get("review.options", [])
            for item in options.get("InsuSectNo", [])
            if item["Value"] and not item.get("Disabled")
        ]
        if not fields.get("department") and len(set(departments)) == 1:
            fields["department"] = departments[0]
        return [{"filter": ReviewCaseFilter(**fields)}]
    if spec.scope == "review_department":
        departments = (
            [config.review_query["department"]]
            if config.review_query.get("department")
            else [
                item["Value"]
                for options in values.get("review.options", [])
                for item in options.get("InsuSectNo", [])
                if item["Value"] and not item.get("Disabled")
            ]
        )
        return [
            {"department": value} for value in list(dict.fromkeys(departments))[: config.max_items]
        ]
    if spec.scope == "review_case":
        cases = {row.reference: row for row in _flatten(values.get("review.cases", []))}
        sample = diverse_sample(
            list(cases.values()),
            config.max_items,
            group=lambda row: (row.verify_code, row.application_date[:4]),
        )
        return [{"ref": row.reference} for row in sample]
    if spec.key == "oppl_records.cases":
        fields = dict(config.surgery_query)
        periods = fields.pop("periods", ("1M",))
        if not any(
            (
                fields.get("surgeon_card"),
                fields.get("supervising_card"),
                *fields.get("assistant_cards", ()),
            )
        ):
            if not config.doctor_card:
                return []
            fields["surgeon_card"] = config.doctor_card
        return [{"filter": SurgeryCaseFilter(**fields, period=p)} for p in periods]
    if spec.key == "oppl_records.note":
        cases = {case.reference: case for case in _flatten(values.get("oppl_records.cases", []))}
        selected = diverse_sample(
            list(cases.values()), config.max_items, group=lambda case: case.surgery_date.year
        )
        return [{"ref": case.reference} for case in selected]
    if spec.key == "oppl_records.pdf":
        return [{"ref": ref} for ref in values.get("oppl_records.note", [])[: config.max_items]]
    if spec.scope == "catalog":
        return [{}]
    if spec.scope == "doctor":
        return [{"doctor_card": config.doctor_card}] if config.doctor_card else []
    if spec.scope == "text_history":
        return [
            {"mrn": config.test_mrn, "department": dept, "days": days}
            for dept, days in (("PATH", 182), ("RAD", 4000), ("CHK", 3650))
        ]
    if spec.scope == "text_report":
        histories = values.get("prq.text_report_history", [])
        if config.profile == "comprehensive":
            # Each recorded department gets its own sample budget. Otherwise
            # the first RAD list consumes every slot before CHK/DBR is reached.
            refs = list(
                dict.fromkeys(
                    ref
                    for history in histories
                    for ref in diverse_sample(
                        history.report_refs, config.max_items, group=reference_group
                    )
                )
            )
            return [{"ref": ref} for ref in refs]
        refs = list(dict.fromkeys(ref for item in histories for ref in item.report_refs))
        return [{"ref": ref} for ref in refs[: config.max_items]]
    if spec.scope == "schedule_form":
        rows = [
            row for value in values.get("oppl.patient_info", []) for row in value.get("surgs", [])
        ]
        return [{"fields": row} for row in rows[: config.max_items]]
    if spec.scope == "consent_template":
        result = []
        for value in values.get("oppl.consent_catalog", []):
            for department, names in value.get("forms", {}).items():
                if department != "OPH":
                    continue
                for name in names:
                    if isinstance(name, str):
                        result.append({"name": name, "department": department})
        return result[: config.max_items]
    if spec.scope == "patient":
        return [{"mrn": config.test_mrn}]
    if spec.scope == "history":
        if config.profile == "comprehensive":
            return [
                {"mrn": config.test_mrn, "filter": selector}
                for selector in history_scenarios(spec.key, order_date=config.order_date)
            ]
        filters = {
            "prq.order_history": OrderHistoryFilter(order_date=config.order_date),
            "prq.medication_history": MedicationHistoryFilter(),
            "prq.numeric_history": NumericHistoryFilter(),
            "prq.surgery_history": SurgeryHistoryFilter(),
        }
        return [{"mrn": config.test_mrn, "filter": filters[spec.key]}]
    if spec.scope == "case":
        cases = config.visit_filter.select(_flatten(values.get("prq.visit_cases", [])))
        if config.visit_date is not None:
            cases = [case for case in cases if case.visit_date == config.visit_date]
        if config.profile == "comprehensive":
            cases = diverse_sample(
                cases,
                config.max_cases,
                group=lambda case: (
                    case.section_code,
                    case.visit_date.year if case.visit_date else None,
                ),
            )
        return [{"case": case} for case in cases[: config.max_cases]]
    if spec.scope.startswith("doctor_") and not config.doctor_card:
        return []
    if spec.scope == "doctor_day":
        dates = [config.opd_date]
        if config.profile == "comprehensive" and config.range_start is not None:
            dates.extend(
                config.opd_date - timedelta(days=offset)
                for offset in range(1, 7)
                if config.range_start
                <= config.opd_date - timedelta(days=offset)
                <= config.range_end
            )
        return [{"doctor_card": config.doctor_card, "visit_date": day} for day in dates]
    if spec.scope == "doctor_range":
        inputs = [
            {
                "doctor_card": config.doctor_card,
                "start": config.range_start or config.opd_date,
                "end": config.range_end or config.opd_date,
            }
        ]
        if (
            config.profile == "comprehensive"
            and config.range_start != config.range_end
            and config.range_start <= config.opd_date <= config.range_end
        ):
            inputs.append(
                {
                    "doctor_card": config.doctor_card,
                    "start": config.opd_date,
                    "end": config.opd_date,
                }
            )
        return inputs
    candidates = _flatten(values.get("prq.order_history", []))
    if config.profile == "comprehensive":
        candidates += _flatten(values.get("prq.case_orders", []))
    orders = select_asset_orders(
        candidates,
        terms=config.asset_terms,
        all_matching=config.all_matching_orders or config.profile == "comprehensive",
    )
    if config.profile == "comprehensive":
        # Preferred HAR examples first, then other available types (including
        # attachments) so one empty keyword search cannot hide every asset.
        orders = [*orders, *candidates]
    details = values.get("prq.order_detail", [])
    reports = [*values.get("prq.order_report", []), *values.get("prq.text_report", [])]
    refs: Iterable[Any]
    if spec.scope == "detail":
        refs = [order.detail_ref for order in orders]
    elif spec.scope == "report":
        refs = [
            *[order.report_ref for order in orders],
            *[ref for detail in details for ref in detail.report_refs],
        ]
    elif spec.scope == "study":
        refs = [
            *[order.pacs_ref for order in orders],
            *[ref for item in (*details, *reports) for ref in item.pacs_refs],
        ]
    elif spec.scope == "image":
        refs = [ref for study in values.get("prq.pacs_study", []) for ref in study.images]
    else:
        # Prefer the explicit surgery-history source in a focused retest.
        # The same download operation also accepts report/upload attachments.
        refs = [
            ref
            for history in values.get("prq.surgery_history", [])
            for row in history
            for ref in row.surgery_record_refs
        ]
        refs.extend(ref for report in reports for ref in report.pdf_refs)
        refs.extend(
            ref for report in values.get("prq.upload_history", []) for ref in report.pdf_refs
        )
    unique = list(dict.fromkeys(ref for ref in refs if ref is not None))
    if config.profile == "comprehensive":
        unique = diverse_sample(unique, config.max_items, group=reference_group)
    return [{"ref": ref} for ref in unique[: config.max_items]]


def _flatten(values: list[Any]) -> list[Any]:
    return [item for rows in values for item in rows]


def _counts(value: Any) -> dict[str, Any]:
    if isinstance(value, ReviewCasePart):
        return {"record_count": value.total}
    if value is None:
        return {"record_count": 0}
    if isinstance(value, TextReportHistory):
        return {"record_count": len(value.report_refs)}
    if isinstance(value, UploadHistory):
        return {"record_count": len(value.pdf_refs), "table_count": len(value.document.tables)}
    if isinstance(value, dict):
        for key in ("consents", "surgs", "reqs", "pfiles", "forms", "caseList"):
            if key in value and isinstance(value[key], (list, dict)):
                return {"record_count": len(value[key])}
    if isinstance(value, OrderReport):
        return {
            "record_count": int(
                bool(value.fields or value.report_text or value.pdf_refs or value.pacs_refs)
            ),
            "report_data_status": value.report_data_status,
            "report_text_characters": len(value.report_text),
            "text_extraction_notes": list(value.text_extraction_notes),
        }
    if isinstance(value, (tuple, list)):
        if value and all(isinstance(row, PatientSurgeryRecord) for row in value):
            return {
                "record_count": len(value),
                "surgery_pdf_ref_count": sum(len(row.surgery_record_refs) for row in value),
                "surgery_pdf_issue_count": sum(len(row.surgery_record_issues) for row in value),
            }
        return {"record_count": len(value)}
    for field in ("tables", "blocks", "images"):
        if hasattr(value, field):
            return {"record_count": len(getattr(value, field))}
    if isinstance(value, BinaryAsset):
        return {"byte_count": value.size}
    return {"record_count": 1}


def _classify(value: Any) -> str:
    # A valid empty query does not establish a positive-data validation.
    count = _counts(value)
    if count.get("surgery_pdf_issue_count"):
        return "ERROR"
    return "EMPTY" if count.get("record_count") == 0 else "OK"


def _raise(error: Exception) -> None:
    raise error


def _write_coverage(
    root: Path, plan: dict[str, Any], steps: list[LiveTestStep], status: str
) -> None:
    rows = []
    for spec in plan["operations"]:
        matching = [step for step in steps if step.operation == spec["key"]]
        counts = {
            key: sum(step.status == key for step in matching)
            for key in ("OK", "EMPTY", "ERROR", "BLOCKED", "MISSING", "NO_SAMPLE")
        }
        rows.append({"operation": spec["key"], **counts})
    earnings = [
        {
            "step": step.name,
            "status": step.status,
            "error_code": step.issue.code if step.issue else "",
        }
        for step in steps
        if step.operation in {"mis.performance", "mis.payroll"}
    ]
    report_data = [
        {
            "step": step.name,
            "operation": step.operation,
            "output": step.output,
            "data_status": (step.details or {}).get("report_data_status", "UNASSESSED"),
            "text_characters": (step.details or {}).get("report_text_characters", 0),
        }
        for step in steps
        if step.operation in {"prq.order_report", "prq.text_report"}
    ]
    selected_path = root / "parsed/network/selected_profiles.json"
    selected = (
        json.loads(selected_path.read_text(encoding="utf-8")) if selected_path.is_file() else {}
    )
    unverified = [
        app
        for app, mode in selected.items()
        if mode.get("applied") and mode.get("certificate_verification") is False
    ]
    write_json_atomic(
        root / "coverage.json",
        {
            "schema_version": 1,
            "status": status,
            "operations": rows,
            "earnings_reports": earnings,
            "report_data": report_data,
            "excluded_write_operations": plan["excluded_write_operations"],
            "unverified_tls_services": unverified,
        },
    )
    lines = [
        f"VGHKS test result: {status}",
        "Debug files and the return ZIP are plain, unencrypted files; no password required.",
        "HTTPS without certificate verification: " + (", ".join(unverified) or "none applied"),
        "",
        "Login / SSO readiness (separate from positive-data query validation):",
    ]
    readiness_path = root / "parsed" / "readiness.json"
    if readiness_path.is_file():
        targets = json.loads(readiness_path.read_text(encoding="utf-8")).get("targets", [])
        lines.extend(
            f"  {item['target']}: {item['status']} {(item.get('issue') or {}).get('code', '')}".rstrip()
            for item in targets
        )
    lines += [
        "",
        "OK = query returned data (report metadata can qualify; text availability is listed below);",
        "EMPTY = successful empty response; ERROR = attempted but failed;",
        "BLOCKED = dependency/auth failure; MISSING = required input missing; NO_SAMPLE = no matching data from successful dependencies.",
        "",
        "Operation                           OK EMPTY ERROR BLOCKED MISSING NO_SAMPLE",
    ]
    lines.extend(
        f"{row['operation']:<36} "
        + " ".join(f"{row[key]:>5}" for key in ("OK", "EMPTY", "ERROR", "BLOCKED", "MISSING", "NO_SAMPLE"))
        for row in rows
    )
    if earnings:
        lines += ["", "Optional own-account earnings queries:"]
        lines.extend(
            f"  {row['step']}: {row['status']} {row['error_code']}".rstrip() for row in earnings
        )
        lines.append("  Full report data: parsed/earnings/")
    if report_data:
        lines += [
            "",
            "Report text availability (independent of PDF/JPG file downloads):",
            "  TEXT_AVAILABLE = captured report text; attachment failure does not revoke this result.",
            "  ATTACHMENT_ONLY / METADATA_ONLY = report content not yet captured as text.",
            "  These labels do not establish that all attachment contents were extracted.",
        ]
        lines.extend(
            f"  {row['step']}: {row['data_status']} ({row['text_characters']} characters); {row['output']}"
            for row in report_data
        )
    lines += [
        "",
        "Write operations excluded from this test: " + ", ".join(plan["excluded_write_operations"]),
    ]
    lines.extend(["", "Recorded failures (a failed probe may be recovered by a later mode):"])
    lines.extend(f"  {step.name}: {step.issue.code}" for step in steps if step.issue)
    workflow_path = root / "parsed/workflows/opd_soap_week/manifest.json"
    if workflow_path.is_file():
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        lines += [
            "",
            "Weekly OPD / same-day visits / arrange CATA:",
            f"  Status: {workflow['status']}",
        ]
        lines.extend(f"  {key}: {value}" for key, value in workflow["counts"].items())
        lines += [
            "  Full stage data: parsed/workflows/opd_soap_week/",
            "  Filtered matches: parsed/workflows/opd_soap_week/matches.json",
        ]
    eye_path = root / "parsed/workflows/ophthalmology_orders/manifest.json"
    if eye_path.is_file():
        eye = json.loads(eye_path.read_text(encoding="utf-8"))
        lines += ["", "Ophthalmology via order lists:", f"  Status: {eye['status']}"]
        lines.extend(f"  {term}: {counts}" for term, counts in eye.get("coverage", {}).items())
        lines += [
            "  Unexecuted orders: recorded and skipped, not proof of absent report data.",
            "  EMPTY JPG viewer: explicit no-data response, not a failed download.",
            "  PDF/JPG files preserve source content; binary availability does not imply text/numeric extraction.",
            "  Each exam has its own sample limit. Missing scenarios remain unverified.",
            "  Sources, per-order results and files: parsed/workflows/ophthalmology_orders/",
        ]
    (root / "RESULTS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
