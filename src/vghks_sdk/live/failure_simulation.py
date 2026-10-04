"""Failure classification exercises using real SDK code and no socket transport."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import requests

from ..acquisition import acquire
from ..core.config import PortalCredentials, RequestPolicy, SDKSettings
from ..core.operations import operation_spec
from ..core.transport import SafeSessionTransport
from ..models import (
    BinaryAsset,
    ClinicalOrder,
    NumericReport,
    NumericTable,
    OrderReport,
    OrderReportRef,
    VisitCase,
    to_jsonable,
)
from ..parsing.clinical import parse_order_report
from ..parsing.soap import parse_soap
from ..parsing.webmaas import parse_patient_demographics
from ..sdk import VghksSDK
from .login_simulation import _ORIGIN, LoginScenario, _MemoryHTTP


@dataclass(frozen=True)
class FailureScenario:
    name: str
    code: str = ""
    root_code: str = ""
    category: str = "NETWORK"
    password_posts: int = 1
    query_calls: int = 3
    retry_safe: bool = True
    retry_recommended: bool = True


FAILURE_SCENARIOS = (
    FailureScenario("dns", "NETWORK_DNS_FAILED"),
    FailureScenario("connect_timeout", "NETWORK_CONNECT_TIMEOUT"),
    FailureScenario("read_timeout", "NETWORK_READ_TIMEOUT"),
    FailureScenario("connection", "NETWORK_CONNECTION_FAILED"),
    FailureScenario("proxy", "NETWORK_PROXY_FAILED", retry_recommended=False),
    FailureScenario("tls_certificate", "TLS_VERIFY_FAILED", retry_recommended=False),
    FailureScenario("tls_protocol", "TLS_PROTOCOL_FAILED", retry_recommended=False),
    FailureScenario(
        "http_404", "HTTP_404", category="HTTP", query_calls=1, retry_recommended=False
    ),
    FailureScenario("http_429", "HTTP_429", category="HTTP"),
    FailureScenario("http_503", "HTTP_503", category="HTTP"),
    FailureScenario(
        "http_401", "AUTH_RELOGIN_FAILED", "HTTP_401", "AUTHENTICATION", 2, 2, False, False
    ),
    FailureScenario(
        "http_403", "AUTH_RELOGIN_FAILED", "HTTP_403", "AUTHENTICATION", 2, 2, False, False
    ),
    FailureScenario(
        "invalid_json",
        "RESPONSE_JSON_INVALID",
        category="PARSE",
        query_calls=1,
        retry_recommended=False,
    ),
    FailureScenario(
        "unknown_schema",
        "UPLOAD_TYPES_INVALID",
        category="PARSE",
        query_calls=1,
        retry_recommended=False,
    ),
    FailureScenario(
        "relogin_dns",
        "AUTH_RELOGIN_FAILED",
        "NETWORK_DNS_FAILED",
        "AUTHENTICATION",
        2,
        1,
        False,
        False,
    ),
    FailureScenario(
        "password_timeout",
        "NETWORK_READ_TIMEOUT",
        password_posts=1,
        query_calls=0,
        retry_safe=False,
        retry_recommended=False,
    ),
    FailureScenario(
        "conditional_write_denied",
        "AUTH_HTTP_DENIED",
        "HTTP_403",
        "AUTHENTICATION",
        1,
        1,
        False,
        False,
    ),
    FailureScenario("read_timeout_once", query_calls=2),
    FailureScenario("http_503_once", query_calls=2),
)

DATA_SCENARIOS = (
    "empty_list",
    "missing_reference",
    "patient_not_found",
    "empty_soap",
    "unknown_soap",
    "partial_numeric",
    "attachment_only",
    "metadata_only",
    "not_executed",
    "binary",
    "opaque_json",
    "unassessed_report",
)


def _response(request, body: str, status: int = 200, location: str = ""):
    response = requests.Response()
    response.request, response.url, response.status_code = request, request.url, status
    response._content = body.encode("utf-8")
    response._content_consumed = True
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    if location:
        response.headers["Location"] = location
    return response


class _FailureHTTP(_MemoryHTTP):
    def __init__(self, scenario: FailureScenario):
        super().__init__(LoginScenario(scenario.name))
        self.mode = scenario.name

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        if path == "/login.do" and (
            self.mode == "password_timeout"
            or (self.mode == "relogin_dns" and self.password_posts == 1)
        ):
            self.password_posts += 1
            if self.mode == "password_timeout":
                raise requests.ReadTimeout("synthetic password POST timeout")
            raise requests.ConnectionError(socket.gaierror("synthetic DNS failure"))
        if path not in {"/PRQWeb/QueryUploadMR.do", "/PRQWeb/EMRProcess.do"}:
            return super().send(request, **kwargs)
        self.query_calls += 1
        self.calls.append({"method": request.method, "path": path})
        errors = {
            "dns": lambda: requests.ConnectionError(socket.gaierror("synthetic DNS")),
            "connect_timeout": lambda: requests.ConnectTimeout("synthetic connect timeout"),
            "read_timeout": lambda: requests.ReadTimeout("synthetic read timeout"),
            "connection": lambda: requests.ConnectionError("synthetic connection failure"),
            "proxy": lambda: requests.exceptions.ProxyError("synthetic proxy failure"),
            "tls_certificate": lambda: requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
            "tls_protocol": lambda: requests.exceptions.SSLError("WRONG_VERSION_NUMBER"),
        }
        if self.mode in errors:
            raise errors[self.mode]()
        if self.mode == "read_timeout_once" and self.query_calls == 1:
            raise requests.ReadTimeout("synthetic first read timeout")
        if self.mode == "http_503_once" and self.query_calls == 1:
            return _response(request, "synthetic unavailable", 503)
        if self.mode.startswith("http_") and not self.mode.endswith("_once"):
            return _response(request, "synthetic HTTP failure", int(self.mode[-3:]))
        if self.mode == "conditional_write_denied":
            return _response(request, "synthetic conditional write denial", 403)
        if self.mode == "relogin_dns":
            return _response(request, "", 302, _ORIGIN + "/index.do")
        if self.mode == "invalid_json":
            return _response(request, "<html>synthetic changed page</html>")
        if self.mode == "unknown_schema":
            return _response(request, '{"unexpected":true}')
        return _response(request, '[{"maintp":"OPD","mainnm":"Synthetic"}]')


def run_failure_scenario(scenario: FailureScenario) -> dict:
    adapter = _FailureHTTP(scenario)
    session = requests.Session()
    session.trust_env = False
    # Intercept every HTTP(S) origin; no live endpoints or credentials are used.
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=0)
    settings = SDKSettings(
        portal_base_url=_ORIGIN,
        prq_base_url=_ORIGIN + "/PRQWeb",
        request_policy=policy,
    )
    with VghksSDK(
        settings=settings,
        credentials=PortalCredentials("TST1", "synthetic-password"),
        transport=SafeSessionTransport(policy=policy, session=session, sleeper=lambda _: None),
    ) as sdk:
        if scenario.name == "password_timeout":
            result = acquire(sdk.auth.login)
        elif scenario.name == "conditional_write_denied":
            write = operation_spec("prq.access_review")
            result = acquire(
                lambda: sdk._runtime.execute(
                    operation_spec("prq.visit_cases"),
                    lambda: sdk._runtime.request_text(write, _ORIGIN + write.path),
                    operation_name="synthetic_conditional_write",
                )
            )
        else:
            result = sdk.queries.run_result("prq.upload_types")
    info = result.error
    root_code = info.root_cause.code if info else ""
    passed = (
        (info.code if info else "") == scenario.code
        and root_code == (scenario.root_code or scenario.code)
        and adapter.password_posts == scenario.password_posts
        and adapter.query_calls == scenario.query_calls
    )
    if info:
        passed = passed and (
            info.category == scenario.category
            and info.retry_safe is scenario.retry_safe
            and info.retry_recommended is scenario.retry_recommended
        )
    return {
        "evidence": "SIMULATED",
        "scenario": scenario.name,
        "passed": passed,
        "status": result.status,
        "error": to_jsonable(info),
        "root_code": root_code,
        "password_posts": adapter.password_posts,
        "query_calls": adapter.query_calls,
        "expected_code": scenario.code,
        "expected_root_code": scenario.root_code or scenario.code,
    }


def run_data_scenario(name: str) -> dict:
    case = VisitCase("00000000", date(2026, 1, 2), "O", "C1", "70", "Synthetic", 0)
    ref = OrderReportRef(case.mrn, case.case_no, case.case_type, "1")
    actions = {
        "empty_list": (lambda: [], "EMPTY", "EMPTY"),
        "missing_reference": (lambda: None, "EMPTY", "EMPTY"),
        "patient_not_found": (
            lambda: parse_patient_demographics([], case.mrn),
            "EMPTY",
            "NOT_FOUND",
        ),
        "empty_soap": (
            lambda: parse_soap('<div id="data">查無SOAP資料</div>', case),
            "EMPTY",
            "EMPTY",
        ),
        "unknown_soap": (
            lambda: parse_soap("<html>synthetic changed layout</html>", case),
            "ERROR",
            None,
        ),
        "partial_numeric": (
            lambda: NumericReport(
                case,
                (
                    NumericTable(
                        "Synthetic",
                        ("OD", "OS"),
                        (("1", "2"),),
                        parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",),
                    ),
                ),
            ),
            "PARTIAL",
            "AVAILABLE",
        ),
        "attachment_only": (
            lambda: parse_order_report(
                "<table><tr><th>報告內容</th><td>詳見附件"
                '<script>var p="//nfs01p/EMRU/00000000/report.pdf";</script></td></tr></table>',
                reference=ref,
            ),
            "OK",
            "ATTACHMENT_ONLY",
        ),
        "metadata_only": (
            lambda: parse_order_report(
                "<table><tr><th>檢查項目</th><td>Synthetic</td></tr></table>",
                reference=ref,
            ),
            "OK",
            "METADATA_ONLY",
        ),
        "not_executed": (
            lambda: ClinicalOrder(case.mrn, case.case_no, "O", "Synthetic", status="未執行"),
            "OK",
            "NOT_EXECUTED",
        ),
        "binary": (
            lambda: BinaryAsset(b"synthetic binary", "application/pdf"),
            "OK",
            "BINARY_AVAILABLE",
        ),
        "opaque_json": (lambda: {}, "OK", "UNKNOWN"),
        "unassessed_report": (lambda: OrderReport(ref, {}), "OK", "UNKNOWN"),
    }
    action, status, availability = actions[name]
    result = acquire(action)
    observed = result.data.availability if result.data else None
    return {
        "evidence": "SIMULATED",
        "scenario": name,
        "passed": result.status == status and observed == availability,
        "status": result.status,
        "data": to_jsonable(result.data),
        "error": to_jsonable(result.error),
    }
