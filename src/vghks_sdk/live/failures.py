"""Bounded live acquisition observations plus separately identified simulations."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

from ..acquisition import acquire
from ..core.errors import ErrorInfo, NotFoundError, SDKError
from ..core.operations import OPERATIONS
from ..local_io import write_json_atomic
from ..models import (
    AcquisitionResult,
    ClinicalOrder,
    OrderReport,
    PasswordStatus,
    VisitFilter,
    to_jsonable,
)
from ..order_status import classify_order_execution
from ..queries import query_spec
from .atomic import _write_coverage
from .auth_edge_simulation import AUTH_EDGE_SCENARIOS, run_auth_edge_scenario
from .auth_edges import run_before_login_checks
from .failure_simulation import (
    DATA_SCENARIOS,
    FAILURE_SCENARIOS,
    run_data_scenario,
    run_failure_scenario,
)
from .login_simulation import SCENARIOS, run_scenario
from .preflight import independent_readiness
from .presets import FAILURE_QUERIES
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)


def build_failure_plan(config) -> dict:
    return {
        "schema_version": 1,
        "profile": "failures",
        "auth_targets": ["portal", "prq", "webmaas"],
        "max_cases": config.max_cases,
        "max_items_per_operation": config.max_items,
        "continue_independent_checks": True,
        "negative_password_post_limit": config.login_negative_attempts,
        "negative_tests_before_correct_login": True,
        "anonymous_query_password_post_limit": 0,
        "password_policy": ["NO_NOTICE", "EXPIRING", "CHANGE_REQUIRED"],
        "simulated_scenarios": [
            *["login." + item.name for item in SCENARIOS],
            *["transport." + item.name for item in FAILURE_SCENARIOS],
            *["data." + name for name in DATA_SCENARIOS],
            *["auth_edge." + name for name in AUTH_EDGE_SCENARIOS],
        ],
        "empty_results": "Observe actual responses; do not invent a nonexistent patient.",
        "session_recovery": "Local cookie loss only; natural TTL expiry is not claimed.",
        "excluded_write_operations": [spec.key for spec in OPERATIONS if spec.mutates],
        "conditional_write": "Recorded PRQ care-reason review may be submitted once by the SDK.",
        "operations": [
            {
                "key": key,
                "sdk_method": query_spec(key).sdk_method,
                "scope": query_spec(key).scope,
                "inputs": list(query_spec(key).inputs),
                "dependencies": list(query_spec(key).dependencies),
                "requested": True,
            }
            for key in FAILURE_QUERIES
        ],
    }


def run_failure_test(
    sdk,
    config,
    *,
    output_dir: Path,
    credentials=None,
    settings=None,
    raw_capture=None,
    diagnostics=None,
    run_id: str = "",
) -> LiveTestResult:
    root = output_dir
    plan = build_failure_plan(config)
    write_json_atomic(root / "test_plan.json", plan)
    steps: list[LiveTestStep] = []
    capture = {"raw_capture": raw_capture, "diagnostics": diagnostics}
    simulated: list[dict] = []
    observations: list[dict] = []
    stopped = False

    simulations = [
        *[("login." + item.name, lambda item=item: run_scenario(item)) for item in SCENARIOS],
        *[
            ("transport." + item.name, lambda item=item: run_failure_scenario(item))
            for item in FAILURE_SCENARIOS
        ],
        *[("data." + name, lambda name=name: run_data_scenario(name)) for name in DATA_SCENARIOS],
        *[("auth_edge." + name, lambda name=name: run_auth_edge_scenario(name))
          for name in AUTH_EDGE_SCENARIOS],
    ]
    for name, action in simulations:
        value, _ = _run_step(
            steps,
            name="failures.simulated." + name,
            operation=action,
            root=root,
            output_path=root / "parsed/failures/simulated" / (name + ".json"),
            classify=lambda row: "OK" if row["passed"] else "ERROR",
            classified_error_code="FAILURE_SIMULATION_FAILED",
            summarize=lambda row: {"evidence": "SIMULATED", "passed": row["passed"]},
            **capture,
        )
        if value is not None:
            simulated.append(value)
    write_json_atomic(root / "parsed/failures/simulations.json", simulated)

    login_allowed = run_before_login_checks(
        config, steps, credentials=credentials, settings=settings, root=root, **capture
    )
    if login_allowed:
        ready = independent_readiness(
            sdk, steps, root=root, only=("portal", "prq", "webmaas"), **capture
        )
    else:
        ready = None
        steps.append(LiveTestStep(name="auth_check.portal", status="BLOCKED",
                                  issue=ErrorInfo("NEGATIVE_LOGIN_OUTCOME_UNCERTAIN", "DEPENDENCY"),
                                  details={"attempted": False}))
    portal_failed = not _target_ready(ready, "portal")

    def password_observation(stage):
        value = getattr(sdk.auth, "password_status", None)
        if value is not None:
            write_json_atomic(root / f"parsed/failures/password-status-{stage}.json", value)

    password_observation("login")

    def unavailable(name: str, key: str, *, failed: bool, reason: str):
        steps.append(
            LiveTestStep(
                name="failures.live." + name,
                operation=key,
                status="BLOCKED" if failed else "NO_SAMPLE",
                issue=ErrorInfo(reason, "DEPENDENCY" if failed else "DATA"),
                details={"evidence": "LIVE", "attempted": False},
            )
        )

    def read(name: str, key: str, **inputs) -> AcquisitionResult | None:
        nonlocal stopped
        if stopped or not _target_ready(ready, query_spec(key).app):
            unavailable(
                name,
                key,
                failed=True,
                reason="AUTHENTICATION_STOPPED" if stopped else "APPLICATION_NOT_READY",
            )
            return None
        error_ids: list[str] = []

        def call():
            try:
                return sdk.queries.run(key, **inputs)
            except SDKError as exc:
                if raw_capture is not None and not isinstance(exc, NotFoundError):
                    error_ids.append(
                        raw_capture.record_error(error=exc, step="failures.live." + name)
                    )
                raise

        result, _ = _run_step(
            steps,
            name="failures.live." + name,
            operation=lambda: acquire(call),
            root=root,
            output_path=root / "parsed/failures/live" / (name + ".json"),
            operation_key=key,
            classify=lambda row: row.status if row.status in {"ERROR", "EMPTY"} else "OK",
            summarize=_summary,
            **capture,
        )
        if result is None:
            return None
        if result.error is not None and result.status == "ERROR":
            steps[-1] = replace(
                steps[-1], issue=result.error, error_id=error_ids[-1] if error_ids else ""
            )
            stopped = result.error.category == "AUTHENTICATION"
        observation = {"step": name, "operation": key, **_summary(result)}
        observations.append(observation)
        write_json_atomic(root / "parsed/failures/observations.json", observations)
        write_json_atomic(root / "step_results.json", to_jsonable(steps))
        return result

    read("basic_info", "webmaas.basic_info", mrn=config.test_mrn)
    read("registrations", "webmaas.registration_query", mrn=config.test_mrn)
    inputs = {"mrn": config.test_mrn}
    if config.access_review_reason:
        inputs["access_review_reason"] = config.access_review_reason
    visits = read("visits", "prq.visit_cases", **inputs)
    cases = (
        VisitFilter(all_sections=True).select(visits.value or []) if visits and visits.ok else []
    )
    cases = cases[: config.max_cases]
    reports, studies, details = [], [], []
    orders_failed = details_failed = reports_failed = False
    if not cases:
        for name, key in (
            ("soap", "prq.soap"),
            ("numeric", "prq.numeric"),
            ("orders", "prq.case_orders"),
        ):
            unavailable(
                name,
                key,
                failed=visits is None or not visits.ok,
                reason="VISIT_QUERY_FAILED"
                if visits is None or not visits.ok
                else "NO_OUTPATIENT_SAMPLE",
            )
    for index, case in enumerate(cases, 1):
        case_inputs = {"case": case}
        if config.access_review_reason:
            case_inputs["access_review_reason"] = config.access_review_reason
        read(f"case.{index}.soap", "prq.soap", **case_inputs)
        read(f"case.{index}.numeric", "prq.numeric", case=case)
        orders = read(f"case.{index}.orders", "prq.case_orders", case=case)
        if orders is None or not orders.ok:
            orders_failed = True
            continue
        for order in orders.value or []:
            if classify_order_execution(order.status) == "NOT_EXECUTED":
                continue
            if order.report_ref is not None:
                reports.append(order.report_ref)
            elif order.detail_ref is not None:
                details.append(order.detail_ref)
            if order.pacs_ref is not None:
                studies.append(order.pacs_ref)
    if not details:
        unavailable(
            "detail",
            "prq.order_detail",
            failed=orders_failed or visits is None or not visits.ok,
            reason="ORDER_QUERY_FAILED" if orders_failed else "NO_DETAIL_REFERENCE",
        )
    for index, ref in enumerate(tuple(dict.fromkeys(details))[: config.max_items], 1):
        detail = read(f"detail.{index}", "prq.order_detail", ref=ref)
        if detail is None or not detail.ok:
            details_failed = True
        elif detail.value is not None:
            reports.extend(detail.value.report_refs)
            studies.extend(detail.value.pacs_refs)
    if not reports:
        unavailable(
            "report",
            "prq.order_report",
            failed=orders_failed or details_failed or visits is None or not visits.ok,
            reason="ORDER_QUERY_FAILED"
            if orders_failed
            else "DETAIL_QUERY_FAILED"
            if details_failed
            else "NO_REPORT_REFERENCE",
        )
    for index, ref in enumerate(tuple(dict.fromkeys(reports))[: config.max_items], 1):
        report = read(f"report.{index}", "prq.order_report", ref=ref)
        if report is None or not report.ok:
            reports_failed = True
        elif report.value is not None:
            studies.extend(report.value.pacs_refs)
    if not studies:
        unavailable(
            "pacs",
            "prq.pacs_study",
            failed=orders_failed
            or details_failed
            or reports_failed
            or visits is None
            or not visits.ok,
            reason="ORDER_QUERY_FAILED"
            if orders_failed
            else "DETAIL_QUERY_FAILED"
            if details_failed
            else "REPORT_QUERY_FAILED"
            if reports_failed
            else "NO_PACS_REFERENCE",
        )
    for index, ref in enumerate(tuple(dict.fromkeys(studies))[: config.max_items], 1):
        read(f"pacs.{index}", "prq.pacs_study", ref=ref)

    catalog = read("catalog", "prq.upload_types")
    recovery = {
        "evidence": "LIVE_COOKIE_LOSS",
        "natural_ttl_verified": False,
        "recovery_observed": False,
    }
    if catalog and catalog.ok and not stopped:

        def recover():
            generation = sdk._runtime.auth.generation
            sdk._runtime.transport.reset_cookies()
            recovery["generation_before"] = generation
            try:
                value = sdk.records.get_upload_types()
                recovery["record_count"] = len(value)
                recovery["recovery_observed"] = sdk._runtime.auth.generation > generation
                return recovery
            finally:
                recovery["generation_after"] = sdk._runtime.auth.generation
                password_observation("cookie-loss")
                write_json_atomic(root / "parsed/failures/cookie_loss.json", recovery)

        _run_step(
            steps,
            name="failures.live.cookie_loss",
            operation=recover,
            root=root,
            output_path=root / "parsed/failures/cookie_loss.json",
            classify=lambda row: "OK" if row["recovery_observed"] else "NO_SAMPLE",
            summarize=lambda row: row,
            **capture,
        )
    else:
        unavailable("cookie_loss", "prq.upload_types", failed=True, reason="CATALOG_QUERY_FAILED")

    policy = getattr(sdk.auth, "password_status", None)
    if not isinstance(policy, PasswordStatus):
        policy = PasswordStatus()
    notices = [policy]
    for filename in ("password-status-login.json", "negative-password-status.json"):
        policy_path = root / "parsed/failures" / filename
        if policy_path.is_file():
            notices.append(PasswordStatus(**json.loads(policy_path.read_text(encoding="utf-8"))))
    policy = next((notice for notice in notices if notice.status == "CHANGE_REQUIRED"),
                  next((notice for notice in notices if notice.status == "EXPIRING"), policy))
    steps.append(LiveTestStep(
        name="failures.live.password_policy", operation="auth.password_status",
        status="NO_SAMPLE" if policy.status == "NO_NOTICE" else "OK",
        details={"evidence": "LIVE", "password_status": to_jsonable(policy),
                 "observed_code": policy.code},
    ))
    if policy.status == "EXPIRING":
        print(f"PASSWORD NOTICE: remaining days = {policy.remaining_days}", flush=True)
    elif policy.status == "CHANGE_REQUIRED":
        print("PASSWORD CHANGE REQUIRED: change it through the hospital portal.", flush=True)
    status = _overall_status(steps, fatal_auth=portal_failed)
    summary = root / "run_summary.json"
    write_json_atomic(
        summary,
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "run_id": run_id,
            "profile": "failures",
            "status": status,
            "test_mrn": config.test_mrn,
            "steps": to_jsonable(steps),
        },
    )
    write_json_atomic(root / "step_results.json", to_jsonable(steps))
    coverage = _classification_coverage(simulated, observations, recovery, steps)
    write_json_atomic(root / "parsed/failures/classification_coverage.json", coverage)
    _write_coverage(root, plan, steps, status)
    existing = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    existing["failure_classification"] = coverage
    write_json_atomic(root / "coverage.json", existing)
    with (root / "RESULTS.txt").open("a", encoding="utf-8") as handle:
        handle.write(
            f"\nFailure classification simulations: {coverage['simulated_passed']}/"
            f"{coverage['simulated_count']} passed (SIMULATED).\n"
        )
        handle.write("Actual data observations: parsed/failures/observations.json\n")
        handle.write("Unobserved live categories remain NO_SAMPLE; natural TTL is NOT_TESTED.\n")
        for name, entry in coverage["live_categories"].items():
            handle.write(f"  {name}: {entry['status']} ({entry['count']} observations)\n")
    return LiveTestResult(status, tuple(steps), summary, run_id)


def _summary(result: AcquisitionResult) -> dict:
    info = result.error
    values = result.value if isinstance(result.value, (list, tuple)) else (result.value,)
    return {
        "evidence": "LIVE",
        "acquisition_status": result.status,
        "availability": result.data.availability if result.data else "UNKNOWN",
        "complete": result.data.complete if result.data else None,
        "item_count": result.data.item_count if result.data else None,
        "parsing_issues": [item.code for item in result.data.issues] if result.data else [],
        "parsing_warnings": [item.code for item in result.data.warnings] if result.data else [],
        **({"report_data_status": result.value.report_data_status}
           if isinstance(result.value, OrderReport) else {}),
        "error": to_jsonable(info),
        "root_code": info.root_cause.code if info else "",
        "root_category": info.root_cause.category if info else "",
        "phase": info.phase if info else "",
        "retry_safe": info.retry_safe if info else None,
        "retry_recommended": info.retry_recommended if info else False,
        "not_executed_count": sum(
            isinstance(value, ClinicalOrder)
            and classify_order_execution(value.status) == "NOT_EXECUTED"
            for value in values
        ),
    }


def _classification_coverage(simulated, observations, recovery, steps) -> dict:
    counts = Counter(item["availability"] for item in observations)
    categories = {
        name: {
            "status": "OBSERVED" if counts[name] else "NO_SAMPLE",
            "count": counts[name],
            "evidence": "LIVE",
        }
        for name in (
            "AVAILABLE",
            "EMPTY",
            "NOT_FOUND",
            "NOT_EXECUTED",
            "ATTACHMENT_ONLY",
            "METADATA_ONLY",
        )
    }
    not_executed = sum(item["not_executed_count"] for item in observations)
    categories["NOT_EXECUTED"].update(
        status="OBSERVED" if not_executed else "NO_SAMPLE", count=not_executed
    )
    # Readiness and cookie recovery can fail before any data observation exists.
    # Count their actual issues too, without counting simulated or blocked steps.
    observed_steps = {"failures.live." + item["step"] for item in observations}
    extra_errors = [
        step
        for step in steps
        if step.status == "ERROR"
        and step.issue is not None
        and not step.name.startswith("failures.simulated.")
        and step.name not in observed_steps
    ]
    for name in ("NETWORK", "HTTP", "AUTHENTICATION", "AUTHORIZATION", "PARSE"):
        count = sum(
            name in {item["root_category"], (item["error"] or {}).get("category")}
            for item in observations
        )
        count += sum(
            name in {step.issue.category, step.issue.root_cause.category} for step in extra_errors
        )
        categories[name] = {
            "status": "OBSERVED" if count else "NO_SAMPLE",
            "count": count,
            "evidence": "LIVE",
        }
    categories["PARTIAL"] = {
        "status": "OBSERVED"
        if any(item["complete"] is False for item in observations)
        else "NO_SAMPLE",
        "count": sum(item["complete"] is False for item in observations),
        "evidence": "LIVE",
    }
    return {
        "simulated_count": len(simulated),
        "simulated_passed": sum(item["passed"] for item in simulated),
        "live_categories": categories,
        "additional_error_steps": [
            {"step": step.name, "error": to_jsonable(step.issue)} for step in extra_errors
        ],
        "cookie_loss": recovery,
        "auth_observations": [
            {"step": step.name, "status": step.status, "details": step.details,
             "error": to_jsonable(step.issue)}
            for step in steps if step.name in {
                "failures.live.unauthenticated", "failures.live.negative_before_login",
                "failures.live.password_policy",
            }
        ],
        "natural_ttl": {"status": "NOT_TESTED", "verified": False},
    }
