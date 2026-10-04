"""Focused WebMAAS readiness, cookie loss, and optional manual idle observations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from ..local_io import write_json_atomic
from ..models import to_jsonable
from ..queries import query_spec
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
)

SESSION_QUERIES = ("webmaas.demographics", "webmaas.basic_info")
_FORM_ERRORS = {"WEBMAAS_QUERY_FORM_MISSING", "WEBMAAS_QUERY_TOKEN_MISSING"}


def build_session_plan(config) -> dict:
    return {
        "schema_version": 1,
        "profile": "session",
        "auth_targets": ["portal", "sectord", "webmaas"],
        "negative_password_post_limit": 0,
        "max_patients": 1,
        "download_assets": False,
        "session_pause": config.session_pause,
        "challenge": "MANUAL_IDLE" if config.session_pause else "WEBMAAS_COOKIE_LOSS",
        "natural_ttl": "No configured TTL is assumed; default does not wait for expiry.",
        "recovery": "Runtime handles explicit expiry once; an unverified form permits one fresh SSO check without forcing Portal login.",
        "operations": [
            {"key": key, "sdk_method": query_spec(key).sdk_method,
             "scope": query_spec(key).scope, "inputs": list(query_spec(key).inputs),
             "dependencies": list(query_spec(key).dependencies), "requested": True}
            for key in SESSION_QUERIES
        ],
    }


def clear_webmaas_cookies(sdk) -> dict:
    """Remove only JSESSIONID cookies isolated to the configured WebMAAS origin/path."""
    runtime = sdk._runtime
    settings = runtime.settings
    base = urlsplit(settings.webmaas_base_url)
    prefix = base.path.rstrip("/")
    others = [urlsplit(getattr(settings, field)) for field in (
        "portal_base_url", "sectord_base_url", "prq_base_url", "oppl_base_url",
        "audit_base_url", "mis_base_url", "review_base_url", "personnel_base_url",
    )]

    def domain_matches(cookie, host: str) -> bool:
        domain = cookie.domain.lstrip(".").lower()
        host = host.lower()
        return domain in {host, host + ".local"} or (
            cookie.domain_initial_dot and host.endswith("." + domain)
        )

    def applies(path: str, target: str) -> bool:
        return target == path or target.startswith(path.rstrip("/") + "/")

    jar = runtime.transport.session.cookies
    selected = []
    for cookie in jar:
        if cookie.name.upper() != "JSESSIONID" or not domain_matches(cookie, base.hostname or ""):
            continue
        path = cookie.path or "/"
        isolated_path = bool(prefix) and (path == prefix or path.startswith(prefix + "/"))
        shared = any(domain_matches(cookie, other.hostname or "") and
                     applies(path, other.path or "/") for other in others)
        if isolated_path or (applies(path, prefix or "/") and not shared):
            selected.append(cookie)
    for cookie in selected:
        jar.clear(cookie.domain, cookie.path, cookie.name)
    return {"evidence": "LOCAL_COOKIE_LOSS", "removed_count": len(selected),
            "isolated": True, "natural_ttl_verified": False}


def run_session_test(
    sdk, config, *, output_dir: Path, raw_capture=None, diagnostics=None,
    run_id: str = "", resume: Callable[[], object] | None = None,
) -> LiveTestResult:
    root = output_dir
    parsed = root / "parsed/session"
    capture = {"raw_capture": raw_capture, "diagnostics": diagnostics}
    steps: list[LiveTestStep] = []
    observations: list[dict] = []
    write_json_atomic(root / "test_plan.json", build_session_plan(config))

    def readiness(phase: str, *, recover_form: bool = False) -> bool:
        report, _ = _run_step(
            steps, name=f"session.{phase}.auth_check",
            operation=lambda: sdk.auth.check(only=("webmaas",)), root=root,
            output_path=parsed / phase / "auth_check.json",
            classify=lambda value: "OK" if value.ok else "ERROR",
            summarize=lambda value: {"evidence": "LIVE", "reauthenticated": value.reauthenticated},
            **capture,
        )
        row = {"phase": phase, "evidence": "LIVE", "readiness": to_jsonable(report),
               "sso_recheck_attempted": False, "sso_recheck_succeeded": False}
        observations.append(row)
        if report is None or report.ok:
            return bool(report and report.ok)
        failures = [target for target in report.targets if target.status == "ERROR"]
        # Missing HTML is not classified as expiry. The first check invalidates
        # only WebMAAS; a separate, bounded check can re-enter its SSO flow.
        can_recheck = recover_form and not report.reauthenticated and failures and all(
            target.target == "webmaas" and target.issue is not None
            and target.issue.code in _FORM_ERRORS for target in failures
        )
        if not can_recheck:
            return False
        row["sso_recheck_attempted"] = True
        refreshed, _ = _run_step(
            steps, name=f"session.{phase}.sso_recheck",
            operation=lambda: sdk.auth.check(only=("webmaas",)), root=root,
            output_path=parsed / phase / "sso_recheck.json",
            classify=lambda value: "OK" if value.ok else "ERROR", **capture,
        )
        row["sso_recheck"] = to_jsonable(refreshed)
        row["sso_recheck_succeeded"] = bool(refreshed and refreshed.ok)
        return row["sso_recheck_succeeded"]

    def patient_reads(phase: str, ready: bool) -> bool:
        succeeded = True
        for key, callback in (
            (SESSION_QUERIES[0], sdk.patients.get_demographics),
            (SESSION_QUERIES[1], sdk.patients.get_basic_info),
        ):
            name = f"session.{phase}.{key}"
            if not ready:
                steps.append(LiveTestStep(name=name, status="BLOCKED", operation=key))
                succeeded = False
                continue
            _, error = _run_step(
                steps, name=name, operation=lambda callback=callback: callback(config.test_mrn),
                root=root, output_path=parsed / phase / (key + ".json"),
                operation_key=key, **capture,
            )
            succeeded = succeeded and error is None
            if error is not None:
                # A failed baseline must not start an intentional challenge.
                ready = False
        return succeeded

    baseline_ready = readiness("baseline")
    baseline_ok = patient_reads("baseline", baseline_ready)
    challenge = {"evidence": "NOT_TESTED", "performed": False}
    if baseline_ok:
        if config.session_pause:
            print("保持此視窗開啟; 閒置期間不會送出請求。要複查時按 Enter。", flush=True)
            started = monotonic()
            (resume or (lambda: input()))()
            challenge = {"evidence": "MANUAL_IDLE", "performed": True,
                         "idle_seconds": round(monotonic() - started, 3),
                         "cookies_cleared": False}
            phase = "after_idle"
        else:
            challenge, _ = _run_step(
                steps, name="session.cookie_loss", operation=lambda: clear_webmaas_cookies(sdk),
                root=root, output_path=parsed / "cookie_loss.json",
                classify=lambda value: "OK" if value["removed_count"] else "NO_SAMPLE", **capture,
            )
            challenge = dict(challenge or {})
            challenge["performed"] = bool(challenge.get("removed_count"))
            phase = "after_cookie_loss"
        if challenge.get("performed"):
            ready = readiness(phase, recover_form=True)
            patient_reads(phase, ready)
    status = _overall_status(steps, fatal_auth=False)
    session_summary = {
        "schema_version": 1, "challenge": challenge, "observations": observations,
        "baseline_succeeded": baseline_ok,
        "natural_ttl": {"status": "IDLE_OBSERVATION" if config.session_pause and
                        challenge.get("performed") else "NOT_TESTED", "verified": False},
        "original_failures_retained": True,
    }
    write_json_atomic(parsed / "comparison.json", session_summary)
    summary_path = root / "run_summary.json"
    write_json_atomic(summary_path, {
        "schema_version": LIVE_TEST_SCHEMA_VERSION, "run_id": run_id, "profile": "session",
        "status": status, "steps": to_jsonable(steps), "session_test": session_summary,
    })
    return LiveTestResult(status=status, steps=tuple(steps), summary_path=summary_path, run_id=run_id)
