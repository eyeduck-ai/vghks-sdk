from __future__ import annotations

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from vghks_sdk import (
    ErrorInfo,
    LoginRejectedError,
    NotFoundError,
    ParseError,
    PortalCredentials,
    RequestError,
    SDKError,
    SDKSettings,
    VghksSDK,
    acquire,
    assess_data,
    error_info,
)
from vghks_sdk.core.config import RequestPolicy
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import _counts
from vghks_sdk.live.failure_simulation import (
    DATA_SCENARIOS,
    FAILURE_SCENARIOS,
    run_data_scenario,
    run_failure_scenario,
)
from vghks_sdk.live.login_simulation import _ORIGIN, LoginScenario, _MemoryHTTP
from vghks_sdk.models import AuthCheckTarget, NumericReport, NumericTable, VisitCase, to_jsonable
from vghks_sdk.offline.replay import identify_operation, replay_response
from vghks_sdk.queries import Queries


class AcquisitionTests(unittest.TestCase):
    def test_optional_envelope_preserves_direct_api_value_and_calls_once(self):
        value = [VisitCase("TEST001", date(2026, 1, 2), "O", "C1", "70", "Synthetic")]
        action = Mock(return_value=value)
        sdk = SimpleNamespace(records=SimpleNamespace(get_visit_cases=action))
        queries = Queries(sdk)
        self.assertIs(queries.run("prq.visit_cases", mrn="TEST001"), value)
        result = queries.run_result("prq.visit_cases", mrn="TEST001")
        self.assertIs(result.value, value)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.data.availability, "AVAILABLE")
        self.assertEqual(action.call_count, 2)

    def test_data_absence_and_http_or_parse_failures_remain_distinct(self):
        for exc, status, availability in (
            (NotFoundError("synthetic"), "EMPTY", "NOT_FOUND"),
            (RequestError("synthetic", status_code=404, code="HTTP_404"), "ERROR", None),
            (ParseError("synthetic changed layout"), "ERROR", None),
        ):
            with self.subTest(type=type(exc).__name__):
                action = Mock(side_effect=exc)
                result = acquire(action)
                action.assert_called_once_with()
                self.assertEqual(result.status, status)
                self.assertEqual(result.data.availability if result.data else None, availability)
                self.assertEqual(result.error.code, exc.info.code)

    def test_opaque_values_are_unknown_and_programming_errors_propagate(self):
        self.assertEqual(assess_data({}).availability, "UNKNOWN")
        self.assertIsNone(assess_data({}).complete)
        with self.assertRaises(KeyError):
            acquire(Mock(side_effect=KeyError("synthetic bug")))

    def test_failed_readiness_report_cannot_be_a_successful_acquisition(self):
        issue = ErrorInfo("PORTAL_LOGIN_REJECTED", "AUTHENTICATION")
        report = make_auth_report(
            [AuthCheckTarget("portal", (), "", False, 0, 0, (), "ERROR", issue)]
        )
        result = acquire(lambda: report)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "PORTAL_LOGIN_REJECTED")

    def test_partial_parsing_preserves_raw_cells_and_issue_codes(self):
        case = VisitCase("TEST001", date(2026, 1, 2), "O", "C1", "70", "Synthetic")
        report = NumericReport(
            case,
            (
                NumericTable(
                    "Synthetic",
                    ("OD", "OS"),
                    (("error", ""),),
                    parsing_issues=("NUMERIC_HEADER_UNALIGNED",),
                ),
            ),
        )
        result = acquire(lambda: report)
        self.assertEqual(result.status, "PARTIAL")
        self.assertIs(result.value, report)
        self.assertEqual(result.value.tables[0].rows, (("error", ""),))
        self.assertEqual(result.data.issues[0].code, "NUMERIC_HEADER_UNALIGNED")

    def test_only_fully_aligned_eye_header_issues_are_recoverable_warnings(self):
        from dataclasses import replace

        case = VisitCase("TEST001", date(2026, 1, 2), "O", "C1", "70", "Synthetic")
        table = NumericTable(
            "Synthetic", ("日期", "Synthetic", "OS", "OD"),
            (("2026-01-02", "error", ""),),
            header_rows=(("日期", "Synthetic"), ("OS", "OD")),
            column_paths=(("日期",), ("Synthetic", "OS"), ("Synthetic", "OD")),
            parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",),
        )
        report = NumericReport(case, (table,))
        result = acquire(lambda: report)
        self.assertEqual(result.status, "OK")
        self.assertTrue(result.data.complete)
        self.assertEqual(result.data.issues, ())
        self.assertEqual(result.data.warnings[0].code, "NUMERIC_HEADER_SPAN_MISMATCH")
        self.assertEqual(assess_data([report]).warnings, result.data.warnings)
        self.assertIs(result.value, report)
        self.assertEqual(table.parsing_issues, ("NUMERIC_HEADER_SPAN_MISMATCH",))
        self.assertEqual(_counts(report)["numeric_warning_count"], 1)
        self.assertEqual(_counts(report)["numeric_error_count"], 0)
        for bad in (
            replace(table, column_paths=()),
            replace(table, rows=(("2026-01-02", "error"),)),
            replace(table, header_rows=(("Unknown", "Synthetic"), ("OS", "OD"))),
            replace(table, parsing_issues=(*table.parsing_issues, "NUMERIC_HEADER_UNALIGNED")),
        ):
            with self.subTest(table=bad):
                self.assertEqual(acquire(lambda bad=bad: NumericReport(case, (bad,))).status, "PARTIAL")

    def test_error_chain_is_safe_explicit_and_bounded(self):
        root = RequestError("SECRET", code="NETWORK_DNS_FAILED", retry_safe=False)
        outer = LoginRejectedError("SECRET")
        outer.__context__ = root
        self.assertIsNone(error_info(outer).cause)
        outer.__cause__ = root
        info = error_info(outer)
        self.assertEqual(info.root_cause.code, "NETWORK_DNS_FAILED")
        self.assertNotIn("SECRET", json.dumps(to_jsonable(info)))
        root.__cause__ = outer
        self.assertEqual(error_info(outer).root_cause.code, "NETWORK_DNS_FAILED")

    def test_retry_recommendation_requires_safety_and_unsafe_context_wins(self):
        self.assertFalse(ErrorInfo("X", "NETWORK", retry_recommended=True).retry_recommended)
        exc = RequestError("synthetic", retry_safe=True, retry_recommended=True)
        exc.with_context(retry_safe=False)
        exc.with_context(retry_safe=True)
        self.assertFalse(exc.info.retry_safe)
        self.assertFalse(exc.info.retry_recommended)

    def test_public_login_metadata_preserves_http_cause_and_prevents_password_replay(self):
        class LandingHTTP(_MemoryHTTP):
            def send(self, request, **kwargs):
                if (
                    self.scenario.name == "landing_timeout"
                    and urlsplit(request.url).path == "/myPortal.do"
                ):
                    raise requests.ReadTimeout("synthetic landing GET timeout")
                return super().send(request, **kwargs)

        for on_demand in (False, True):
            for mode, root in (
                ("login_403", "HTTP_403"),
                ("landing_timeout", "NETWORK_READ_TIMEOUT"),
            ):
                with self.subTest(on_demand=on_demand, mode=mode):
                    adapter = LandingHTTP(LoginScenario(mode))
                    session = requests.Session()
                    session.trust_env = False
                    session.mount("https://", adapter)
                    session.mount("http://", adapter)
                    policy = RequestPolicy(
                        min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=0
                    )
                    with VghksSDK(
                        settings=SDKSettings(
                            portal_base_url=_ORIGIN,
                            prq_base_url=_ORIGIN + "/PRQWeb",
                            request_policy=policy,
                        ),
                        credentials=PortalCredentials("TST1", "synthetic-password"),
                        transport=SafeSessionTransport(
                            policy=policy, session=session, sleeper=lambda _: None
                        ),
                    ) as sdk:
                        action = sdk.records.get_upload_types if on_demand else sdk.auth.login
                        with self.assertRaises(SDKError) as caught:
                            action()
                    info = caught.exception.info
                    self.assertEqual(info.root_cause.code, root)
                    self.assertFalse(info.retry_safe)
                    self.assertFalse(info.retry_recommended)
                    self.assertEqual(adapter.password_posts, 1)
                    self.assertEqual(adapter.query_calls, 0)

    def test_real_sdk_failure_and_data_scenarios_use_no_sockets(self):
        with patch("socket.socket", side_effect=AssertionError("simulation opened a socket")):
            for scenario in FAILURE_SCENARIOS:
                with self.subTest(scenario=scenario.name):
                    result = run_failure_scenario(scenario)
                    self.assertTrue(result["passed"], result)
            for name in DATA_SCENARIOS:
                with self.subTest(scenario=name):
                    result = run_data_scenario(name)
                    self.assertTrue(result["passed"], result)

    def test_relogin_dns_is_visible_in_serialized_metadata(self):
        scenario = next(item for item in FAILURE_SCENARIOS if item.name == "relogin_dns")
        result = run_failure_scenario(scenario)
        self.assertEqual(result["error"]["phase"], "REAUTHENTICATION")
        self.assertEqual(result["error"]["cause"]["code"], "NETWORK_DNS_FAILED")
        self.assertFalse(result["error"]["retry_recommended"])

    def test_offline_absence_and_registration_pagination_are_not_parse_errors(self):
        result = replay_response("webmaas.demographics", b"[]", {"patno": "TEST001"})
        self.assertEqual(result["status"], "EMPTY")
        self.assertEqual(result["data_availability"], "NOT_FOUND")
        self.assertEqual(
            identify_operation(
                {
                    "method": "GET",
                    "url": "https://synthetic.test/webmaas/RSV/RSV11W001.do?d-123-p=2&patno=TEST001",
                }
            ),
            "webmaas.registration_query",
        )


if __name__ == "__main__":
    unittest.main()
