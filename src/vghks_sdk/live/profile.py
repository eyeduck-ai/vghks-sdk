"""Core/full live-test orchestration and compatible workflow imports."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .._version import __version__
from ..core.capture import RawCaptureSink
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import AuthenticationError, ConfigurationError, ErrorInfo
from ..local_io import restrict_permissions, restrict_tree_permissions
from ..models import AuthCheckReport, VisitCase, VisitFilter, to_jsonable
from ..search import SoapSearch
from ..services.protocols import SDKProtocol
from ..workflows.patient_records import download_order_assets, select_asset_orders
from .patient import (
    _case_status as _case_status,
)
from .patient import (
    _fetch_case as _fetch_case,
)
from .patient import (
    _FetchedCase as _FetchedCase,
)
from .patient import (
    _history_row as _history_row,
)
from .patient import (
    _run_local_search as _run_local_search,
)
from .patient import (
    _run_patient_histories as _run_patient_histories,
)
from .patient import (
    _write_history_outputs as _write_history_outputs,
)
from .steps import (
    LIVE_TEST_MRN as LIVE_TEST_MRN,
)
from .steps import (
    LIVE_TEST_SCHEMA_VERSION as LIVE_TEST_SCHEMA_VERSION,
)
from .steps import (
    LiveTestResult as LiveTestResult,
)
from .steps import (
    LiveTestStep as LiveTestStep,
)
from .steps import (
    _blocked_step as _blocked_step,
)
from .steps import (
    _capture_range as _capture_range,
)
from .steps import (
    _overall_status as _overall_status,
)
from .steps import (
    _run_step as _run_step,
)
from .steps import (
    _target_ready as _target_ready,
)
from .steps import (
    _utc_now as _utc_now,
)
from .steps import (
    _warning as _warning,
)
from .steps import (
    _write_json as _write_json,
)


def run_live_test(
    sdk: SDKProtocol,
    *,
    output_dir: Path,
    test_mrn: str = LIVE_TEST_MRN,
    profile: str = "full",
    access_review_reason: str | None = None,
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
    restrict_permissions(parsed_dir, directory=True)
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
            operation=lambda: (
                sdk.records.get_visit_cases(
                    test_mrn, access_review_reason=access_review_reason
                ) if access_review_reason else sdk.records.get_visit_cases(test_mrn)
            ),
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
                access_review_reason=access_review_reason,
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
    restrict_tree_permissions(parsed_dir)
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
