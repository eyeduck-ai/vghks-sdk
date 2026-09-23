"""Turn an EXE return ZIP into diagnoses, replay evidence and a retest recipe."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..build_info import build_identity
from ..core.errors import ConfigurationError
from ..core.readiness import AUTH_CHECK_REGISTRY
from ..live.config import resolve_live_test_config
from ..live.login import expected_rejection
from ..local_io import write_json_atomic
from ..queries import QUERY_BY_KEY, QUERY_SPECS
from .bundle import BundleReader
from .opd_review import review_weekly_physicians
from .replay import replay_bundle, request_parts, summarize_replay

_NETWORK_TARGETS = (*(spec.key for spec in AUTH_CHECK_REGISTRY), "mis")

_LEGACY_STEPS = {
    "patient_demographics": "webmaas.demographics",
    "registration_history": "webmaas.registration_query",
    "visit_cases": "prq.visit_cases",
    "order_history": "prq.order_history",
    "order_history_departmental": "prq.order_history",
    "medication_history": "prq.medication_history",
    "numeric_history": "prq.numeric_history",
    "surgery_history": "prq.surgery_history",
    "opd_patient_list": "prq.opd_patients",
    "surgery_schedule": "oppl.surgery_schedule",
    "unsigned_records": "audit.unsigned_records",
}
_CASE_STEPS = {
    "case_detail": "prq.case_detail",
    "soap": "prq.soap",
    "numeric": "prq.numeric",
    "orders": "prq.case_orders",
    "medications": "prq.case_medications",
    "consults": "prq.consults",
    "treatments": "prq.treatments",
}


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
    summary = reader.json("run_summary.json")
    environment = reader.json("environment.json") if "environment.json" in reader.names else {}
    rows = replay_bundle(reader)
    steps = summary.get("steps") or []
    if not steps and "step_results.json" in reader.names:
        steps = reader.json("step_results.json")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ConfigurationError("invalid step results", code="BUNDLE_STEPS_INVALID")
    expected_steps = {step["name"]: step for step in steps if expected_rejection(step)}
    expected_rows = []
    recovered_auth_ids = set()
    for row in rows:
        match = next((step for step in expected_steps.values() if _in_capture_range(row, step)), None)
        if match and row["error_code"] == "PORTAL_LOGIN_REJECTED":
            row.update(observed_status=row["status"], status="EXPECTED_NEGATIVE", expected_step=match["name"])
            expected_rows.append(row)
        recovery = next((step for step in steps if step.get("name") == "login.cookie_loss"
                         and step.get("status") == "OK"
                         and (step.get("details") or {}).get("recovered") is True), None)
        if (recovery and _in_capture_range(row, recovery)
                and row["operation"] == "prq.upload_types"
                and row["error_code"] in {
                    "AUTH_EXPIRED", "AUTH_SESSION_REDIRECT", "AUTH_SESSION_LOGIN_FORM", "HTTP_401", "HTTP_403",
                }
                and any(later["capture_id"] > row["capture_id"]
                        and _in_capture_range(later, recovery)
                        and later["operation"] == row["operation"]
                        and later["status"] in {"PARSED", "EMPTY"} for later in rows)):
            row.update(recovered=True, recovery_type="COOKIE_LOSS_RELOGIN")
            recovered_auth_ids.add(row["capture_id"])
    readiness = (
        reader.json("parsed/readiness.json") if "parsed/readiness.json" in reader.names else {}
    )
    authentication = _authentication_results(readiness, steps)
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
        if code in {"AUTH_EXPIRED", "AUTH_SESSION_REDIRECT", "AUTH_SESSION_LOGIN_FORM"} and item.get("linked_capture_id") in recovered_auth_ids:
            continue
        if code and code not in {"DEPENDENCY_FAILED", "READINESS_FAILED"}:
            operation = issue.get("operation", "")
            if operation not in QUERY_BY_KEY and operation not in {
                "portal.login",
                "portal.entry",
                "portal.session_check",
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
            label = str(item.get("name") or item.get("step") or "")
            destination = preflight_problems if label.startswith("network.") else problems
            # Raw exception/response evidence takes precedence over legacy
            # summary codes (0.10.1 called every SSL error a CA failure).
            checkpoint = next((step for step in steps if step.get("name") == label), item)
            last_id = checkpoint.get("last_capture_id")
            evidence = next(
                (row for row in rows if row["capture_id"] == last_id and row["error_code"]), None
            )
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
        selected = [step for step in steps if _step_query(step) == spec.key]
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
        recorded = [row for row in rows if row["operation"] == spec.key]
        matrix.append(
            {
                "operation": spec.key,
                "sdk_method": spec.sdk_method,
                "live_status": live,
                "live_step_count": len(selected),
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
    has_gaps = bool(no_sample_steps) or run_status == "COMPLETED_WITH_GAPS"
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
        "expected_login_rejections": expected_rows,
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
        "problems": problems,
        "operations": matrix,
        "replay": summarize_replay(rows),
        "opd_evidence": _opd_evidence(reader, steps, rows),
        "weekly_opd_workflow": _weekly_opd_summary(reader),
        "weekly_physician_review": review_weekly_physicians(reader),
        "ophthalmology_orders": _ophthalmology_summary(reader),
        "earnings_reports": _earnings_results(steps),
        "report_content": _report_content_summary(steps, rows),
        "no_sample_operations": [row["operation"] for row in matrix if row["live_status"] == "NO_SAMPLE"],
        "no_sample_steps": no_sample_steps,
        "scope": "Per-run evidence only. Offline parsing does not verify current intranet access or request sequencing.",
    }
    config = reader.json("run_config.json") if "run_config.json" in reader.names else {}
    return report, _retest_config(config, matrix, main, run_status)


def _in_capture_range(row: dict, step: dict) -> bool:
    values = [row.get("capture_id"), step.get("first_capture_id"), step.get("last_capture_id")]
    if not all(isinstance(value, str) and re.fullmatch(r"\d{6}", value) for value in values):
        return False
    return values[1] <= values[0] <= values[2]


def _no_sample_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = []
    for index, step in enumerate(steps, 1):
        if step.get("status") != "NO_SAMPLE":
            continue
        operation = _step_query(step)
        name = str(step.get("name") or "")
        # Never copy arbitrary step names: older tools may include patient IDs.
        known_check = re.fullmatch(
            r"visits\.(?:same_patient|list_equivalence|"
            r"filters\.(?:category_[OAE]|arrival_date|department_(?:code|name)|"
            r"doctor_(?:name|card)|combined)|followup(?:\.(?:soap|orders)_sample)?)",
            name,
        )
        login_check = re.fullmatch(
            r"login\.(?:cookie_loss|personnel\.(?:options|by_card|employee|name|title|unit|subunits|filters))",
            name,
        )
        reason = _safe_code((step.get("issue") or {}).get("code"))
        detail_reason = (step.get("details") or {}).get("reason")
        if (not reason and login_check and isinstance(detail_reason, str)
                and detail_reason in {"NO_UNAMBIGUOUS_OPTION", "DEPENDENCY_FAILED"}):
            reason = detail_reason
        gaps.append(
            {
                "step_index": index,
                "step": name if known_check or login_check else operation,
                "operation": operation,
                "reason_code": reason,
            }
        )
    return gaps


def _ophthalmology_summary(reader: BundleReader) -> dict[str, Any]:
    path = "parsed/workflows/ophthalmology_orders/manifest.json"
    if path not in reader.names:
        return {"status": "NOT_RUN"}
    manifest = reader.json(path)
    # Aggregate only; individual orders and report content remain in the ZIP.
    return {
        "status": manifest.get("status", "UNKNOWN"),
        "counts": manifest.get("counts", {}),
        "coverage": manifest.get("coverage", {}),
        "entry_path": "order_list",
        "manifest": path,
    }


def _report_content_summary(
    steps: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    operations = {"prq.order_report", "prq.text_report"}
    statuses = ("TEXT_AVAILABLE", "ATTACHMENT_ONLY", "METADATA_ONLY", "EMPTY", "UNASSESSED")
    live = [
        (step.get("details") or {}).get("report_data_status", "UNASSESSED")
        for step in steps
        if _step_query(step) in operations and step.get("status") in {"OK", "EMPTY"}
    ]
    replay = [
        row["report_data_status"] for row in rows if row.get("report_data_status") in statuses
    ]
    return {
        "live_steps": {status: live.count(status) for status in statuses},
        "offline_responses": {status: replay.count(status) for status in statuses},
        "scope": "Report text availability; metadata, links and downloaded binaries do not establish extracted report text. Attachment errors do not revoke available text.",
    }


def _earnings_results(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    output = []
    for key in ("mis.performance", "mis.payroll"):
        selected = [step for step in steps if step.get("operation") == key]
        if not selected:
            continue
        statuses = {step.get("status") for step in selected}
        failed = next(
            (status for status in ("ERROR", "BLOCKED", "MISSING") if status in statuses), ""
        )
        report = next((step for step in selected if step.get("name") == f"{key}.report"), {})
        status = failed or ("VERIFIED" if report.get("status") == "OK" else "NOT_TESTED")
        output.append(
            {
                "operation": key,
                "live_status": status,
                "error_code": next(
                    (
                        _safe_code((step.get("issue") or {}).get("code"))
                        for step in selected
                        if step.get("issue")
                    ),
                    "",
                ),
            }
        )
    return output


def _opd_evidence(reader, steps, exchanges):
    missing_sections = 0
    for step in steps:
        if _step_query(step) != "prq.opd_patients":
            continue
        name = step.get("output", "")
        if name not in reader.names:
            continue
        data = reader.json(name)
        if isinstance(data, list):
            missing_sections += sum(
                isinstance(row, dict) and not row.get("section_code") for row in data
            )
    return {
        "recorded_rows_without_section": missing_sections,
        "offline_responses": [
            {
                "capture_id": row["capture_id"],
                "section_counts": row["section_counts"],
                "missing_mrn_count": row.get("missing_mrn_count", 0),
            }
            for row in exchanges
            if "section_counts" in row
        ],
    }


def _weekly_opd_summary(reader):
    path = "parsed/workflows/opd_soap_week/manifest.json"
    if path not in reader.names:
        return {"status": "NOT_TESTED", "counts": {}, "stages": {}}
    value = reader.json(path)
    keys = (
        "days_total",
        "days_completed",
        "days_failed",
        "registrations",
        "dedicated_registrations",
        "shared_registrations",
        "unclassified_registrations",
        "unqueryable_registrations",
        "patients_total",
        "patients_checked",
        "visit_query_errors",
        "registrations_with_visit",
        "registrations_without_visit",
        "registrations_unknown",
        "soap_cases",
        "soap_checked",
        "soap_errors",
        "soap_missing",
        "matches",
        "matched_patients",
    )
    return {
        "status": _safe_code(value.get("status")) or "UNKNOWN",
        "counts": {
            key: value.get("counts", {}).get(key)
            for key in keys
            if type(value.get("counts", {}).get(key)) is int
        },
        "stages": {
            stage: _safe_code(value.get("stages", {}).get(stage))
            for stage in ("stage1_opd", "stage2_visits", "stage3_soap")
        },
    }


def _safe_network_step(value: Any) -> str:
    candidate = str(value or "")
    return (
        candidate
        if re.fullmatch(
            r"network\.(portal|prq|sectord|webmaas|oppl|oppl_records|review|audit|mis|personnel)\."
            r"(dns|tcp|configure|https(_direct)?(_tls12(_compat)?)?(_unverified)?|https_schannel)",
            candidate,
        )
        else ""
    )


def _authentication_results(
    readiness: dict[str, Any], steps: list[dict[str, Any]]
) -> list[dict[str, str]]:
    result = []
    for key in _NETWORK_TARGETS:
        if key == "mis" and not any(row.get("name") == "auth_check.mis" for row in steps):
            continue
        target = next((row for row in readiness.get("targets", []) if row.get("target") == key), {})
        if not target:
            target = next((row for row in steps if row.get("name") == f"auth_check.{key}"), {})
        status = target.get("status")
        result.append(
            {
                "target": key,
                "status": status if status in {"OK", "ERROR", "BLOCKED"} else "NOT_TESTED",
                "error_code": _safe_code((target.get("issue") or {}).get("code")),
            }
        )
    return result


def _connection_trials(reader: BundleReader, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep tested modes/results, never arbitrary URLs, headers or server values."""
    result = []
    for step in steps:
        name = _safe_network_step(step.get("name"))
        if not name:
            continue
        _, app, phase = name.split(".")
        path = f"parsed/network/{app}-{phase}-context.json"
        context = reader.json(path) if path in reader.names else {}
        status = step.get("status")
        result.append(
            {
                "step": name,
                "status": status if status in {"OK", "ERROR"} else "UNKNOWN",
                "error_code": _safe_code((step.get("issue") or {}).get("code")),
                "effective_proxy_present": context.get("effective_proxy_present") is True,
                "certificate_verification": context.get("certificate_verification")
                if isinstance(context.get("certificate_verification"), bool)
                else None,
            }
        )
    return result


