"""Patient/case checks and saved history for the core/full live-test profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..core.capture import RawCaptureSink
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import AuthenticationError, ErrorInfo, error_info
from ..local_io import restrict_permissions
from ..models import (
    ClinicalOrder,
    MedicationHistoryFilter,
    NumericHistoryFilter,
    NumericReport,
    OrderHistoryFilter,
    SoapRecord,
    SurgeryHistoryFilter,
    VisitCase,
    to_jsonable,
)
from ..search import SoapSearch, evaluate_soap_search
from ..services.protocols import SDKProtocol
from .steps import (
    LIVE_TEST_MRN,
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestStep,
    _blocked_step,
    _run_step,
    _write_json,
)


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


def _fetch_case(
    sdk: SDKProtocol,
    case: VisitCase,
    *,
    index: int,
    profile: str,
    access_review_reason: str | None,
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
        operation=lambda: (
            sdk.records.get_soap(case, access_review_reason=access_review_reason)
            if access_review_reason else sdk.records.get_soap(case)
        ),
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
    restrict_permissions(records_path)


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
