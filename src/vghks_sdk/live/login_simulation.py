"""Small deterministic auth scenarios for the portable tester; no network adapter.

Exercises the real SDK, Runtime and Requests transport using an in-memory HTTP
adapter. It never borrows live credentials, cookies, endpoints or raw captures.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter

from ..core.config import PortalCredentials, RequestPolicy, SDKSettings
from ..core.errors import SDKError, error_code
from ..core.transport import SafeSessionTransport
from ..sdk import VghksSDK

_FORM = '<form><input name="muid"><input name="mpassword" type="password"></form>'
_LOGIN = '<script>targetUrl="myPortal.do";</script>'
_DENIED = '<h3>登入失敗</h3><p>帳號或密碼錯誤</p><button onclick="location.href=\'logout.do\'">重新登入</button>'
_ORIGIN = "https://simulation.invalid"


@dataclass(frozen=True)
class LoginScenario:
    name: str
    expected_code: str = ""
    expected_posts: int = 1
    query: bool = False


SCENARIOS = (
    LoginScenario("normal"),
    LoginScenario("blank_username", "CREDENTIAL_USERNAME_MISSING", 0),
    LoginScenario("blank_password", "CREDENTIAL_PASSWORD_MISSING", 0),
    LoginScenario("unknown_account", "PORTAL_LOGIN_REJECTED"),
    LoginScenario("wrong_password", "PORTAL_LOGIN_REJECTED", query=True),
    LoginScenario("wrong_password_error_page", "PORTAL_LOGIN_REJECTED", query=True),
    LoginScenario("login_401", "PORTAL_LOGIN_HTTP_DENIED", query=True),
    LoginScenario("login_403", "PORTAL_LOGIN_HTTP_DENIED", query=True),
    LoginScenario("empty_response", "PORTAL_LOGIN_RESPONSE_EMPTY", query=True),
    LoginScenario("unknown_response", "PORTAL_LOGIN_TARGET_MISSING", query=True),
    LoginScenario("landing_login_form", "PORTAL_LOGIN_REJECTED"),
    LoginScenario("redirect_302"),
    LoginScenario("redirect_307", "PORTAL_LOGIN_POST_REDIRECT_UNSUPPORTED"),
    LoginScenario("redirect_308", "PORTAL_LOGIN_POST_REDIRECT_UNSUPPORTED"),
    LoginScenario("login_timeout", "NETWORK_TIMEOUT", query=True),
    LoginScenario("expired_once", expected_posts=2, query=True),
    LoginScenario("expired_login_redirect", expected_posts=2, query=True),
    LoginScenario("expired_script_entrance", expected_posts=2, query=True),
    LoginScenario("expired_twice", "AUTH_RELOGIN_FAILED", 2, query=True),
    LoginScenario("relogin_rejected", "PORTAL_LOGIN_REJECTED", 2, query=True),
)


class _MemoryHTTP(BaseAdapter):
    def __init__(self, scenario: LoginScenario):
        self.scenario = scenario
        self.password_posts = 0
        self.query_calls = 0
        self.calls: list[dict[str, str]] = []

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        self.calls.append({"method": request.method, "path": path})
        mode = self.scenario.name
        body, status, location = "ok", 200, ""
        if path == "/index.do":
            body = _FORM
        elif path == "/login.do":
            self.password_posts += 1
            if self.password_posts > 2:
                raise SDKError(
                    "simulated login exceeded budget", code="LOGIN_SIMULATION_POST_LIMIT"
                )
            body = _LOGIN
            if mode in {"unknown_account", "wrong_password"} or (
                mode == "relogin_rejected" and self.password_posts == 2
            ):
                body = _FORM
            elif mode == "wrong_password_error_page":
                body = _DENIED
            elif mode in {"login_401", "login_403"}:
                status = int(mode[-3:])
            elif mode == "empty_response":
                body = ""
            elif mode == "unknown_response":
                body = "<html>Changed login response</html>"
            elif mode.startswith("redirect_"):
                body, status, location = "", int(mode[-3:]), "/myPortal.do"
            elif mode == "login_timeout":
                raise requests.Timeout("synthetic timeout")
        elif path == "/myPortal.do":
            body = _FORM if mode == "landing_login_form" else "portal"
        elif path == "/ssoFromDn.do":
            body = (
                f'<form action="{_ORIGIN}/PRQWeb/WPSAutoLogon">'
                '<input name="HID" value="SYNTHETIC"><input name="ssID" value="test">'
                '<input name="keyOne" value="1"><input name="keyTwo" value="2">'
                '<input name="keyThree" value="3"><input name="uid" value="TST1">'
                f'<input name="targetURL" value="{_ORIGIN}/PRQWeb/landing.do"></form>'
            )
        elif path in {
            "/PRQWeb/WPSAutoLogon",
            "/PRQWeb/landing.do",
            "/sessionCheck.do",
            "/ssoLogAdd.do",
            "/aptreePath.do",
        }:
            body = "application"
        elif path == "/PRQWeb/QueryUploadMR.do":
            self.query_calls += 1
            expired = mode == "expired_twice" or (
                mode in {"expired_once", "relogin_rejected"} and self.query_calls == 1
            )
            body = _FORM if expired else '[{"maintp":"OPD","mainnm":"Synthetic"}]'
            if self.query_calls == 1 and mode == "expired_login_redirect":
                body, status, location = "", 302, _ORIGIN.replace("https:", "http:") + "/"
            if self.query_calls == 1 and mode == "expired_script_entrance":
                body = f'<script>location.href="{_ORIGIN}/index.do";</script>'
        else:
            raise SDKError("unexpected simulated endpoint", code="LOGIN_SIMULATION_ROUTE_UNKNOWN")
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = status
        response._content = body.encode("utf-8")
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        if location:
            response.headers["Location"] = location
        return response

    def close(self):
        pass


def run_scenario(scenario: LoginScenario) -> dict:
    adapter = _MemoryHTTP(scenario)
    session = requests.Session()
    session.trust_env = False
    # Both schemes are intercepted; even an unexpected redirect cannot use a socket.
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=0)
    settings = SDKSettings(
        portal_base_url=_ORIGIN,
        prq_base_url=_ORIGIN + "/PRQWeb",
        request_policy=policy,
    )
    credentials = PortalCredentials(
        "" if scenario.name == "blank_username" else "TST1",
        "" if scenario.name == "blank_password" else "synthetic-password",
    )
    observed = ""
    generation = 0
    try:
        with VghksSDK(
            settings=settings,
            credentials=credentials,
            transport=SafeSessionTransport(policy=policy, session=session),
        ) as sdk:
            credentials.validate()
            if scenario.query:
                sdk.records.get_upload_types()
            else:
                sdk.auth.login()
            generation = sdk._runtime.auth.generation
    except Exception as exc:
        observed = error_code(exc)
    finally:
        session.close()
    return {
        "evidence": "SIMULATED",
        "scenario": scenario.name,
        "passed": observed == scenario.expected_code
        and adapter.password_posts == scenario.expected_posts,
        "expected_code": scenario.expected_code,
        "observed_code": observed,
        "expected_password_posts": scenario.expected_posts,
        "password_posts": adapter.password_posts,
        "generation": generation,
        "requests": adapter.calls,
    }
