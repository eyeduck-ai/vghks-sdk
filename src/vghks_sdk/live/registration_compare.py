"""Compare registration flows with two isolated Portal accounts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..core.capture import RawCaptureSink
from ..core.config import PortalCredentials, SDKSettings
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import AuthenticationError, ErrorInfo
from ..local_io import write_json_atomic
from ..models import RegistrationRecord, to_jsonable
from ..sdk import VghksSDK
from .profile import LiveTestResult, LiveTestStep, _overall_status, _run_step


def append_registration_comparison(
    result: LiveTestResult,
    *,
    primary_credentials: PortalCredentials,
    primary_mrn: str,
    secondary_credentials: PortalCredentials,
    secondary_mrn: str,
    settings: SDKSettings,
    output_dir: Path,
    raw_capture: RawCaptureSink,
    manifest_path: Path,
    diagnostics: DiagnosticRecorder,
) -> LiveTestResult:
    """Run direct and CHECK_PAT-first queries in fresh SDKs for each account."""

    steps = list(result.steps)
    destinations = output_dir / "parsed" / "registration_comparison"
    for label, credentials, mrn in (
        ("vghks", primary_credentials, primary_mrn),
        ("union", secondary_credentials, secondary_mrn),
    ):
        if label == "vghks" and _primary_portal_failed(result, output_dir):
            steps.extend(
                LiveTestStep(
                    f"registration_compare.{label}.{phase}", "BLOCKED",
                    issue=ErrorInfo("PRIMARY_READINESS_FAILED", "DEPENDENCY"),
                )
                for phase in ("login", "direct", "demographics", "after_demographics")
            )
            continue
        with VghksSDK(
            settings=settings, credentials=credentials,
            diagnostics=diagnostics, raw_capture=raw_capture,
        ) as sdk:
            _, login_error = _run_step(
                steps,
                name=f"registration_compare.{label}.login",
                operation=sdk.auth.login,
                output_path=None,
                root=output_dir,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
            )
            if login_error is not None:
                steps.extend(
                    LiveTestStep(
                        f"registration_compare.{label}.{phase}", "BLOCKED",
                        issue=ErrorInfo("ACCOUNT_LOGIN_FAILED", "DEPENDENCY"),
                    )
                    for phase in ("direct", "demographics", "after_demographics")
                )
                continue
            _, direct_error = _run_step(
                steps,
                name=f"registration_compare.{label}.direct",
                operation=lambda sdk=sdk, mrn=mrn: sdk.patients.get_registration_history(mrn),
                operation_key="webmaas.registration_query",
                output_path=destinations / label / "direct.json",
                root=output_dir,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
                summarize=_registration_count,
                classify=_registration_status,
            )
        if isinstance(direct_error, AuthenticationError):
            steps.extend((
                LiveTestStep(
                    f"registration_compare.{label}.demographics", "BLOCKED",
                    issue=ErrorInfo("ACCOUNT_AUTH_FAILED", "DEPENDENCY"),
                ),
                LiveTestStep(
                    f"registration_compare.{label}.after_demographics", "BLOCKED",
                    issue=ErrorInfo("ACCOUNT_AUTH_FAILED", "DEPENDENCY"),
                ),
            ))
            continue
        with VghksSDK(
            settings=settings, credentials=credentials,
            diagnostics=diagnostics, raw_capture=raw_capture,
        ) as sdk:
            _, demographics_error = _run_step(
                steps,
                name=f"registration_compare.{label}.demographics",
                operation=lambda sdk=sdk, mrn=mrn: sdk.patients.get_demographics(mrn),
                operation_key="webmaas.demographics",
                output_path=destinations / label / "demographics.json",
                root=output_dir,
                raw_capture=raw_capture,
                diagnostics=diagnostics,
            )
            if demographics_error is not None:
                steps.append(LiveTestStep(
                    f"registration_compare.{label}.after_demographics", "BLOCKED",
                    issue=ErrorInfo("DEMOGRAPHICS_QUERY_FAILED", "DEPENDENCY"),
                ))
            else:
                _run_step(
                    steps,
                    name=f"registration_compare.{label}.after_demographics",
                    operation=lambda sdk=sdk, mrn=mrn: sdk.patients.get_registration_history(mrn),
                    operation_key="webmaas.registration_query",
                    output_path=destinations / label / "after_demographics.json",
                    root=output_dir,
                    raw_capture=raw_capture,
                    diagnostics=diagnostics,
                    summarize=_registration_count,
                    classify=_registration_status,
                )

    write_json_atomic(output_dir / "step_results.json", to_jsonable(steps))
    comparison_path = output_dir / "registration_comparison.json"
    write_json_atomic(comparison_path, _comparison_report(steps, manifest_path))
    status = (
        _overall_status(steps, fatal_auth=False)
        if result.status in {"OK", "COMPLETED_WITH_GAPS"}
        else result.status
    )
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    summary.update({
        "status": status,
        "steps": to_jsonable(steps),
        "registration_comparison": "registration_comparison.json",
    })
    write_json_atomic(result.summary_path, summary)
    return LiveTestResult(status, tuple(steps), result.summary_path, result.run_id)


def _registration_count(rows: list[RegistrationRecord]) -> dict[str, int]:
    return {"record_count": len(rows)}


def _registration_status(rows: list[RegistrationRecord]) -> str:
    return "OK" if rows else "EMPTY"


def _primary_portal_failed(result: LiveTestResult, root: Path) -> bool:
    readiness = next((step for step in result.steps if step.name == "auth_check"), None)
    if readiness is None or readiness.status == "OK":
        return False
    report_path = root / "parsed" / "readiness.json"
    if not report_path.is_file():
        return True
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    return not any(
        target.get("target") == "portal" and target.get("status") == "OK"
        for target in report.get("targets", [])
        if isinstance(target, dict)
    )


def _comparison_report(steps: list[LiveTestStep], manifest_path: Path) -> dict:
    counts: dict[str, dict[str, int]] = {}
    if manifest_path.is_file():
        with manifest_path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                step = row.get("live_test_step", "")
                if not step.startswith("registration_compare."):
                    continue
                request = row.get("request") or {}
                target = urlsplit(request.get("url", ""))
                path = target.path
                method = request.get("method", "")
                key = (
                    "sso" if method == "GET" and path == "/webmaas/WPSAutoLogon"
                    else "pagination_get" if method == "GET" and path == "/webmaas/RSV/RSV11W001.do"
                    and re.search(r"(?:^|&)d-\w+-p=", target.query)
                    else "form_get" if method == "GET" and path == "/webmaas/RSV/RSV11W001.do"
                    else "patient_check" if method == "POST" and path == "/webmaas/ajax/AJAXAction.do"
                    else "registration_post" if method == "POST" and path == "/webmaas/RSV/RSV11W001.do"
                    else "other"
                )
                bucket = counts.setdefault(step, {})
                bucket[key] = bucket.get(key, 0) + 1
    return {
        "scope": "request shape and result status only; see local raw capture for response bodies",
        "accounts": {
            label: {
                phase: {
                    "status": next((s.status for s in steps if s.name == name), "MISSING"),
                    "error_code": next((s.error_code for s in steps if s.name == name), ""),
                    "record_count": next(
                        (s.details["record_count"] for s in steps
                         if s.name == name and s.details and "record_count" in s.details),
                        None,
                    ),
                    "requests": counts.get(name, {}),
                }
                for phase in ("login", "direct", "demographics", "after_demographics")
                for name in (f"registration_compare.{label}.{phase}",)
            }
            for label in ("vghks", "union")
        },
    }
