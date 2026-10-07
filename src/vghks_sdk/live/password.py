"""Observe one old-password login and bounded reads without changing credentials."""


from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..core.config import PortalCredentials
from ..core.errors import NotAuthenticatedError, ParseError, error_code
from ..core.operations import operation_spec
from ..core.transport import SafeSessionTransport, _AnonymousAuth
from ..local_io import write_json_atomic
from ..models import to_jsonable
from ..parsing.portal import find_password_change_target, parse_password_status
from ..queries import query_spec
from ..sdk import VghksSDK
from .defaults import SYNTHETIC_MRN
from .login import password_post_budget
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
)


def build_password_plan(config) -> dict:
    return {
        "schema_version": 1, "profile": "password", "auth_targets": ["portal"],
        "negative_password_post_limit": 0, "password_post_limit": 1,
        "max_patients": 1, "max_password_page_gets": 1, "download_assets": False,
        "change_password_submissions": 0, "cookie_loss_challenges": 0,
        "anonymous_catalog_comparison": True,
        "blocked_login_probe": "One PRQ catalog request with existing cookies; no SSO or login recovery.",
        "scope": "A catalog response alone proves neither credentials nor access to patient records.",
        "operations": [
            {"key": key, "sdk_method": query_spec(key).sdk_method, "scope": query_spec(key).scope,
             "inputs": list(query_spec(key).inputs), "dependencies": list(query_spec(key).dependencies),
             "requested": True}
            for key in ("prq.upload_types", "webmaas.basic_info")
        ],
    }


def _catalog_without_login(sdk) -> list[dict]:
    """Use the recorded read contract directly, with no ensure/execute/recovery."""
    spec = operation_spec("prq.upload_types")
    runtime = sdk._runtime
    data = runtime.request_json(
        spec, runtime.settings.prq_base_url.rstrip("/") + "/QueryUploadMR.do",
        data=dict(spec.operation_values), retry_safe=False, auth=_AnonymousAuth(),
    )
    if not isinstance(data, list) or any(
        not isinstance(row, dict) or not {"maintp", "mainnm"}.issubset(row) for row in data
    ):
        raise ParseError("upload type schema changed", code="UPLOAD_TYPES_INVALID")
    return data


def _read_change_page(sdk, target: str) -> dict:
    # The tester owns this single GET; retain the SDK's shared TLS/session state
    # and capture sinks without changing its normal request/retry policy.
    with sdk._runtime.operation_lock:
        original = sdk._runtime.transport
        reader = SafeSessionTransport(
            policy=replace(original.policy, max_attempts=1), verify=original.verify,
            session=original.session, sleeper=original.sleeper, rng=original.rng,
            diagnostics=original.diagnostics, raw_capture=original.raw_capture,
            connections=original.connections,
        )
        response = reader.request("GET", target, allow_redirects=False, auth=_AnonymousAuth())
        policy = parse_password_status(reader.text(response), login_stage=True)
        return {"status": "CAPTURED", "captured": True, "http_status": response.status_code,
                "password_status": to_jsonable(policy), "form_submitted": False}


@contextmanager
def _watch_change_destination(sdk):
    """Keep a trusted destination in memory; the normal capture owns its raw URL."""
    transport = sdk._runtime.transport
    session = transport.session
    original = session.send
    observed = {"target": None}

    def send(request, **kwargs):
        response = original(request, **kwargs)
        target = find_password_change_target(
            transport.text(response), response.url, sdk._runtime.settings.portal_base_url,
            location=response.headers.get("Location", "") if response.status_code in {301, 302, 303} else "",
        )
        if target:
            observed["target"] = target
        return response

    session.send = send
    try:
        yield observed
    finally:
        session.send = original


