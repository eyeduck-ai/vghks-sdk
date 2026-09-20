"""Focused live test: historical/case order lists and their report branches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import ErrorInfo
from ..local_io import write_json_atomic
from ..models import OrderHistoryFilter, to_jsonable
from ..order_status import classify_order_execution
from ..queries import run_query
from ..workflows.order_reports import collect_order_reports, matching_order_terms
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


def run_ophthalmology_test(
    sdk: Any,
    config: Any,
    *,
    plan: dict[str, Any],
    output_dir: Path,
    settings: Any = None,
    raw_capture: Any = None,
    diagnostics: Any = None,
    run_id: str = "",
) -> LiveTestResult:
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    workflow_root = root / "parsed/workflows/ophthalmology_orders"
    discovery = workflow_root / "stage1_discovery"
    write_json_atomic(root / "test_plan.json", plan)
    steps: list[LiveTestStep] = []
    common = {"root": root, "raw_capture": raw_capture, "diagnostics": diagnostics}
    if settings is not None:
        run_network_checks(
            settings,
            steps,
            only=("portal", "prq"),
            configure_connection=getattr(sdk, "configure_connection", None),
            allow_unverified_tls=config.allow_unverified_tls,
            **common,
        )
    readiness = independent_readiness(sdk, steps, only=("portal", "prq"), **common)
    fatal = not _target_ready(readiness, "portal")
    ready = not fatal and _target_ready(readiness, "prq")

    def query(name, key, arguments, destination):
        write_json_atomic(
            destination.with_suffix(".input.json"),
            {
                "operation": key,
                "arguments": to_jsonable(arguments),
            },
        )
        return _run_step(
            steps,
            name=name,
            operation_key=key,
            operation=lambda: run_query(sdk, key, **arguments),
            output_path=destination,
            classify=_classify,
            summarize=_counts,
            **common,
        )

    sources = {}
    if ready:
        # Test both recorded order selectors without fetching unrelated reports.
        # A failure in either history query still permits case discovery.
        for label, category in (("history_all", "*"), ("history_departmental", "OR")):
            rows, _ = query(
                f"ophthalmology.{label}",
                "prq.order_history",
                {
                    "mrn": config.test_mrn,
                    "filter": OrderHistoryFilter(category=category, order_date=config.order_date),
                },
                discovery / f"{label}.json",
            )
            if rows is not None:
                sources[label] = rows
        cases, _ = query(
            "ophthalmology.visits",
            "prq.visit_cases",
            {"mrn": config.test_mrn},
            discovery / "visits.json",
        )
        selected = config.visit_filter.select(cases or [])
        if config.visit_date:
            selected = [case for case in selected if case.visit_date == config.visit_date]
        target_cases = {
            (order.case_type, order.case_no)
            for rows in sources.values()
            for order in rows
            if matching_order_terms(order, config.asset_terms)
            and classify_order_execution(order.status) != "NOT_EXECUTED"
        }
        # Include the visits containing completed target tests, even when older
        # than the latest six visits. Remaining slots cover recent eye visits.
        selected.sort(
            key=lambda case: case.visit_date.isoformat() if case.visit_date else "", reverse=True
        )
        selected.sort(key=lambda case: (case.case_type, case.case_no) not in target_cases)
        write_json_atomic(
            discovery / "visit_selection.json",
            {
                "matching_visits": len(selected),
                "selected": to_jsonable(selected[: config.max_cases]),
                "omitted": max(0, len(selected) - config.max_cases),
                "policy": "target order visits first, then recent visits matching the configured filter",
            },
        )
        for index, case in enumerate(selected[: config.max_cases], 1):
            label = f"case_{index:04d}"
            rows, _ = query(
                f"ophthalmology.{label}",
                "prq.case_orders",
                {"case": case},
                discovery / f"{label}.json",
            )
            if rows is not None:
                sources[label] = rows
    else:
        steps.append(
            LiveTestStep(
                "ophthalmology.discovery",
                "BLOCKED",
                operation="prq.order_history",
                issue=ErrorInfo("READINESS_FAILED", "DEPENDENCY"),
            )
        )
    workflow = collect_order_reports(
        sdk,
        order_sources=sources,
        output_dir=workflow_root,
        terms=config.asset_terms,
        max_orders_per_term=config.max_items,
        download_assets=config.download_assets,
        query_runner=query,
        blocked_reason="PRQ_NOT_READY" if not ready else "",
    )
    steps.append(
        LiveTestStep(
            "ophthalmology.results",
            "BLOCKED"
            if not ready
            else "ERROR"
            if workflow.status == "INCOMPLETE"
            else "EMPTY"
            if workflow.status == "NO_MATCHING_ORDERS"
            else "OK",
            output=workflow.manifest_path.relative_to(root).as_posix(),
            details={"workflow_status": workflow.status, **workflow.counts},
            issue=ErrorInfo("ORDER_REPORT_BRANCH_FAILED", "DATA")
            if workflow.status == "INCOMPLETE"
            else None,
        )
    )
    write_json_atomic(root / "step_results.json", to_jsonable(steps))
    status = _overall_status(steps, fatal_auth=fatal)
    _write_coverage(root, plan, steps, status)
    path = root / "run_summary.json"
    write_json_atomic(
        path,
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "run_id": run_id,
            "profile": config.profile,
            "status": status,
            "steps": to_jsonable(steps),
            "test_plan": "test_plan.json",
            "coverage": "coverage.json",
            "ophthalmology_orders": workflow.manifest_path.relative_to(root).as_posix(),
        },
    )
    return LiveTestResult(status, tuple(steps), path, run_id)
