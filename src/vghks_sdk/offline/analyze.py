"""Assemble saved-bundle analysis and retain the existing analysis entry points."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from ..build_info import build_identity
from ..contracts.auth_evidence import expected_anonymous_challenge, expected_rejection
from ..contracts.auth_evidence import in_capture_range as _in_capture_range
from ..contracts.auth_evidence import verified_cookie_recovery as _verified_cookie_recovery
from ..core.errors import ConfigurationError
from ..local_io import write_json_atomic
from ..queries import QUERY_BY_KEY, QUERY_SPECS
from .assets import verify_order_assets
from .bundle import BundleReader
from .cache import _AnalysisReader
from .evidence import (
    _CASE_STEPS as _CASE_STEPS,
)
from .evidence import (
    _LEGACY_STEPS as _LEGACY_STEPS,
)
from .evidence import (
    _NETWORK_TARGETS as _NETWORK_TARGETS,
)
from .evidence import (
    _authentication_results as _authentication_results,
)
from .evidence import (
    _certificate_comparisons as _certificate_comparisons,
)
from .evidence import (
    _connection_profiles as _connection_profiles,
)
from .evidence import (
    _connection_trials as _connection_trials,
)
from .evidence import (
    _data_quality_summary as _data_quality_summary,
)
from .evidence import (
    _earnings_results as _earnings_results,
)
from .evidence import (
    _next_action as _next_action,
)
from .evidence import (
    _no_sample_steps as _no_sample_steps,
)
from .evidence import (
    _opd_evidence as _opd_evidence,
)
from .evidence import (
    _ophthalmology_summary as _ophthalmology_summary,
)
from .evidence import (
    _password_policy_summary as _password_policy_summary,
)
from .evidence import (
    _phase as _phase,
)
from .evidence import (
    _portal_login_evidence as _portal_login_evidence,
)
from .evidence import (
    _report_content_summary as _report_content_summary,
)
from .evidence import (
    _safe_build as _safe_build,
)
from .evidence import (
    _safe_code as _safe_code,
)
from .evidence import (
    _safe_network_step as _safe_network_step,
)
from .evidence import (
    _step_query as _step_query,
)
from .evidence import (
    _structured_root_issue as _structured_root_issue,
)
from .evidence import (
    _weekly_opd_summary as _weekly_opd_summary,
)
from .opd_review import review_weekly_physicians
from .pacs import pacs_test_summary
from .password import password_test_summary
from .presentation import _markdown as _markdown
from .presentation import compare_reports as compare_reports
from .recovery import verify_webmaas_recoveries
from .registrations import registration_batch_summary
from .replay import replay_bundle, summarize_replay
from .retest import build_retest_config as _retest_config
from .session import recorded_session_readiness as _recorded_session_readiness
from .session import session_test_summary as _session_test_summary


def analyze_bundle(
    input_path: Path, *, output_dir: Path, compare_path: Path | None = None
) -> dict[str, Any]:
    destination = output_dir.expanduser().resolve()
    for source in (input_path, compare_path):
        if source is not None and source.is_dir() and destination.is_relative_to(source.resolve()):
            raise ConfigurationError(
                "analysis output must be outside the original bundle",
                code="ANALYSIS_OUTPUT_IN_BUNDLE",
            )
    with BundleReader(input_path) as reader:
        report, retest = inspect_bundle(reader)
    if compare_path is not None:
        with BundleReader(compare_path) as previous:
            before, _ = inspect_bundle(previous)
        report["comparison"] = compare_reports(before, report)
    destination.mkdir(parents=True, exist_ok=True)
    write_json_atomic(destination / "analysis.json", report)
    write_json_atomic(destination / "retest-config.json", retest)
    (destination / "analysis.md").write_text(_markdown(report), encoding="utf-8")
    return report


def inspect_bundle(reader: BundleReader) -> tuple[dict[str, Any], dict[str, Any]]:
    with _AnalysisReader(reader) as snapshot:
        return _inspect_bundle(snapshot)


def _inspect_bundle(reader: BundleReader) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = reader.json("run_summary.json")
    environment = reader.json("environment.json") if "environment.json" in reader.names else {}
    rows = replay_bundle(reader)
    steps = summary.get("steps") or []
    if not steps and "step_results.json" in reader.names:
        steps = reader.json("step_results.json")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ConfigurationError("invalid step results", code="BUNDLE_STEPS_INVALID")
    application_recoveries = verify_webmaas_recoveries(reader, rows)
    steps_by_name = {}
    steps_by_query: dict[str, list[dict]] = defaultdict(list)
    for step in steps:
        if isinstance(step.get("name"), str):
            steps_by_name.setdefault(step["name"], step)
        steps_by_query[_step_query(step)].append(step)
    rows_by_operation: dict[str, list[dict]] = defaultdict(list)
    error_rows_by_id = {}
    for row in rows:
        rows_by_operation[row["operation"]].append(row)
        if row["error_code"]:
            error_rows_by_id.setdefault(row["capture_id"], row)
    expected_steps = {step["name"]: step for step in steps if expected_rejection(step)}
    anonymous_steps = {step["name"]: step for step in steps if expected_anonymous_challenge(step)}
    expected_rows = []
    anonymous_rows = []
    recovered_auth_ids = set()
    recovery = next((step for step in steps if _verified_cookie_recovery(step)), None)
    last_recovery_response = max(
        (row["capture_id"] for row in rows_by_operation["prq.upload_types"]
         if recovery and _in_capture_range(row, recovery) and row["status"] in {"PARSED", "EMPTY"}),
        default="",
    )
    for row in rows:
        match = next((step for step in expected_steps.values() if _in_capture_range(row, step)), None)
        if match and row["error_code"] == "PORTAL_LOGIN_REJECTED":
            row.update(observed_status=row["status"], status="EXPECTED_NEGATIVE", expected_step=match["name"])
            expected_rows.append(row)
        anonymous = next((step for step in anonymous_steps.values()
                          if _in_capture_range(row, step)), None)
        if anonymous and row["operation"] == "prq.upload_types" and row["error_code"] in {
            "HTTP_401", "HTTP_403", "AUTH_SESSION_REDIRECT", "AUTH_SESSION_LOGIN_FORM",
        }:
            row.update(observed_status=row["status"], status="EXPECTED_UNAUTHENTICATED",
                       expected_step=anonymous["name"])
            anonymous_rows.append(row)
        if (recovery and _in_capture_range(row, recovery)
                and row["operation"] == "prq.upload_types"
                and row["error_code"] in {
                    "AUTH_EXPIRED", "AUTH_HTTP_DENIED", "AUTH_SESSION_REDIRECT", "AUTH_SESSION_LOGIN_FORM", "HTTP_401", "HTTP_403",
                }
                and row["capture_id"] < last_recovery_response):
            row.update(recovered=True, recovery_type="COOKIE_LOSS_RELOGIN")
            recovered_auth_ids.add(row["capture_id"])
    readiness = (
        reader.json("parsed/readiness.json") if "parsed/readiness.json" in reader.names else {}
    )
    session_readiness = _recorded_session_readiness(reader, steps)
    # Session step issues already retain every original check. Use its latest
    # report only for the readiness table, without adding duplicate problems.
    authentication = _authentication_results(
        session_readiness if session_readiness is not None else readiness, steps,
    )
    connection_profiles = _connection_profiles(reader)
    problems: list[dict[str, str]] = []
    preflight_problems: list[dict[str, str]] = []
    for row in rows:
        if row.get("recovered"):
            continue
        if row["status"] in {"NETWORK_ERROR", "HTTP_ERROR", "PARSE_ERROR", "UNAVAILABLE"}:
            destination = preflight_problems if row.get("preflight") else problems
            destination.append(
                {
                    "phase": _phase(row["error_code"]),
                    "code": row["error_code"],
                    "operation": row["operation"],
                    "capture_id": row["capture_id"],
                    "step": row.get("probe", ""),
                }
            )
    # Readiness is often returned as a value rather than thrown; old bundles
    # have an empty errors.jsonl even when TLS failed. Inspect it explicitly.
    for item in [summary, *steps, *(readiness.get("targets") or []), *reader.jsonl("errors.jsonl")]:
        if item.get("status") == "NO_SAMPLE":
            # Missing evidence is tracked separately. Actual response errors
            # above and exception journal entries still remain problems.
            continue
        issue = item.get("issue") or {}
        code = _safe_code(issue.get("code"))
        expected_step = expected_steps.get(item.get("name") or item.get("step"))
        linked = {"capture_id": item.get("linked_capture_id")}
        if code == "PORTAL_LOGIN_REJECTED" and expected_step and _in_capture_range(linked, expected_step):
            continue
        anonymous_step = anonymous_steps.get(item.get("name") or item.get("step"))
        if code == "AUTH_NOT_AUTHENTICATED" and anonymous_step and _in_capture_range(linked, anonymous_step):
            continue
        if code in {"AUTH_EXPIRED", "AUTH_HTTP_DENIED", "AUTH_SESSION_REDIRECT", "AUTH_SESSION_LOGIN_FORM"} and item.get("linked_capture_id") in recovered_auth_ids:
            continue
        outer_code = code
        code = _safe_code(_structured_root_issue(issue).get("code")) or code
        if code and code not in {"DEPENDENCY_FAILED", "READINESS_FAILED"}:
            operation = issue.get("operation", "")
            if operation not in QUERY_BY_KEY and operation not in {
                "portal.login",
                "portal.entry",
                "portal.session_check",
                "webmaas.session_check",
                "portal.sso_from_dn",
                "auth_check",
                "mis.performance",
                "mis.payroll",
            }:
                operation = ""
            problem = {
                "phase": _phase(code),
                "code": code,
                "operation": operation,
                "capture_id": "",
                "step": _safe_network_step(item.get("name") or item.get("step")),
            }
            if outer_code != code:
                problem["context_code"] = outer_code
            label = str(item.get("name") or item.get("step") or "")
            destination = preflight_problems if label.startswith("network.") else problems
            # Raw exception/response evidence takes precedence over legacy
            # summary codes (0.10.1 called every SSL error a CA failure).
            checkpoint = steps_by_name.get(label, item)
            last_id = checkpoint.get("last_capture_id")
            evidence = error_rows_by_id.get(last_id) if isinstance(last_id, str) else None
            if evidence is not None:
                code = evidence["error_code"]
                operation = evidence["operation"]
                problem.update(
                    code=code,
                    phase=_phase(code),
                    operation=operation,
                    capture_id=evidence["capture_id"],
                    step=evidence.get("probe", ""),
                )
            if operation and any(
                existing["operation"] == operation and existing["capture_id"]
                for existing in destination
            ):
                continue
            if not any(
                item["code"] == code
                and item["operation"] == operation
                and (not problem["step"] or item["step"] == problem["step"])
                for item in destination
            ):
                destination.append(problem)
    order_assets = verify_order_assets(reader, steps)
    problems.extend(
        {"phase": "DATA", "code": issue["code"], "operation": issue["operation"],
         "capture_id": "", "step": ""}
        for issue in order_assets["issues"]
    )
    pacs_test = pacs_test_summary(reader, steps)
    for issue in pacs_test["issues"]:
        if not any(item["code"] == issue["code"] and item["operation"] == issue["operation"]
                   for item in problems):
            problems.append({"phase": "DATA", **issue, "capture_id": "", "step": ""})
    registration_batch = registration_batch_summary(reader, steps)
    problems.extend({"phase": "DATA", **issue, "capture_id": "", "step": ""}
                    for issue in registration_batch["issues"])
    priority = {
        "CONNECTIVITY": 0,
        "AUTHENTICATION": 1,
        "HTTP": 2,
        "PARSE": 3,
        "DATA": 4,
        "EXECUTION": 5,
    }
    problems.sort(key=lambda item: priority.get(item["phase"], 9))
    matrix = []
    for spec in QUERY_SPECS:
        selected = steps_by_query[spec.key]
        statuses = {step.get("status") for step in selected}
        if statuses & {"ERROR", "BLOCKED", "MISSING"}:
            live = (
                "FAILED"
                if "ERROR" in statuses
                else "BLOCKED"
                if "BLOCKED" in statuses
                else "MISSING"
            )
        elif "OK" in statuses:
            positive = any(
                step.get("status") == "OK"
                and (step.get("details") or {}).get("record_count", 1) != 0
                for step in selected
            )
            live = "VERIFIED" if positive else "EMPTY"
        elif "EMPTY" in statuses:
            live = "EMPTY"
        elif "NO_SAMPLE" in statuses:
            live = "NO_SAMPLE"
        else:
            live = "NOT_TESTED"
        recorded = rows_by_operation[spec.key]
        matrix.append(
            {
                "operation": spec.key,
                "sdk_method": spec.sdk_method,
                "live_status": live,
                "live_step_count": len(selected),
                "live_partial_count": sum(
                    (step.get("details") or {}).get("acquisition_status") == "PARTIAL"
                    for step in selected
                ),
                "live_counts": {
                    status: sum(step.get("status") == status for step in selected)
                    for status in ("OK", "EMPTY", "ERROR", "BLOCKED", "MISSING", "NO_SAMPLE")
                },
                "replay_parsed": sum(row["status"] == "PARSED" for row in recorded),
                "replay_empty": sum(row["status"] == "EMPTY" for row in recorded),
                "replay_errors": sum(
                    not row.get("recovered")
                    and row["status"] in {"PARSE_ERROR", "HTTP_ERROR", "NETWORK_ERROR", "UNAVAILABLE"}
                    for row in recorded
                ),
            }
        )
    run_status = _safe_code(summary.get("status")) or "UNKNOWN"
    ready = {row["target"] for row in authentication if row["status"] == "OK"}
    connected = ready | {row["target"] for row in connection_profiles if row["applied"]}
    for item in preflight_problems:
        parts = item["step"].split(".")
        item["recovered"] = len(parts) == 3 and parts[1] in connected and parts[2] != "configure"
    unresolved_probes = [item for item in preflight_problems if not item["recovered"]]
    main = problems[0] if problems else unresolved_probes[0] if unresolved_probes else None
    no_sample_steps = _no_sample_steps(steps)
    data_quality = _data_quality_summary(steps, rows)
    has_gaps = (bool(no_sample_steps) or run_status == "COMPLETED_WITH_GAPS"
                or bool(data_quality["recorded_partial_steps"] or data_quality["offline_partial_responses"]))
    if problems or unresolved_probes or run_status not in {"OK", "COMPLETED_WITH_GAPS"}:
        analysis_status = "NEEDS_ATTENTION"
    else:
        analysis_status = "COMPLETED_WITH_GAPS" if has_gaps else "OK"
    connection_trials = _connection_trials(reader, steps)
    profiles_path = "parsed/network/selected_profiles.json"
    profiles = reader.json(profiles_path) if profiles_path in reader.names else {}
    unverified_services = [
        target
        for target in _NETWORK_TARGETS
        if isinstance(profiles.get(target), dict)
        and profiles[target].get("applied") is True
        and profiles[target].get("certificate_verification") is False
    ]
    report = {
        "schema_version": 3,
        "authentication": authentication,
        "query_summary": {
            key: sum(row["live_status"] == key for row in matrix)
            for key in ("VERIFIED", "EMPTY", "FAILED", "BLOCKED", "MISSING", "NO_SAMPLE", "NOT_TESTED")
        },
        "connection_trials": connection_trials,
        "connection_profiles": connection_profiles,
        "certificate_comparisons": _certificate_comparisons(connection_trials),
        "unverified_tls_services": unverified_services,
        "preflight_findings": preflight_problems,
        "recovered_requests": [row for row in rows if row.get("recovered")],
        "application_session_recoveries": application_recoveries,
        "expected_login_rejections": expected_rows,
        "expected_unauthenticated_responses": anonymous_rows,
        "bundle_status": run_status,
        "analysis_status": analysis_status,
        "bundle": {
            "status": "READABLE",
            "file_count": len(reader.names),
        },
        "recorded_build": _safe_build(
            environment.get("build") or {"sdk_version": environment.get("sdk_version")}
        ),
        "analyzer_build": build_identity(),
        "root_cause": main,
        "next_action": _next_action(main, has_gaps=has_gaps),
        "portal_login_evidence": _portal_login_evidence(reader),
        "password_policy": _password_policy_summary(steps, rows),
        "data_quality": data_quality,
        "problems": problems,
        "operations": matrix,
        "replay": summarize_replay(rows),
        "session_test": _session_test_summary(reader, steps, rows, application_recoveries),
        "opd_evidence": _opd_evidence(reader, steps, rows),
        "weekly_opd_workflow": _weekly_opd_summary(reader),
        "weekly_physician_review": review_weekly_physicians(reader),
        "ophthalmology_orders": _ophthalmology_summary(reader),
        "order_assets": order_assets,
        "pacs_test": pacs_test,
        "registration_batch": registration_batch,
        "earnings_reports": _earnings_results(steps),
        "report_content": _report_content_summary(steps, rows),
        "no_sample_operations": [row["operation"] for row in matrix if row["live_status"] == "NO_SAMPLE"],
        "no_sample_steps": no_sample_steps,
        "scope": "Per-run evidence only. Offline parsing does not verify current intranet access or request sequencing.",
    }
    password_test = password_test_summary(reader)
    if password_test is not None:
        report["password_change_test"] = password_test
    config = reader.json("run_config.json") if "run_config.json" in reader.names else {}
    return report, _retest_config(config, matrix, main, run_status)