def run_password_test(sdk, config, *, output_dir: Path, raw_capture=None,
                      diagnostics=None, run_id: str = "") -> LiveTestResult:
    root = output_dir
    parsed = root / "parsed/password"
    capture = {"raw_capture": raw_capture, "diagnostics": diagnostics}
    steps: list[LiveTestStep] = []
    write_json_atomic(root / "test_plan.json", build_password_plan(config))

    def step(name, callback, *, key="", classify=None, summarize=None):
        return _run_step(
            steps, name="password." + name, operation=callback, root=root,
            output_path=parsed / (name + ".json"), operation_key=key,
            classify=classify, summarize=summarize, **capture,
        )

    def anonymous():
        with VghksSDK(
            settings=sdk._runtime.settings, credentials=PortalCredentials("ANONYMOUS-PROBE", "unused"),
            **capture,
        ) as probe, password_post_budget(probe._runtime.transport.session, limit=0) as budget:
            try:
                value = _catalog_without_login(probe)
            except NotAuthenticatedError as error:
                return {"evidence": "LIVE_UNAUTHENTICATED", "prior_login": False,
                        "expected_challenge": True, "observed_code": error.info.code,
                        "query_accepted": False, **budget}
            return {"evidence": "LIVE_UNAUTHENTICATED", "prior_login": False,
                    "expected_challenge": False, "observed_code": "", "query_accepted": True,
                    "record_count": len(value), **budget}

    anonymous_value, _ = step("unauthenticated_catalog", anonymous, key="prq.upload_types",
                              summarize=lambda value: value)
    page = {"status": "NO_SAMPLE", "captured": False, "password_status": None}
    source = {"status": "NOT_TESTED", "query_accepted": None, "error_code": ""}
    sdk_catalog_status = "BLOCKED"
    patient_status = "BLOCKED"
    with password_post_budget(sdk._runtime.transport.session, limit=1) as budget:
        with _watch_change_destination(sdk) as observed:
            _, login_error = step("login", sdk.auth.login, key="portal.login")
        if observed["target"]:
            value, page_error = step("change_page", lambda: _read_change_page(sdk, observed["target"]))
            page = value or {"status": "ERROR", "captured": False,
                             "error_code": error_code(page_error), "password_status": None}
        else:
            steps.append(LiveTestStep(name="password.change_page", status="NO_SAMPLE",
                                      details={"reason": "NO_TRUSTED_CHANGE_DESTINATION"}))
        # Keep the SDK decision even if a source endpoint still accepts cookies.
        policy = to_jsonable(sdk.auth.password_status)
        step("policy", lambda: {"password_status": policy}, summarize=lambda value: value)
        if login_error is not None:
            value, source_error = step("existing_cookie_catalog", lambda: _catalog_without_login(sdk),
                                       key="prq.upload_types",
                                       classify=lambda value: "OK" if value else "EMPTY",
                                       summarize=lambda value: {"record_count": len(value),
                                                                "login_recovery": False})
            source = {"status": "ERROR" if source_error else "OK" if value else "EMPTY",
                      "query_accepted": source_error is None, "error_code": error_code(source_error)
                      if source_error else ""}
            for name, key in (("sdk_catalog", "prq.upload_types"), ("basic_info", "webmaas.basic_info")):
                steps.append(LiveTestStep(name="password." + name, status="BLOCKED", operation=key,
                                          details={"reason": "LOGIN_NOT_ESTABLISHED", "attempted": False}))
        else:
            value, catalog_error = step("sdk_catalog", sdk.records.get_upload_types, key="prq.upload_types",
                                        classify=lambda value: "OK" if value else "EMPTY",
                                        summarize=lambda value: {"record_count": len(value)})
            sdk_catalog_status = "ERROR" if catalog_error else "OK" if value else "EMPTY"
            if config.test_mrn == SYNTHETIC_MRN:
                patient_status = "NO_SAMPLE"
                steps.append(LiveTestStep(name="password.basic_info", status="NO_SAMPLE",
                                          operation="webmaas.basic_info",
                                          details={"reason": "NO_AUTHORIZED_PATIENT"}))
            elif catalog_error is None:
                _, patient_error = step("basic_info", lambda: sdk.patients.get_basic_info(config.test_mrn),
                                         key="webmaas.basic_info")
                patient_status = "ERROR" if patient_error else "OK"
            else:
                steps.append(LiveTestStep(name="password.basic_info", status="BLOCKED",
                                          operation="webmaas.basic_info", details={"attempted": False}))
    summary = {
        "schema_version": 1, "login_status": "ERROR" if login_error else "OK",
        "login_error_code": error_code(login_error) if login_error else "",
        "credential_validity": "ACCEPTED" if not login_error else "REJECTED" if
                               error_code(login_error) == "PORTAL_LOGIN_REJECTED" else "UNKNOWN",
        "password_status": policy, "change_page": page,
        "anonymous_catalog": anonymous_value or {"query_accepted": None},
        "existing_cookie_catalog": source, "sdk_catalog_status": sdk_catalog_status,
        "patient_read_status": patient_status, "password_post_budget": dict(budget),
        "change_password_submissions": 0, "natural_ttl_verified": False,
        "scope": "Only the recorded PRQ catalog and one authorized patient's basic info. Catalog access is not credential validity or all-record access.",
    }
    write_json_atomic(parsed / "summary.json", summary)
    status = _overall_status(steps, fatal_auth=False)
    summary_path = root / "run_summary.json"
    write_json_atomic(summary_path, {
        "schema_version": LIVE_TEST_SCHEMA_VERSION, "run_id": run_id, "profile": "password",
        "status": status, "steps": to_jsonable(steps), "password_change_test": summary,
    })
    return LiveTestResult(status=status, steps=tuple(steps), summary_path=summary_path, run_id=run_id)
