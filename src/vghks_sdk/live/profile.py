"""Unredacted validation workflows with an explicit test patient."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

from .._version import __version__
from ..core.capture import RawCaptureSink
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import (
    AuthenticationError,
    ConfigurationError,
    ErrorInfo,
    error_info,
)
from ..local_io import write_json_atomic
from ..models import (
    AuthCheckReport,
    ClinicalOrder,
    MedicationHistoryFilter,
    NumericHistoryFilter,
    NumericReport,
    OrderHistoryFilter,
    SoapRecord,
    SurgeryHistoryFilter,
    VisitCase,
    VisitFilter,
    to_jsonable,
)
from ..search import SoapSearch, evaluate_soap_search
from ..services.protocols import SDKProtocol
from ..workflows.patient_records import download_order_assets, select_asset_orders

LIVE_TEST_MRN = "0000000"
LIVE_TEST_SCHEMA_VERSION = 6
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LiveTestStep:
    name: str
    status: str
    output: str = ""
    issue: ErrorInfo | None = None
    error_id: str = ""
    details: Mapping[str, Any] | None = None
    operation: str = ""
    duration_ms: float = 0.0
    first_capture_id: str = ""
    last_capture_id: str = ""

    @property
    def error_code(self) -> str:
        return self.issue.code if self.issue is not None else ""


@dataclass(frozen=True, slots=True)
class LiveTestResult:
    status: str
    steps: tuple[LiveTestStep, ...]
    summary_path: Path
    run_id: str = ""

    @property
    def error_count(self) -> int:
        return sum(step.status in {"ERROR", "BLOCKED", "MISSING"} for step in self.steps)


@dataclass(slots=True)
class _FetchedCase:
    case: VisitCase
    detail: Any = None
    detail_status: str = "SKIPPED"
    detail_issue: ErrorInfo | None = None
    soap: SoapRecord | None = None
    soap_status: str = "SKIPPED"
    soap_issue: ErrorInfo | None = None
    numeric: NumericReport | None = None
    numeric_status: str = "SKIPPED"
    numeric_issue: ErrorInfo | None = None
    orders: list[ClinicalOrder] | None = None
    orders_status: str = "SKIPPED"
    orders_issue: ErrorInfo | None = None
    medications: Any = None
    medications_status: str = "SKIPPED"
    medications_issue: ErrorInfo | None = None
    consults: Any = None
    consults_status: str = "SKIPPED"
    consults_issue: ErrorInfo | None = None
    treatments: Any = None
    treatments_status: str = "SKIPPED"
    treatments_issue: ErrorInfo | None = None


def run_live_test(
    sdk: SDKProtocol,
    *,
    output_dir: Path,
    test_mrn: str = LIVE_TEST_MRN,
    profile: str = "full",
    doctor_card: str | None = None,
    probe_date: date | None = None,
    include_surgery: bool = False,
    include_unsigned: bool = False,
    range_start: date | None = None,
    range_end: date | None = None,
    visit_filter: VisitFilter | None = None,
    soap_search: SoapSearch | None = None,
    visit_date: date | None = None,
    order_date: date | None = None,
    download_assets: bool = True,
    asset_terms: tuple[str, ...] = ("Microsonography", "DBR"),
    all_matching_orders: bool = False,
    raw_capture: RawCaptureSink | None = None,
    diagnostics: DiagnosticRecorder | None = None,
    run_id: str = "",
) -> LiveTestResult:
    """Run core/full checks using an explicitly authorized test patient."""

    profile = str(profile).strip().lower()
    _validate_options(
        profile=profile,
        doctor_card=doctor_card,
        probe_date=probe_date,
        include_surgery=include_surgery,
        include_unsigned=include_unsigned,
        range_start=range_start,
        range_end=range_end,
    )
    visit_filter = visit_filter or VisitFilter(section_name_contains=("眼科",))
    if soap_search is not None:
        soap_search.compile()  # validate before the first SDK operation
    normalized_asset_terms = _normalize_asset_terms(asset_terms)
    if download_assets and not normalized_asset_terms:
        raise ConfigurationError("live-test asset download requires at least one search term")
    if all_matching_orders and not download_assets:
        raise ConfigurationError("live-test all-matching mode requires asset download")
    asset_terms = normalized_asset_terms
    root = output_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    parsed_dir = root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict(parsed_dir, directory=True)
    summary_path = root / "run_summary.json"
    started_at = _utc_now()
    steps: list[LiveTestStep] = []
    fatal_auth = False
    auth_report: AuthCheckReport | None = None

    core_targets = ["prq", "webmaas"]
    if doctor_card is not None and (profile == "full" or include_surgery):
        core_targets.append("oppl")
    if doctor_card is not None and (profile == "full" or include_unsigned):
        core_targets.append("audit")
    auth_only = tuple(core_targets)
    auth_report, auth_error = _run_step(
        steps,
        name="auth_check",
        operation=lambda: sdk.auth.check(only=auth_only),
        output_path=parsed_dir / "readiness.json",
        root=root,
        raw_capture=raw_capture,
        diagnostics=diagnostics,
        summarize=lambda report: {
            "targets": [target.target for target in report.targets],
            "status": report.status,
            "reauthenticated": report.reauthenticated,
        },
        classify=lambda report: "OK" if report.ok else "ERROR",
    )
    if isinstance(auth_error, AuthenticationError):
        fatal_auth = True
    if auth_report is None or not _target_ready(auth_report, "portal"):
        fatal_auth = True
    if profile == "core" and auth_report is not None:
        required_core = {"prq", "webmaas"}
        if any(
            target.target in required_core and target.status != "OK"
            for target in auth_report.targets
        ):
            # PRQ and WebMAAS are essential to the core smoke test. Optional
            # OPPL/Audit failures remain isolated and are reported later.
            fatal_auth = True

    visit_cases: list[VisitCase] | None = None

    if not fatal_auth and _target_ready(auth_report, "webmaas"):
        _demographics, error = _run_step(
            steps,
            name="patient_demographics",
            operation=lambda: sdk.patients.get_demographics(test_mrn),
            output_path=parsed_dir / "demographics.json",
            aliases=(parsed_dir / "patient_demographics.json",),
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
        )
        fatal_auth = isinstance(error, AuthenticationError)
        if not fatal_auth:
            _registration, error = _run_step(
                steps,
                name="registration_history",
                operation=lambda: sdk.patients.get_registration_history(test_mrn),
                output_path=parsed_dir / "registration.json",
                aliases=(parsed_dir / "registration_history.json",),
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                summarize=lambda rows: {"record_count": len(rows)},
            )
            fatal_auth = isinstance(error, AuthenticationError)
    elif not fatal_auth:
        _blocked_step(steps, "patient_demographics", "WEBMAAS_NOT_READY")
        _blocked_step(steps, "registration_history", "WEBMAAS_NOT_READY")

    if not fatal_auth and _target_ready(auth_report, "prq"):
        visit_cases, error = _run_step(
            steps,
            name="visit_cases",
            operation=lambda: sdk.records.get_visit_cases(test_mrn),
            output_path=parsed_dir / "visit_cases.json",
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
            summarize=lambda rows: {"record_count": len(rows)},
        )
        fatal_auth = isinstance(error, AuthenticationError)
    elif not fatal_auth:
        _blocked_step(steps, "visit_cases", "PRQ_NOT_READY")

    selected_cases: list[VisitCase] = []
    fetched: list[_FetchedCase] = []
    if not fatal_auth and visit_cases is not None:
        selected_cases = visit_filter.select(visit_cases)
        if visit_date is not None:
            selected_cases = [item for item in selected_cases if item.visit_date == visit_date]
        _write_json(parsed_dir / "matching_visit_cases.json", selected_cases)
        steps.append(
            LiveTestStep(
                name="matching_visit_cases",
                status="OK" if selected_cases else "MISSING",
                output="parsed/matching_visit_cases.json",
                issue=(None if selected_cases else ErrorInfo("NO_MATCHING_VISIT", "DATA")),
                details={"record_count": len(selected_cases)},
            )
        )
        cases_to_fetch = selected_cases if profile == "full" else selected_cases[:1]
        for index, case in enumerate(cases_to_fetch, start=1):
            item, error = _fetch_case(
                sdk,
                case,
                index=index,
                profile=profile,
                steps=steps,
                parsed_dir=parsed_dir,
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
            )
            fetched.append(item)
            if isinstance(error, AuthenticationError):
                fatal_auth = True
                break
        _write_history_outputs(parsed_dir, fetched)
    elif not fatal_auth and _target_ready(auth_report, "prq"):
        _blocked_step(steps, "matching_visit_cases", "CASE_LIST_FAILED")

    if not fatal_auth:
        _run_local_search(
            fetched,
            soap_search,
            steps=steps,
            output_path=parsed_dir / "soap_search.json",
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
        )

    candidate_orders = [
        order
        for item in fetched
        for order in (item.orders or [])
        if order_date is None or order.order_date.startswith(order_date.isoformat())
    ]
    if not fatal_auth and profile == "full":
        history_orders, history_error = _run_patient_histories(
            sdk,
            test_mrn=test_mrn,
            parsed_dir=parsed_dir,
            root=root,
            steps=steps,
            order_date=order_date,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
        )
        candidate_orders.extend(history_orders)
        fatal_auth = isinstance(history_error, AuthenticationError)

    if not fatal_auth and download_assets:
        orders_facade = getattr(sdk, "orders", None)
        if orders_facade is None:
            steps.append(LiveTestStep(name="order_assets", status="SKIPPED"))
        else:
            selected_asset_orders = select_asset_orders(
                candidate_orders,
                terms=asset_terms,
                all_matching=all_matching_orders,
            )
            normalized_names = tuple(
                _normalize_asset_text(order.name) for order in candidate_orders
            )
            missing_terms = [
                term
                for term in asset_terms
                if not any(_normalize_asset_text(term) in name for name in normalized_names)
            ]

            def fetch_assets() -> dict[str, Any]:
                manifest, count = download_order_assets(
                    sdk,
                    selected_orders=selected_asset_orders,
                    assets_dir=parsed_dir / "assets",
                )
                manifest["asset_count"] = count
                manifest["requested_terms"] = list(asset_terms)
                manifest["missing_terms"] = missing_terms
                asset_order_indexes = {
                    int(item["order_index"]) for item in manifest["assets"] if "order_index" in item
                }
                manifest["orders_without_assets"] = [
                    index
                    for index in range(1, len(manifest["selected_orders"]) + 1)
                    if index not in asset_order_indexes
                ]
                return manifest

            def classify_assets(value: Mapping[str, Any]) -> str:
                if value["issues"]:
                    return "ERROR"
                if (
                    value["missing_terms"]
                    or not value["selected_orders"]
                    or value["orders_without_assets"]
                ):
                    return "MISSING"
                return "OK"

            _assets, assets_error = _run_step(
                steps,
                name="order_assets",
                operation=fetch_assets,
                output_path=parsed_dir / "assets" / "asset_manifest.json",
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                summarize=lambda value: {
                    "selected_order_count": len(value["selected_orders"]),
                    "asset_count": value["asset_count"],
                    "error_count": len(value["issues"]),
                    "missing_term_count": len(value["missing_terms"]),
                    "orders_without_assets": len(value["orders_without_assets"]),
                },
                classify=classify_assets,
                missing_code="ORDER_ASSETS_MISSING",
                classified_error_code="ORDER_ASSET_PARTIAL_FAILURE",
            )
            fatal_auth = isinstance(assets_error, AuthenticationError)

    run_optional = profile == "full" or doctor_card is not None
    effective_start = range_start or probe_date
    effective_end = range_end or probe_date
    if not fatal_auth and run_optional and doctor_card and probe_date:
        if _target_ready(auth_report, "prq"):
            _, error = _run_step(
                steps,
                name="opd_patient_list",
                operation=lambda: sdk.opd.get_doctor_patients(doctor_card, probe_date),
                output_path=parsed_dir / "opd_patient_list.json",
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                summarize=lambda rows: {"record_count": len(rows)},
            )
            fatal_auth = isinstance(error, AuthenticationError)
        else:
            _blocked_step(steps, "opd_patient_list", "PRQ_NOT_READY")

    run_surgery = profile == "full" or include_surgery
    if not fatal_auth and run_surgery and doctor_card and effective_start and effective_end:
        if _target_ready(auth_report, "oppl"):
            _, error = _run_step(
                steps,
                name="surgery_schedule",
                operation=lambda: sdk.surgery.get_schedule(
                    doctor_card, effective_start, effective_end
                ),
                output_path=parsed_dir / "surgery_schedule.json",
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                summarize=lambda rows: {"record_count": len(rows)},
            )
            fatal_auth = isinstance(error, AuthenticationError)
        else:
            _blocked_step(steps, "surgery_schedule", "OPPL_NOT_READY")

    run_unsigned = profile == "full" or include_unsigned
    if not fatal_auth and run_unsigned and doctor_card and effective_start and effective_end:
        if _target_ready(auth_report, "audit"):
            _, error = _run_step(
                steps,
                name="unsigned_records",
                operation=lambda: sdk.audit.get_unsigned_records(
                    doctor_card, effective_start, effective_end
                ),
                output_path=parsed_dir / "unsigned_records.json",
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                summarize=lambda rows: {"record_count": len(rows)},
            )
            fatal_auth = isinstance(error, AuthenticationError)
        else:
            _blocked_step(steps, "unsigned_records", "AUDIT_NOT_READY")

    status = _overall_status(steps, fatal_auth=fatal_auth)
    result = LiveTestResult(
        status=status,
        steps=tuple(steps),
        summary_path=summary_path,
        run_id=run_id,
    )
    _restrict_tree(parsed_dir)
    _write_json(
        summary_path,
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "run_id": run_id,
            "warning": _warning(),
            "test_mrn": test_mrn,
            "profile": profile,
            "runtime": {
                "sdk_version": __version__,
            },
            "started_at": started_at,
            "completed_at": _utc_now(),
            "status": result.status,
            "steps": to_jsonable(result.steps),
            "capture_manifest": "capture_manifest.jsonl",
            "request_directory": "requests",
            "response_directory": "responses",
            "parsed_outputs": {
                "demographics": "parsed/demographics.json",
                "registration": "parsed/registration.json",
                "visit_cases": "parsed/visit_cases.json",
                "case_detail": "parsed/latest_case_detail.json"
                if profile == "core"
                else "parsed/visit_history.json",
                "soap": "parsed/latest_soap.json"
                if profile == "core"
                else "parsed/visit_history.json",
                "numeric": "parsed/latest_numeric.json"
                if profile == "core"
                else "parsed/visit_history.json",
                "soap_search": "parsed/soap_search.json",
                "order_history": "parsed/order_history.json" if profile == "full" else None,
                "departmental_order_history": (
                    "parsed/order_history_departmental.json"
                    if profile == "full" and order_date is None
                    else None
                ),
                "order_category_comparison": (
                    "parsed/order_category_comparison.json"
                    if profile == "full" and order_date is None
                    else None
                ),
                "medication_history": (
                    "parsed/medication_history.json" if profile == "full" else None
                ),
                "numeric_history": "parsed/numeric_history.json" if profile == "full" else None,
                "surgery_history": "parsed/surgery_history.json" if profile == "full" else None,
                "assets": "parsed/assets/asset_manifest.json" if download_assets else None,
            },
        },
    )
    return result


def _fetch_case(
    sdk: SDKProtocol,
    case: VisitCase,
    *,
    index: int,
    profile: str,
    steps: list[LiveTestStep],
    parsed_dir: Path,
    root: Path,
    raw_capture: RawCaptureSink | None,
    diagnostics: DiagnosticRecorder | None,
) -> tuple[_FetchedCase, BaseException | None]:
    item = _FetchedCase(case=case)
    prefix = "latest" if profile == "core" else f"visit_{index:04d}"
    history_dir = parsed_dir / "visit_history" / "cases" / f"{index:04d}"
    if profile == "full":
        history_dir.mkdir(parents=True, exist_ok=True)

    detail_callable = getattr(sdk.records, "get_case_detail", None)
    if callable(detail_callable):
        detail_path = (
            parsed_dir / "latest_case_detail.json"
            if profile == "core"
            else history_dir / "case_detail.json"
        )
        item.detail, detail_error = _run_step(
            steps,
            name=f"{prefix}_case_detail",
            operation=lambda: detail_callable(case),
            output_path=detail_path,
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
        )
        item.detail_status = "ERROR" if detail_error else "OK"
        item.detail_issue = error_info(detail_error) if detail_error else None
        if isinstance(detail_error, AuthenticationError):
            return item, detail_error
    else:  # lightweight protocol fakes used by offline callers
        item.detail_status = "SKIPPED"
        steps.append(LiveTestStep(name=f"{prefix}_case_detail", status="SKIPPED"))

    soap_path = parsed_dir / "latest_soap.json" if profile == "core" else history_dir / "soap.json"
    item.soap, soap_error = _run_step(
        steps,
        name=f"{prefix}_soap",
        operation=lambda: sdk.records.get_soap(case),
        output_path=soap_path,
        root=root,
        raw_capture=raw_capture,
        diagnostics=diagnostics,
        classify=lambda soap: "OK" if soap.blocks else "MISSING",
        missing_code="SOAP_MISSING",
    )
    if soap_error:
        item.soap_status = "ERROR"
        item.soap_issue = error_info(soap_error)
    elif item.soap is None or not item.soap.blocks:
        item.soap_status = "MISSING"
        item.soap_issue = ErrorInfo("SOAP_MISSING", "DATA")
    else:
        item.soap_status = "OK"
    if isinstance(soap_error, AuthenticationError):
        return item, soap_error

    numeric_path = (
        parsed_dir / "latest_numeric.json" if profile == "core" else history_dir / "numeric.json"
    )
    item.numeric, numeric_error = _run_step(
        steps,
        name=f"{prefix}_numeric",
        operation=lambda: sdk.records.get_numeric_report(case),
        output_path=numeric_path,
        root=root,
        raw_capture=raw_capture,
        diagnostics=diagnostics,
        classify=lambda report: "OK" if report.tables else "MISSING",
        missing_code="NUMERIC_MISSING",
    )
    if numeric_error:
        item.numeric_status = "ERROR"
        item.numeric_issue = error_info(numeric_error)
    elif item.numeric is None or not item.numeric.tables:
        item.numeric_status = "MISSING"
        item.numeric_issue = ErrorInfo("NUMERIC_MISSING", "DATA")
    else:
        item.numeric_status = "OK"
    if isinstance(numeric_error, AuthenticationError):
        return item, numeric_error

    clinical_calls = (
        (
            "orders",
            getattr(getattr(sdk, "orders", None), "get_case_orders", None),
            "case_orders",
        ),
        (
            "medications",
            getattr(getattr(sdk, "medications", None), "get_case_medications", None),
            "case_medications",
        ),
        ("consults", getattr(sdk.records, "get_consults", None), "consults"),
        ("treatments", getattr(sdk.records, "get_treatments", None), "treatments"),
    )
    for field_name, callback, filename in clinical_calls:
        if not callable(callback):
            steps.append(LiveTestStep(name=f"{prefix}_{field_name}", status="SKIPPED"))
            continue
        output_path = (
            parsed_dir / f"latest_{filename}.json"
            if profile == "core"
            else history_dir / f"{filename}.json"
        )
        value, clinical_error = _run_step(
            steps,
            name=f"{prefix}_{field_name}",
            operation=lambda callback=callback: callback(case),
            output_path=output_path,
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
            summarize=lambda rows: {"record_count": len(rows)},
        )
        setattr(item, field_name, value)
        setattr(item, f"{field_name}_status", "ERROR" if clinical_error else "OK")
        setattr(
            item,
            f"{field_name}_issue",
            error_info(clinical_error) if clinical_error else None,
        )
        if isinstance(clinical_error, AuthenticationError):
            return item, clinical_error
    return item, None


def _run_local_search(
    fetched: list[_FetchedCase],
    search: SoapSearch | None,
    *,
    steps: list[LiveTestStep],
    output_path: Path,
    root: Path,
    raw_capture: RawCaptureSink | None,
    diagnostics: DiagnosticRecorder | None,
) -> None:
    if search is None:
        payload = {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "status": "SKIPPED",
            "search": None,
            "results": [],
        }
        _write_json(output_path, payload)
        steps.append(
            LiveTestStep(
                name="soap_search",
                status="SKIPPED",
                output=output_path.relative_to(root).as_posix(),
            )
        )
        return

    def operation() -> dict[str, Any]:
        matcher = search.compile()
        results: list[dict[str, Any]] = []
        for item in fetched:
            if item.soap is None:
                results.append(
                    {
                        "case": to_jsonable(item.case),
                        "status": "MISSING" if item.soap_status == "MISSING" else "ERROR",
                        "issue": to_jsonable(
                            item.soap_issue or ErrorInfo("SOAP_UNAVAILABLE", "DATA")
                        ),
                        "text_length": 0,
                        "evidence": None,
                    }
                )
                continue
            evaluation = evaluate_soap_search(item.soap, search, compiled=matcher)
            results.append(
                {
                    "case": to_jsonable(item.case),
                    "status": evaluation.status,
                    "issue": (
                        to_jsonable(ErrorInfo(evaluation.error_code, "DATA"))
                        if evaluation.error_code
                        else None
                    ),
                    "text_length": evaluation.text_length,
                    "evidence": to_jsonable(evaluation.evidence),
                }
            )
        errors = sum(row["status"] == "ERROR" for row in results)
        matches = sum(row["status"] == "MATCHED" for row in results)
        missing = sum(row["status"] == "MISSING" for row in results)
        if errors:
            status = "ERROR"
        elif not results or missing == len(results):
            status = "MISSING"
        else:
            status = "OK"
        return {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "status": status,
            "search": to_jsonable(search),
            "result_count": len(results),
            "match_count": matches,
            "error_count": errors,
            "results": results,
        }

    _run_step(
        steps,
        name="soap_search",
        operation=operation,
        output_path=output_path,
        root=root,
        raw_capture=raw_capture,
        diagnostics=diagnostics,
        summarize=lambda value: {
            "result_count": value["result_count"],
            "match_count": value["match_count"],
            "error_count": value["error_count"],
        },
        classify=lambda value: str(value["status"]),
        missing_code="NO_SEARCHABLE_SOAP",
    )


def _run_patient_histories(
    sdk: SDKProtocol,
    *,
    test_mrn: str = LIVE_TEST_MRN,
    parsed_dir: Path,
    root: Path,
    steps: list[LiveTestStep],
    order_date: date | None,
    raw_capture: RawCaptureSink | None,
    diagnostics: DiagnosticRecorder | None,
) -> tuple[list[ClinicalOrder], BaseException | None]:
    orders: list[ClinicalOrder] = []
    all_order_rows: list[ClinicalOrder] | None = None
    calls = (
        (
            "order_history",
            getattr(getattr(sdk, "orders", None), "get_order_history", None),
            lambda callback: callback(
                test_mrn,
                OrderHistoryFilter(lookback_days="all", order_date=order_date),
            ),
        ),
        (
            "medication_history",
            getattr(getattr(sdk, "medications", None), "get_medication_history", None),
            lambda callback: callback(
                test_mrn,
                MedicationHistoryFilter(lookback_days="all"),
            ),
        ),
        (
            "numeric_history",
            getattr(sdk.records, "get_numeric_history", None),
            lambda callback: callback(test_mrn, NumericHistoryFilter(lookback_days="all")),
        ),
        (
            "surgery_history",
            getattr(sdk.records, "get_surgery_history", None),
            lambda callback: callback(test_mrn, SurgeryHistoryFilter(lookback_days="all")),
        ),
    )
    for name, callback, invoke in calls:
        if not callable(callback):
            steps.append(LiveTestStep(name=name, status="SKIPPED"))
            continue
        value, error = _run_step(
            steps,
            name=name,
            operation=lambda callback=callback, invoke=invoke: invoke(callback),
            output_path=parsed_dir / f"{name}.json",
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
            summarize=lambda result: {
                "record_count": len(result) if hasattr(result, "__len__") else len(result.tables)
            },
        )
        if name == "order_history" and isinstance(value, list):
            all_order_rows = [item for item in value if isinstance(item, ClinicalOrder)]
            orders.extend(all_order_rows)
        if isinstance(error, AuthenticationError):
            return orders, error

    order_callback = getattr(getattr(sdk, "orders", None), "get_order_history", None)
    if order_date is not None:
        steps.append(LiveTestStep(name="order_history_category_comparison", status="SKIPPED"))
    elif callable(order_callback):
        departmental_rows, error = _run_step(
            steps,
            name="order_history_departmental",
            operation=lambda: order_callback(
                test_mrn,
                OrderHistoryFilter(lookback_days="all", category="DEPARTMENTAL"),
            ),
            output_path=parsed_dir / "order_history_departmental.json",
            root=root,
            raw_capture=raw_capture,
            diagnostics=diagnostics,
            summarize=lambda result: {"record_count": len(result)},
        )
        if isinstance(error, AuthenticationError):
            return orders, error
        if all_order_rows is None or not isinstance(departmental_rows, list):
            _blocked_step(
                steps,
                "order_history_category_comparison",
                "ORDER_HISTORY_COMPARISON_UNAVAILABLE",
            )
        else:
            all_identities = {item.identity for item in all_order_rows}
            departmental_identities = {
                item.identity for item in departmental_rows if isinstance(item, ClinicalOrder)
            }
            _run_step(
                steps,
                name="order_history_category_comparison",
                operation=lambda: {
                    "all_count": len(all_order_rows),
                    "departmental_count": len(departmental_rows),
                    "identity_sets_differ": all_identities != departmental_identities,
                },
                output_path=parsed_dir / "order_category_comparison.json",
                root=root,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                classify=lambda value: "OK" if value["identity_sets_differ"] else "ERROR",
                classified_error_code="ORDER_CATEGORY_RESULTS_NOT_DISTINCT",
            )
    return orders, None


def _write_history_outputs(parsed_dir: Path, fetched: list[_FetchedCase]) -> None:
    rows = [_history_row(item) for item in fetched]
    _write_json(
        parsed_dir / "visit_history.json",
        {"schema_version": 3, "records": rows},
    )
    records_path = parsed_dir / "visit_history_records.jsonl"
    with records_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    _restrict(records_path)


def _history_row(item: _FetchedCase) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "mrn": item.case.mrn,
        "case": to_jsonable(item.case),
        "status": _case_status(item),
        "case_detail": to_jsonable(item.detail),
        "case_detail_status": item.detail_status,
        "soap": to_jsonable(item.soap),
        "soap_status": item.soap_status,
        "numeric_report": to_jsonable(item.numeric),
        "numeric_status": item.numeric_status,
        "orders": to_jsonable(item.orders),
        "orders_status": item.orders_status,
        "medications": to_jsonable(item.medications),
        "medications_status": item.medications_status,
        "consults": to_jsonable(item.consults),
        "consults_status": item.consults_status,
        "treatments": to_jsonable(item.treatments),
        "treatments_status": item.treatments_status,
        "issues": [
            to_jsonable(value)
            for value in (
                item.detail_issue,
                item.soap_issue,
                item.numeric_issue,
                item.orders_issue,
                item.medications_issue,
                item.consults_issue,
                item.treatments_issue,
            )
            if value is not None
        ],
    }


def _case_status(item: _FetchedCase) -> str:
    statuses = {
        item.detail_status,
        item.soap_status,
        item.numeric_status,
        item.orders_status,
        item.medications_status,
        item.consults_status,
        item.treatments_status,
    }
    if "ERROR" in statuses:
        return "ERROR" if statuses <= {"ERROR", "SKIPPED"} else "PARTIAL_ERROR"
    if "MISSING" in statuses:
        return "MISSING"
    return "OK"


def _run_step(
    steps: list[LiveTestStep],
    *,
    name: str,
    operation: Callable[[], T],
    output_path: Path | None,
    root: Path,
    raw_capture: RawCaptureSink | None = None,
    diagnostics: DiagnosticRecorder | None = None,
    aliases: tuple[Path, ...] = (),
    summarize: Callable[[T], Mapping[str, Any]] | None = None,
    classify: Callable[[T], str] | None = None,
    missing_code: str = "MISSING",
    classified_error_code: str = "",
    operation_key: str = "",
) -> tuple[T | None, BaseException | None]:
    started = monotonic()
    first = getattr(raw_capture, "capture_count", 0) + 1
    print(f"START {name}", flush=True)
    if raw_capture is not None:
        raw_capture.set_live_step(name)
    try:
        value = operation()
        if output_path is not None:
            _write_json(output_path, value)
            for alias in aliases:
                _write_json(alias, value)
        details = dict(summarize(value)) if summarize is not None else {}
        status = classify(value) if classify is not None else "OK"
        code = (
            missing_code
            if status == "MISSING"
            else classified_error_code
            if status == "ERROR"
            else ""
        )
        step = LiveTestStep(
            name=name,
            status=status,
            output=(output_path.relative_to(root).as_posix() if output_path is not None else ""),
            issue=(
                next((target.issue for target in value.targets if target.status == "ERROR"), None)
                if isinstance(value, AuthCheckReport) and not value.ok
                else ErrorInfo(code, "DATA")
                if code
                else None
            ),
            details=details,
            operation=operation_key,
            duration_ms=round((monotonic() - started) * 1000, 3),
            **_capture_range(raw_capture, first),
        )
        steps.append(step)
        if diagnostics is not None:
            diagnostics.record_probe_step(
                name=name,
                status=status,
                error_code_value=code,
                details=details,
            )
        return value, None
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        issue = error_info(exc)
        error_id = raw_capture.record_error(error=exc, step=name) if raw_capture is not None else ""
        step = LiveTestStep(
            name=name,
            status="ERROR",
            issue=issue,
            error_id=error_id,
            operation=operation_key,
            duration_ms=round((monotonic() - started) * 1000, 3),
            **_capture_range(raw_capture, first),
        )
        steps.append(step)
        if diagnostics is not None:
            diagnostics.record_probe_step(name=name, status="ERROR", error=exc)
        return None, exc
    finally:
        if raw_capture is not None:
            raw_capture.set_live_step(None)
        # Persist completed work after every unit, including when the next
        # operation is interrupted. No response values enter this journal.
        write_json_atomic(root / "step_results.json", to_jsonable(steps))
        if steps and steps[-1].name == name:
            last = steps[-1]
            line = f"{last.status} {name} ({last.duration_ms:.0f} ms) {last.error_code}".rstrip()
            print(line, flush=True)
            with (root / "progress.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def _capture_range(capture: RawCaptureSink | None, first: int) -> dict[str, str]:
    last = getattr(capture, "capture_count", 0)
    if last < first:
        return {}
    return {"first_capture_id": f"{first:06d}", "last_capture_id": f"{last:06d}"}


def _target_ready(report: AuthCheckReport | None, key: str) -> bool:
    if report is None:
        return False
    target = next((item for item in report.targets if item.target == key), None)
    # Small offline SDK fakes historically returned only Portal while reporting
    # overall OK. A real readiness report always includes requested targets.
    return report.ok if target is None else target.status == "OK"


def _blocked_step(steps: list[LiveTestStep], name: str, code: str) -> None:
    steps.append(
        LiveTestStep(
            name=name,
            status="BLOCKED",
            issue=ErrorInfo(code, "DEPENDENCY"),
        )
    )


def _overall_status(steps: list[LiveTestStep], *, fatal_auth: bool) -> str:
    if fatal_auth:
        authentication = [
            step
            for step in steps
            if step.name == "auth_check" or step.name.startswith("auth_check.")
        ]
        failures = authentication or steps
        if any(step.issue and step.issue.category == "NETWORK" for step in failures):
            return "CONNECTIVITY_FAILED"
        if any(step.issue and step.issue.category == "HTTP" for step in failures):
            return "HTTP_FAILED"
        return "AUTHENTICATION_FAILED"
    ready = {
        step.name.removeprefix("auth_check.")
        for step in steps
        if step.name.startswith("auth_check.") and step.status == "OK"
    }
    if any(
        step.status in {"ERROR", "BLOCKED", "MISSING"}
        and not (
            step.name.startswith("network.")
            and step.name.split(".")[1] in ready
            and not step.name.endswith(".configure")
        )
        for step in steps
    ):
        return "COMPLETED_WITH_ERRORS"
    if any(step.status == "NO_SAMPLE" for step in steps):
        return "COMPLETED_WITH_GAPS"
    return "OK"


def _validate_options(
    *,
    profile: str,
    doctor_card: str | None,
    probe_date: date | None,
    include_surgery: bool,
    include_unsigned: bool,
    range_start: date | None,
    range_end: date | None,
) -> None:
    if profile not in {"core", "full"}:
        raise ConfigurationError("live-test profile must be core or full")
    if (doctor_card is None) != (probe_date is None):
        raise ConfigurationError("live-test requires --doctor-card and --date together")
    if (include_surgery or include_unsigned) and doctor_card is None:
        raise ConfigurationError(
            "live-test surgery and unsigned checks require --doctor-card and --date"
        )
    if (range_start is None) != (range_end is None):
        raise ConfigurationError("live-test requires --start and --end together")
    if range_start and range_end and range_end < range_start:
        raise ConfigurationError("live-test end date must not be before start date")
    if (range_start is not None or range_end is not None) and doctor_card is None:
        raise ConfigurationError("live-test date range requires --doctor-card and --date")


def _write_json(path: Path, value: Any) -> None:
    write_json_atomic(path, to_jsonable(value))
    _restrict(path)


def _restrict(path: Path, *, directory: bool = False) -> None:
    mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR if directory else stat.S_IRUSR | stat.S_IWUSR
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def _restrict_tree(root: Path) -> None:
    _restrict(root, directory=True)
    for path in root.rglob("*"):
        _restrict(path, directory=path.is_dir())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warning() -> str:
    return (
        "UNREDACTED: this bundle contains credentials, cookies, tokens, patient "
        "data, SOAP, and replayable authenticated session state. ZIP is not encrypted."
    )


def _normalize_asset_terms(values: tuple[str, ...]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = unicodedata.normalize("NFKC", str(value)).strip()
        identity = term.casefold()
        if identity and identity not in seen:
            seen.add(identity)
            output.append(term)
    return tuple(output)


def _normalize_asset_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()
