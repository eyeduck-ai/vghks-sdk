"""Exercise authentication challenges and password policy with no sockets."""

# ruff: noqa: RUF001

from __future__ import annotations

from urllib.parse import urlsplit

import requests

from ..acquisition import acquire
from ..core.config import PortalCredentials, RequestPolicy, SDKSettings
from ..core.errors import AuthExpiredError, NotAuthenticatedError, SDKError, error_info
from ..core.operations import operation_spec
from ..core.transport import SafeSessionTransport
from ..models import to_jsonable
from ..sdk import VghksSDK
from .login_simulation import _ORIGIN, LoginScenario, _MemoryHTTP

AUTH_EDGE_SCENARIOS = (
    "unauthenticated_401",
    "unauthenticated_403",
    "unauthenticated_redirect",
    "unauthenticated_form",
    "expired_401",
    "expired_403",
    "expired_redirect",
    "expired_form",
    "password_warning_visible",
    "password_warning_script",
    "password_warning_bracketed",
    "password_required_visible",
    "password_required_form",
    "password_required_redirect",
    "password_required_landing",
    "password_unknown",
    "password_inactive_alert",
)

_CHANGE_FORM = '<form><input name="oldPassword" type="password"><input name="newPassword" type="password"></form>'


class _EdgeHTTP(_MemoryHTTP):
    def send(self, request, **kwargs):
        mode = self.scenario.name
        path = urlsplit(request.url).path
        if (
            path == "/PRQWeb/QueryUploadMR.do"
            and mode.startswith(("unauthenticated_", "expired_"))
            and self.password_posts < 2
        ):
            self.query_calls += 1
            response = requests.Response()
            response.request, response.url = request, request.url
            response.status_code = 200
            body = '<form><input name="muid"><input name="mpassword"></form>'
            if mode.endswith(("401", "403")):
                response.status_code = int(mode[-3:])
            if mode.endswith("redirect"):
                response.status_code = 302
                response.headers["Location"] = _ORIGIN + "/index.do"
            response._content = body.encode("utf-8")
            response._content_consumed = True
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            return response
        response = super().send(request, **kwargs)
        body = None
        if path == "/login.do":
            body = {
                "password_warning_visible": '<p>密碼將於 3 天後到期，請更改密碼。</p><script>targetUrl="myPortal.do";</script>',
                "password_warning_script": '<script>alert("密碼剩餘3天到期"); targetUrl="myPortal.do";</script>',
                "password_warning_bracketed": '<script>alert("密碼將於【4】日後到期，請盡快修改。"); targetUrl="myPortal.do";</script>',
                "password_required_visible": "<h3>密碼已到期，必須先變更密碼才能登入。</h3>",
                "password_required_form": _CHANGE_FORM,
                "password_required_redirect": '<script>targetUrl="changePassword.do";</script>',
                "password_unknown": "<p>密碼問題請洽資訊室</p>",
                "password_inactive_alert": '<script>// alert("密碼已到期");\nfunction later(){alert("密碼已到期");}; targetUrl="myPortal.do";</script>',
            }.get(mode)
        elif path == "/myPortal.do" and mode == "password_required_landing":
            body = "<p>必須變更密碼後才能登入</p>"
        if body is not None:
            response._content = body.encode("utf-8")
        return response


def run_auth_edge_scenario(name: str) -> dict:
    if name not in AUTH_EDGE_SCENARIOS:
        raise ValueError("unknown auth edge simulation")
    adapter = _EdgeHTTP(LoginScenario(name))
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    policy = RequestPolicy(
        min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=0, max_attempts=1
    )
    settings = SDKSettings(
        portal_base_url=_ORIGIN, prq_base_url=_ORIGIN + "/PRQWeb", request_policy=policy
    )
    info, recovered, actual_type = None, False, ""
    with VghksSDK(
        settings=settings,
        credentials=PortalCredentials("TST1", "synthetic-password"),
        transport=SafeSessionTransport(policy=policy, session=session, sleeper=lambda _: None),
    ) as sdk:
        if name.startswith(("unauthenticated_", "expired_")):
            if name.startswith("expired_"):
                sdk.auth.login()
            spec = operation_spec("prq.upload_types")
            try:
                sdk._runtime.request_json(
                    spec, _ORIGIN + spec.path, data=dict(spec.operation_values)
                )
            except SDKError as exc:
                info, actual_type = error_info(exc), type(exc).__name__
                expected = (
                    AuthExpiredError if name.startswith("expired_") else NotAuthenticatedError
                )
                passed = isinstance(exc, expected)
            else:
                passed = False
            if name.startswith("expired_"):
                result = acquire(sdk.records.get_upload_types)
                recovered = result.ok and sdk._runtime.auth.generation == 2
                passed = passed and recovered and adapter.password_posts == 2
            else:
                passed = (
                    passed and adapter.password_posts == 0 and info.code == "AUTH_NOT_AUTHENTICATED"
                )
        else:
            result = acquire(sdk.auth.login)
            info = result.error
            status = sdk.auth.password_status
            if name.startswith("password_warning"):
                expected_days = 4 if name == "password_warning_bracketed" else 3
                passed = result.ok and status.status == "EXPIRING" and status.remaining_days == expected_days
            elif name.startswith("password_required"):
                passed = (
                    result.status == "ERROR"
                    and info.code == "PORTAL_PASSWORD_CHANGE_REQUIRED"
                    and not info.retry_safe
                    and status.status == "CHANGE_REQUIRED"
                )
            elif name == "password_unknown":
                passed = (
                    result.status == "ERROR"
                    and info.code == "PORTAL_LOGIN_TARGET_MISSING"
                    and status.status == "NO_NOTICE"
                )
            else:
                passed = result.ok and status.status == "NO_NOTICE"
            passed = passed and adapter.password_posts == 1
        return {
            "evidence": "SIMULATED",
            "scenario": name,
            "passed": passed,
            "error": to_jsonable(info),
            "error_type": actual_type,
            "password_status": to_jsonable(sdk.auth.password_status),
            "password_posts": adapter.password_posts,
            "recovered": recovered,
        }
