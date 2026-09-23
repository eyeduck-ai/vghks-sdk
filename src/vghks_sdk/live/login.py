"""Bounded login validation and directory smoke queries; no clinical writes."""

from __future__ import annotations

import re
import secrets
import string
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..core.config import PortalCredentials
from ..core.errors import ErrorInfo, LoginRejectedError, SDKError, error_code
from ..core.operations import OPERATIONS
from ..core.readiness import AUTH_CHECK_REGISTRY
from ..local_io import write_json_atomic
from ..models import PersonnelFilter, PersonnelOption, to_jsonable
from ..queries import query_spec
from ..sdk import VghksSDK
from .atomic import _write_coverage
from .login_simulation import SCENARIOS, run_scenario
from .preflight import independent_readiness
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)


def build_login_plan(config) -> dict:
    keys = ("prq.upload_types", "personnel.options", "personnel.search")
    return {
        "schema_version": 1,
        "profile": "login",
        "auth_targets": [spec.key for spec in AUTH_CHECK_REGISTRY],
        "max_cases": 0,
        "max_items_per_operation": 1,
        "continue_independent_checks": True,
        "negative_password_post_limit": config.login_negative_attempts,
        "negative_tests_last": True,
        "simulated_scenarios": [scenario.name for scenario in SCENARIOS],
        "session_recovery": "Clear local cookies; report whether a real expiry/relogin occurs.",
        "excluded_write_operations": [spec.key for spec in OPERATIONS if spec.mutates],
        "operations": [
            {
                "key": key,
                "sdk_method": query_spec(key).sdk_method,
                "scope": query_spec(key).scope,
                "inputs": list(query_spec(key).inputs),
                "dependencies": list(query_spec(key).dependencies),
                "requested": True,
            }
            for key in keys
        ],
    }


@contextmanager
def password_post_budget(session, limit: int = 1):
    """Count actual prepared sends, including redirects; block before transmission.

    This is a tester guard, not SDK retry policy. The normal SDK still owns TLS,
    session recovery and read retries. An anonymous TLS probe never submits a body.
    """
    counts = {"password_posts": 0, "blocked_posts": 0}
    original_send = session.send

    def send(request, **kwargs):
        body = request.body or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        credential_post = request.method == "POST" and (
            urlsplit(request.url).path == "/login.do"
            or (isinstance(body, str) and "mpassword" in parse_qs(body, keep_blank_values=True))
        )
        if credential_post:
            if counts["password_posts"] >= limit:
                counts["blocked_posts"] += 1
                raise SDKError("password POST limit reached", code="LOGIN_TEST_POST_LIMIT")
            counts["password_posts"] += 1
        return original_send(request, **kwargs)

    session.send = send
    try:
        yield counts
    finally:
        session.send = original_send


def negative_login(sdk, *, lazy: bool, audit_path: Path | None = None) -> dict:
    with password_post_budget(sdk._runtime.transport.session) as counts:
        observed = "LOGIN_TEST_UNEXPECTED_SUCCESS"
        try:
            if lazy:
                sdk.records.get_upload_types()
            else:
                sdk.auth.login()
        except Exception as exc:
            observed = error_code(exc)
            if (
                not isinstance(exc, LoginRejectedError)
                or observed != "PORTAL_LOGIN_REJECTED"
                or counts
                != {
                    "password_posts": 1,
                    "blocked_posts": 0,
                }
            ):
                raise
            return {
                "evidence": "LIVE",
                "expected_failure": True,
                "observed_code": exc.info.code,
                **counts,
                "entry": "lazy_query" if lazy else "explicit_login",
            }
        finally:
            if audit_path is not None:
                write_json_atomic(audit_path, {**counts, "observed_code": observed})
        raise SDKError(
            "wrong password was unexpectedly accepted", code="LOGIN_TEST_UNEXPECTED_SUCCESS"
        )


def expected_rejection(step: dict) -> bool:
    """Only this completed, bounded live scenario can explain a rejection capture."""
    details = step.get("details") or {}
    return (
        step.get("name") in {"login.negative.1", "login.negative.2"}
        and step.get("status") == "OK"
        and details.get("evidence") == "LIVE"
        and details.get("expected_failure") is True
        and details.get("observed_code") == "PORTAL_LOGIN_REJECTED"
        and details.get("password_posts") == 1
        and details.get("blocked_posts") == 0
    )