def _certificate_comparisons(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_step = {row["step"]: row for row in trials}
    result = []
    for trial in trials:
        if not trial["step"].endswith("_unverified"):
            continue
        paired = by_step.get(trial["step"].removesuffix("_unverified"), {})
        result.append(
            {
                "probe": trial["step"],
                "verified_status": paired.get("status", "NOT_TESTED"),
                "verified_error": paired.get("error_code", ""),
                "unverified_status": trial["status"],
                "unverified_error": trial["error_code"],
                "anonymous_probe": True,
            }
        )
    return result


def _connection_profiles(reader: BundleReader) -> list[dict[str, Any]]:
    """Report selected TLS evidence separately from authentication readiness."""
    path = "parsed/network/selected_profiles.json"
    if path not in reader.names:
        return []
    selections = reader.json(path)
    captures = reader.jsonl("capture_manifest.jsonl")
    output = []
    for key in _NETWORK_TARGETS:
        choice = selections.get(key)
        if not isinstance(choice, dict):
            continue
        probe = _safe_network_step(choice.get("probe"))
        negotiated = next(
            (
                (row.get("response") or {}).get("tls", {})
                for row in captures
                if probe
                and row.get("live_test_step") == probe
                and row.get("kind") == "HTTP_EXCHANGE"
            ),
            {},
        )
        profile = choice.get("tls_profile")
        protocol = negotiated.get("negotiated_protocol")
        cipher = str(negotiated.get("cipher") or "")
        output.append(
            {
                "target": key,
                "profile": profile
                if profile in {"DEFAULT", "TLS12", "TLS12_COMPAT"}
                else "UNKNOWN",
                "applied": choice.get("applied") is True,
                "certificate_verification": choice.get("certificate_verification")
                if isinstance(choice.get("certificate_verification"), bool)
                else None,
                "protocol": protocol if protocol in {"TLSv1.2", "TLSv1.3"} else "UNKNOWN",
                "cipher": cipher
                if re.fullmatch(r"(?:TLS_|ECDHE-|DHE-|AES)[A-Z0-9_-]{1,80}", cipher)
                else "UNKNOWN",
            }
        )
    return output


def _portal_login_evidence(reader: BundleReader) -> list[dict[str, Any]]:
    """Export structural login facts only, without headers, form values or URLs."""
    result = []
    entry_seen = False
    for row in reader.jsonl("capture_manifest.jsonl"):
        if str(row.get("live_test_step", "")).startswith("network."):
            continue
        method, path, _, _ = request_parts(row.get("request") or {})
        if path == "/index.do" and method == "GET":
            entry_seen = True
        if path != "/login.do" or method != "POST" or row.get("kind") != "HTTP_EXCHANGE":
            continue
        response = row.get("response") or {}
        body = reader.read(row["response_file"]) if row.get("response_file") else b""
        headers = {
            str(key).casefold(): str(value)
            for key, value in (row.get("request") or {}).get("headers", [])
        }
        result.append(
            {
                "http_status": int(response.get("status_code", 0)),
                "response_bytes": len(body),
                "response_blank": not body.strip(),
                "entry_page_requested_before_login": entry_seen,
                "origin_header_present": "origin" in headers,
                "referer_is_entry_page": urlsplit(headers.get("referer", "")).path == "/index.do",
            }
        )
    return result


def _step_query(step: dict[str, Any]) -> str:
    key = step.get("operation")
    if key in QUERY_BY_KEY:
        return key
    name = str(step.get("name", ""))
    if name in _LEGACY_STEPS:
        return _LEGACY_STEPS[name]
    match = re.fullmatch(r"(?:latest|visit_\d+)_(.+)", name)
    return _CASE_STEPS.get(match[1], "") if match else ""


def _retest_config(
    config: dict[str, Any], matrix: list[dict[str, Any]], main: dict[str, str] | None, status: str
) -> dict[str, Any]:
    allowed = {
        "login_negative_attempts",
        "visit_filter",
        "doctor_card",
        "opd_date",
        "range_start",
        "range_end",
        "visit_date",
        "order_date",
        "download_assets",
        "asset_terms",
        "all_matching_orders",
        "request_policy",
        "ca_bundle",
        "allow_unverified_tls",
        "endpoint_overrides",
        "max_cases",
        "max_items",
        "weekly_opd_soap",
        "weekly_opd_end",
        "include_earnings",
        "surgery_query",
        "review_query",
    }
    value = {key: item for key, item in config.items() if key in allowed}
    auth_failed = main and main["phase"] in {"CONNECTIVITY", "AUTHENTICATION"}
    value.update(schema_version=6, profile="auth" if auth_failed else "atomic")
    value["only_operations"] = (
        []
        if auth_failed
        else [
            row["operation"]
            for row in matrix
            if row["live_status"] in {"FAILED", "BLOCKED", "MISSING", "EMPTY"}
        ]
    )
    if config.get("profile") in {"comprehensive", "ophthalmology", "visits", "login"}:
        # First-run scenarios include category/date variants which an atomic
        # default call would not reproduce. Preserve the complete scenario set.
        value.update(
            profile=config["profile"],
            only_operations=(config.get("only_operations") or [])
            if config["profile"] == "comprehensive"
            else [],
        )
    if value.get("include_earnings") and value["profile"] == "auth":
        value["profile"] = "comprehensive"
    if not auth_failed and not value["only_operations"] and config.get("profile") == "atomic":
        value["only_operations"] = config.get("only_operations") or []
    # An interruption can leave requested operations with no checkpoint yet.
    if (
        value["profile"] == "atomic"
        and not auth_failed
        and status in {"INTERRUPTED", "RECOVERED_INCOMPLETE"}
    ):
        requested = config.get("only_operations") or [row["operation"] for row in matrix]
        for row in matrix:
            if row["operation"] in requested and row["live_status"] == "NOT_TESTED":
                if QUERY_BY_KEY[row["operation"]].scope.startswith("doctor_") and not config.get(
                    "doctor_card"
                ):
                    continue
                if (
                    QUERY_BY_KEY[row["operation"]].scope in {"image", "pdf", "surgery_pdf"}
                    and config.get("download_assets") is False
                ):
                    continue
                value["only_operations"].append(row["operation"])
    # Validate the generated recipe, but do not load credentials, TLS, or SDK.
    resolved = resolve_live_test_config(json_values=value, environ={})
    resolved.validate_for_execution()
    result = resolved.to_safe_dict()
    result.pop("output_root", None)
    return result


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {row["operation"]: row["live_status"] for row in before["operations"]}
    new = {row["operation"]: row["live_status"] for row in after["operations"]}
    return {
        "previous_build": before["recorded_build"],
        "newly_verified": [
            key for key in new if new[key] == "VERIFIED" and old.get(key) != "VERIFIED"
        ],
        "regressions": [
            key
            for key in new
            if old.get(key) == "VERIFIED" and new[key] in {"FAILED", "BLOCKED", "MISSING"}
        ],
        "not_retested": [
            key for key in new if old.get(key) == "VERIFIED" and new[key] == "NOT_TESTED"
        ],
        "note": "Different dates, filters and changing clinical data can affect comparisons.",
    }


def _safe_code(value: Any) -> str:
    candidate = str(value or "")
    return candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", candidate) else ""


def _safe_build(value: dict[str, Any]) -> dict[str, Any]:
    version = str(value.get("sdk_version") or "")
    build_id = str(value.get("build_id") or "")
    return {
        "sdk_version": version if re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown",
        "build_id": build_id if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", build_id) else None,
    }


def _phase(code: str) -> str:
    if code.startswith(("NETWORK_", "TLS_", "CA_")):
        return "CONNECTIVITY"
    if code.startswith(("AUTH_", "PORTAL_", "SSO_", "SECTORD_")):
        return "AUTHENTICATION"
    if code.startswith("HTTP_"):
        return "HTTP"
    if any(term in code for term in ("PARSE", "INVALID", "STRUCTURE", "CONTAINER", "REPLAY_")):
        return "PARSE"
    if (
        "MISSING" in code
        or "EMPTY" in code
        or code in {"QUERY_INPUT_UNAVAILABLE", "NO_MATCHING_VISIT"}
    ):
        return "DATA"
    return "EXECUTION"


def _next_action(main: dict[str, str] | None, *, has_gaps: bool = False) -> str:
    if main is None:
        if has_gaps:
            return "未發現未恢復的執行錯誤；NO_SAMPLE 表示缺少欄位或候選資料，仍未完成該項驗證。需要驗證這些項目時，先準備相應樣本；不必只為同一份缺樣本資料重跑 EXE。"
        return "檢查操作驗證矩陣；NOT_TESTED 與 EMPTY 尚未證實能取得目標資料。"
    code = main["code"]
    if code == "TLS_VERIFY_FAILED":
        return "已收到憑證驗證失敗證據；檢查 Windows 信任庫、憑證主機名稱與有效期，需要時使用院內提供的 CA PEM。新版 comprehensive 預設比較略過驗證的 HTTPS，連線成功就同輪繼續登入／查詢。不能推論帳密錯誤。"
    if code in {"TLS_EOF", "TLS_PROTOCOL_FAILED", "NETWORK_TLS_FAILED"}:
        return "雙擊新版 EXE 重跑 comprehensive：先測正常憑證驗證、TLS 1.2 與 AES／RSA 相容套件；全部失敗時比較略過憑證驗證及 Windows Schannel。預設允許採用能連線的未驗證 HTTPS，在同輪繼續 SSO／查詢，並標示套用的服務。TLS_EOF 本身尚不能判定是 CA、帳密或特定套件問題，略過驗證也可能仍然失敗。"
    if code == "PORTAL_LOGIN_RESPONSE_EMPTY":
        return "登入 HTTP 回應只有空白，不能判定帳密正確或錯誤。新版先載入 index.do、保留同一個 Session／隱藏欄位並補上 Origin／Referer；仍須在內網雙擊新版 EXE 驗證。"
    if main["phase"] == "CONNECTIVITY":
        return "先重測 auth profile，依錯誤碼檢查內網連線、DNS、Proxy 或逾時設定。"
    if main["phase"] == "AUTHENTICATION":
        return "依登入／SSO 表定位失敗的子系統，檢查該服務的表單與轉址；其他服務已通過的查詢仍是有效實測證據。修正後重測該子系統及其查詢。"
    return "使用 capture_id 對照原始回應，修改 parser 後重新執行離線分析，再攜帶新版 EXE 與 retest-config.json 重測。"


def _markdown(report: dict[str, Any]) -> str:
    root = report["root_cause"]
    lines = [
        "# 內網測試回傳分析",
        "",
        f"- 原執行狀態：`{report['bundle_status']}`",
        f"- 分析狀態：`{report['analysis_status']}`",
        f"- 封裝讀取狀態：`{report['bundle']['status']}`",
        f"- 原 EXE／SDK：`{report['recorded_build']['sdk_version']}`",
        f"- 本機分析器：`{report['analyzer_build']['sdk_version']}`",
        f"- 主要問題：`{root['code'] if root else '未發現錯誤'}`",
        "",
        report["next_action"],
        "",
    ]
    if report["no_sample_steps"]:
        lines += [
            "缺少樣本的檢查（不列為執行錯誤）：",
            "",
            "| 步驟序號 | 檢查／操作 | 原因碼 |",
            "| ---: | --- | --- |",
        ]
        lines.extend(
            f"| {row['step_index']} | {row['step']} | {row['reason_code']} |"
            for row in report["no_sample_steps"]
        )
        lines.append("")
    lines += ["| 登入／SSO 目標 | 實測狀態 | 錯誤碼 |", "| --- | --- | --- |"]
    lines.extend(
        f"| {row['target']} | {row['status']} | {row['error_code']} |"
        for row in report["authentication"]
    )
    counts = report["query_summary"]
    content = report["report_content"]
    lines += [
        "",
        "報告正文與 PDF／JPG 下載分開驗證：",
        "",
        "| 正文狀態 | 原實測步驟 | 本機重解析回應 |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {status} | {count} | {content['offline_responses'][status]} |"
        for status, count in content["live_steps"].items()
    )
    lines += [
        "",
        "TEXT_AVAILABLE 表示已擷取可供後續使用的報告文字，不要求 PDF 下載成功。ATTACHMENT_ONLY／METADATA_ONLY 仍未取得正文；UNASSESSED 表示該版本未獨立判定。",
        "整體測試仍保留其他操作與附件的錯誤；本機重解析不改寫原始實測結果，也不保證附件中的所有內容均已擷取。",
        "",
    ]
    eye = report.get("ophthalmology_orders", {})
    if eye.get("status") not in {None, "NOT_RUN"}:
        lines += ["", f"眼科醫囑路徑：{eye['status']}。"]
        lines.extend(f"- {term}：{counts}" for term, counts in eye.get("coverage", {}).items())
        lines += [
            "",
            "未執行醫囑只記錄略過；JPG 查無資料是有效空結果。PDF/JPG 成功下載不等於已抽取文字或數值；各筆來源、正文與附件位於 parsed/workflows/ophthalmology_orders/。",
            "",
        ]
    opd = report["opd_evidence"]
    if opd["recorded_rows_without_section"]:
        lines += [
            "",
            f"門診清單原解析有 {opd['recorded_rows_without_section']} 筆缺少科別；舊結果不能直接用來區分專屬／共用門診。新版依每筆靜態索引重建科別及診間，以下是同一回應的本機重解析：",
            "",
        ]
    for row in opd["offline_responses"]:
        if row["section_counts"]:
            lines += [
                f"- 回應 {row['capture_id']}：科別筆數 {row['section_counts']}；缺病歷號 {row['missing_mrn_count']} 筆（保留清單，不能進行病歷查詢）。"
            ]
    workflow = report["weekly_opd_workflow"]
    physician_review = report["weekly_physician_review"]
    if physician_review["status"] == "RECLASSIFIED":
        lines += [
            "",
            "依新版醫師標示規則重算門診清單（不改寫原實測就診／SOAP 結果）：",
            f"- 清單分類：{physician_review['counts']}",
            f"- 專屬門診科別分布：{physician_review['dedicated_section_counts']}",
            f"- 專屬門診病人 {physician_review['dedicated_patients']} 人；缺病歷號 {physician_review['dedicated_missing_mrn']} 筆。",
            f"- 比原分類新增 {physician_review['additional_registrations']} 筆掛號、{physician_review['additional_patients']} 位病人；新增掛號須補足當日就診／SOAP 驗證。",
            "",
        ]
    lines += ["", f"最近七天門診／當日就診／SOAP 組合測試：{workflow['status']}。", ""]
    if workflow["counts"]:
        lines += [f"- {key}: {value}" for key, value in workflow["counts"].items()]
        lines += [
            "",
            "三階段資料與命中清單在 ZIP 的 parsed/workflows/opd_soap_week/。INCOMPLETE 代表仍有未知結果，不能當成完整陰性結果。",
            "",
        ]
    lines += [
        "",
        f"{len(report['operations'])} 個原子查詢：成功取得非空資料 {counts['VERIFIED']}、空結果 {counts['EMPTY']}、"
        f"執行失敗 {counts['FAILED']}、相依阻擋 {counts['BLOCKED']}、"
        f"缺輸入 {counts['MISSING']}、未測 {counts['NOT_TESTED']}。",
        "",
    ]
    if report["authentication"][0]["status"] == "OK":
        lines += ["入口登入與 Session 已通過；這不代表所有子系統 SSO 或資料查詢都已通過。", ""]
    if report["connection_profiles"]:
        lines += [
            "本輪選用的 HTTPS 模式（連線成功與登入／資料查詢分開判定）：",
            "",
            "| 服務 | 模式 | 實際 TLS | 實際套件 | 憑證驗證 | 已套用 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {row['target']} | {row['profile']} | {row['protocol']} | {row['cipher']} | "
            f"{row['certificate_verification']} | {row['applied']} |"
            for row in report["connection_profiles"]
        )
        lines += [
            "",
            "HTTPS 與 TLS 加密仍保留。certificate_verification=False 才代表略過憑證驗證；HTTP 404 是收到伺服器回應後的應用程式問題。",
            "",
        ]
    for evidence in report["portal_login_evidence"]:
        lines.extend(
            [
                f"登入證據：HTTP {evidence['http_status']}，回應 {evidence['response_bytes']} bytes，"
                f"僅空白：{evidence['response_blank']}；登入前已取登入頁：{evidence['entry_page_requested_before_login']}；"
                f"Origin：{evidence['origin_header_present']}；Referer 指向登入頁：{evidence['referer_is_entry_page']}。",
                "",
            ]
        )
    if report["preflight_findings"]:
        lines.extend(
            [
                "連線探測紀錄（recovered=True 表示同輪已有可用模式套用或登入／SSO 通過；不代表資料查詢成功）：",
                "",
                "| 探測步驟 | 回應編號 | 依原始證據分類 | recovered |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{item.get('step', '')}` | `{item['capture_id']}` | `{item['code']}` | {item['recovered']} |"
            for item in report["preflight_findings"]
        )
        if any(item["code"] == "TLS_EOF" for item in report["preflight_findings"]):
            lines.extend(
                [
                    "",
                    "`TLS_EOF` 表示 TLS 連線提早中斷，單靠此訊息無法證實是憑證或加密套件問題。",
                ]
            )
        lines.append("")
    tls12_failed = [
        row["step"].split(".")[1]
        for row in report["connection_trials"]
        if row["step"].endswith(".https_tls12") and row["status"] == "ERROR"
    ]
    if tls12_failed:
        lines += [
            f"本輪已實測 TLS 1.2 仍失敗（預設套件）：{', '.join(tls12_failed)}；後續相容套件的結果見連線模式表。",
            "",
        ]
    if report["certificate_comparisons"]:
        lines += [
            "憑證驗證開／關對照：仍使用 HTTPS；以下探測不帶帳密。是否套用至後續登入／查詢，依本輪設定與探測結果決定。",
            "",
            "| 對照探測 | 開啟驗證 | 略過驗證 |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| `{row['probe']}` | {row['verified_status']} {row['verified_error']} | "
            f"{row['unverified_status']} {row['unverified_error']} |"
            for row in report["certificate_comparisons"]
        )
        lines += [
            "",
            "若只有略過驗證時收到 HTTP 回應，優先檢查憑證信任鏈、有效期與主機名稱；"
            "若略過後仍 TLS_EOF，關閉憑證驗證並未解決本次連線中斷。單次成功差異仍可能受連線波動影響。",
            "",
        ]
    if report["unverified_tls_services"]:
        lines += [
            "本輪後續登入／查詢已設定略過憑證驗證的服務："
            + ", ".join(report["unverified_tls_services"])
            + "。HTTPS 加密保留；各服務是否實際登入及取得資料，仍以登入／SSO 與操作結果為準。",
            "",
        ]
    lines += [
        "`VERIFIED` 只代表這份回傳包中有成功且非空的 Service 結果。離線解析成功不等同內網驗證；`EMPTY`、`NOT_TESTED`、`MISSING` 均不算取得資料的成功證據。",
        "",
        "| 操作 | 內網結果 | 本機解析成功 | 空結果 | 解析／HTTP 錯誤 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{row['operation']}` | {row['live_status']} | {row['replay_parsed']} | {row['replay_empty']} | {row['replay_errors']} |"
        for row in report["operations"]
    )
    if report["earnings_reports"]:
        lines += [
            "",
            "選用的本人績點／專勤工作獎金查詢：",
            "",
            "| 操作 | 內網結果 | 錯誤碼 |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| `{row['operation']}` | {row['live_status']} | {row['error_code']} |"
            for row in report["earnings_reports"]
        )
    lines += [
        "",
        "重測設定：`retest-config.json`（不含帳密；使用新 Session 重查，不沿用舊 Cookie）。",
        "",
        "本報告不輸出病歷內容、請求值、帳密或 Cookie；原始回傳包仍保留在原處。",
        "",
    ]
    return "\n".join(lines)