def _personnel_option_value(label: str, options: Sequence[PersonnelOption]) -> str | None:
    """Match a displayed name or the form's own ``value - name`` label uniquely."""
    label = label.strip()
    if not label:
        return None
    matches = {
        option.value
        for option in options
        if option.value
        and label
        in {
            option.label.strip(),
            re.sub(rf"^{re.escape(option.value)}\s*-\s*", "", option.label.strip()),
        }
    }
    return next(iter(matches)) if len(matches) == 1 else None


def run_login_test(
    sdk,
    config,
    *,
    credentials,
    settings,
    output_dir: Path,
    raw_capture=None,
    diagnostics=None,
    run_id: str = "",
) -> LiveTestResult:
    root = output_dir
    plan = build_login_plan(config)
    write_json_atomic(root / "test_plan.json", plan)
    steps: list[LiveTestStep] = []
    capture = {"raw_capture": raw_capture, "diagnostics": diagnostics}

    def run(name: str, action: Callable, *, key="", summarize=None, classify=None, code=""):
        return _run_step(
            steps,
            name=name,
            operation=action,
            root=root,
            output_path=root / "parsed" / "login" / (name + ".json"),
            operation_key=key,
            summarize=summarize,
            classify=classify,
            classified_error_code=code,
            **capture,
        )

    def unavailable(name, *, key="", missing=False, reason="DEPENDENCY_FAILED"):
        steps.append(
            LiveTestStep(
                name=name,
                operation=key,
                status="NO_SAMPLE" if missing else "BLOCKED",
                details={"reason": reason, "evidence": "LIVE"},
                issue=None if missing else ErrorInfo(reason, "DEPENDENCY"),
            )
        )
        write_json_atomic(root / "step_results.json", to_jsonable(steps))

    # Independent and wholly offline, so an unavailable network cannot hide these.
    for scenario in SCENARIOS:
        run(
            "login.simulated." + scenario.name,
            lambda s=scenario: run_scenario(s),
            summarize=lambda value: value,
            classify=lambda value: "OK" if value["passed"] else "ERROR",
            code="LOGIN_SIMULATION_FAILED",
        )

    _, initial_error = run("login.normal", sdk.auth.login, summarize=lambda _: {"evidence": "LIVE"})
    ready = None
    if initial_error is None:
        ready = independent_readiness(sdk, steps, root=root, **capture)

        def reuse():
            generation = sdk._runtime.auth.generation
            captures = getattr(raw_capture, "capture_count", 0)
            sdk.auth.login()
            return {
                "evidence": "LIVE",
                "generation_before": generation,
                "generation_after": sdk._runtime.auth.generation,
                "additional_requests": getattr(raw_capture, "capture_count", 0) - captures,
            }

        run(
            "login.reuse",
            reuse,
            summarize=lambda value: value,
            classify=lambda v: "OK"
            if v["generation_before"] == v["generation_after"] and not v["additional_requests"]
            else "ERROR",
            code="LOGIN_REUSE_FAILED",
        )

    if _target_ready(ready, "prq"):
        _, query_error = run(
            "login.query",
            sdk.records.get_upload_types,
            key="prq.upload_types",
            summarize=lambda rows: {"evidence": "LIVE", "record_count": len(rows)},
        )
    else:
        query_error = initial_error or SDKError("PRQ not ready")
        unavailable("login.query", key="prq.upload_types")

    # Directory is independent of PRQ and does not require a patient identifier.
    if _target_ready(ready, "personnel"):
        options, options_error = run(
            "login.personnel.options",
            sdk.personnel.get_options,
            key="personnel.options",
        )
        person, person_error = run(
            "login.personnel.by_card",
            lambda: sdk.personnel.get_by_card(credentials.username),
            key="personnel.search",
            classify=lambda value: "OK" if value else "NO_SAMPLE",
        )
        filters = []
        if person:
            filters.extend(
                [
                    ("employee", {"employee_id": person.employee_id}),
                    ("name", {"name": person.name}),
                ]
            )
            for field, label, candidates in (
                ("title", person.title, options.titles if options else ()),
                ("unit", person.unit, options.units if options else ()),
            ):
                value = _personnel_option_value(label, candidates)
                if value is not None:
                    fields = {field: value, "employee_id": person.employee_id}
                    filters.append((field, fields))
                    if field == "unit":
                        filters.append(("subunits", {**fields, "include_subunits": True}))
                else:
                    unavailable(
                        "login.personnel." + field,
                        key="personnel.search",
                        missing=options_error is None,
                        reason="NO_UNAMBIGUOUS_OPTION",
                    )
                    if field == "unit":
                        unavailable(
                            "login.personnel.subunits",
                            key="personnel.search",
                            missing=options_error is None,
                            reason="NO_UNAMBIGUOUS_OPTION",
                        )
        else:
            unavailable(
                "login.personnel.filters", key="personnel.search", missing=person_error is None
            )
        for label, filter_value in filters:

            def search(value=filter_value):
                selected = PersonnelFilter(**value)
                rows = sdk.personnel.search(selected)
                return {
                    "filter": to_jsonable(selected),
                    "records": to_jsonable(rows),
                    "record_count": len(rows),
                    "own_employee_found": any(
                        row.employee_id == person.employee_id for row in rows
                    ),
                }

            run(
                "login.personnel." + label,
                search,
                key="personnel.search",
                summarize=lambda v: {"record_count": v["record_count"], "evidence": "LIVE"},
                classify=lambda v: "OK" if v["own_employee_found"] else "ERROR",
                code="PERSONNEL_FILTER_RESULT_MISMATCH",
            )
    else:
        for label, key in (
            ("options", "personnel.options"),
            ("by_card", "personnel.search"),
            ("filters", "personnel.search"),
        ):
            unavailable("login.personnel." + label, key=key)

    if query_error is None:

        def recover():
            generation = sdk._runtime.auth.generation
            sdk._runtime.transport.reset_cookies()
            data = {"evidence": "LIVE_COOKIE_LOSS", "generation_before": generation}
            try:
                rows = sdk.records.get_upload_types()
                data.update(
                    record_count=len(rows), recovered=sdk._runtime.auth.generation > generation
                )
                return data
            finally:
                data["generation_after"] = sdk._runtime.auth.generation
                write_json_atomic(root / "parsed/login/cookie_loss.json", data)

        run(
            "login.cookie_loss",
            recover,
            summarize=lambda v: v,
            classify=lambda v: "OK" if v["recovered"] else "NO_SAMPLE",
        )
    else:
        unavailable("login.cookie_loss")

    # No correct-password login follows the deliberate failures: avoid obscuring
    # the observed failure counters, and finish all useful queries beforehand.
    negative_allowed = initial_error is None and _target_ready(ready, "portal")
    negative_results = []
    for index in range(1, config.login_negative_attempts + 1):
        name = f"login.negative.{index}"
        if not negative_allowed:
            unavailable(name)
            continue

        def wrong_login(number=index):
            alphabet = string.ascii_letters + string.digits
            wrong = "".join(secrets.choice(alphabet) for _ in range(12))
            while wrong == credentials.password:
                wrong = "".join(secrets.choice(alphabet) for _ in range(12))
            with VghksSDK(
                settings=settings,
                credentials=PortalCredentials(credentials.username, wrong),
                raw_capture=raw_capture,
                diagnostics=diagnostics,
            ) as negative_sdk:
                try:
                    return negative_login(
                        negative_sdk,
                        lazy=number == 2,
                        audit_path=root / f"parsed/login/negative-{number}-post-counts.json",
                    )
                finally:
                    write_json_atomic(
                        root / f"parsed/login/negative-{number}-connections.json",
                        negative_sdk.connection_status(),
                    )

        value, error = run(name, wrong_login, summarize=lambda v: v)
        negative_results.append(
            value or {"observed_code": error_code(error), "expected_failure": False}
        )
        # Only a proven rejection warrants the second intentional failure.
        negative_allowed = error is None

    # Reuse the existing coverage/report schema without claiming an unconfirmed
    # initial TLS choice was exercised. Negative sessions keep their own states.
    write_json_atomic(
        root / "parsed/network/selected_profiles.json",
        {
            key: {**choice, "applied": choice["confirmed"]}
            for key, choice in sdk.connection_status().items()
        },
    )
    status = _overall_status(steps, fatal_auth=initial_error is not None)
    result = LiveTestResult(status, tuple(steps), root / "run_summary.json", run_id)
    write_json_atomic(
        result.summary_path,
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "profile": "login",
            "run_id": run_id,
            "status": status,
            "steps": to_jsonable(steps),
            "login_tests": {
                "negative_limit": config.login_negative_attempts,
                "negative_results": negative_results,
                "simulation_count": len(SCENARIOS),
                "natural_session_timeout_verified": False,
            },
        },
    )
    _write_coverage(root, plan, steps, status)
    with (root / "RESULTS.txt").open("a", encoding="utf-8") as handle:
        handle.write("\nLogin scenarios: SIMULATED does not mean intranet verified.\n")
        handle.write(
            "Cookie-loss NO_SAMPLE = no expiry triggered; natural server timeout not measured.\n"
        )
        handle.write("Expected rejected passwords count as passed checks, not successful logins.\n")
        for step in steps:
            handle.write(f"  {step.name}: {step.status} {step.error_code}\n")
    return result
