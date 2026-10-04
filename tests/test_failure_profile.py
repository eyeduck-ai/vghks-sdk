from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk import ErrorInfo, NotFoundError, ParseError
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.atomic import build_test_plan
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.failures import run_failure_test
from vghks_sdk.live.presets import FAILURE_QUERIES
from vghks_sdk.live_test_app import main
from vghks_sdk.models import (
    AuthCheckTarget,
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderReportRef,
    VisitCase,
)
from vghks_sdk.offline.analyze import _structured_root_issue, _verified_cookie_recovery
from vghks_sdk.queries import Queries


def fake_sdk(*, visits_error=None, login_error=False):
    targets = [
        AuthCheckTarget(
            key,
            (),
            "",
            False,
            1,
            0,
            (),
            "ERROR" if login_error and key == "portal" else "OK",
            ErrorInfo("PORTAL_LOGIN_REJECTED", "AUTHENTICATION") if login_error else None,
        )
        for key in ("portal", "sectord", "prq", "webmaas")
    ]
    auth = SimpleNamespace(generation=1)
    transport = SimpleNamespace(clear=False)
    transport.reset_cookies = lambda: setattr(transport, "clear", True)

    def catalog():
        if transport.clear:
            auth.generation += 1
            transport.clear = False
        return [{"maintp": "OPD", "mainnm": "Synthetic"}]

    sdk = SimpleNamespace(
        auth=SimpleNamespace(check=Mock(return_value=make_auth_report(targets))),
        patients=SimpleNamespace(
            get_basic_info=Mock(return_value=None), get_registration_history=Mock(return_value=[])
        ),
        records=SimpleNamespace(
            get_visit_cases=Mock(return_value=[], side_effect=visits_error),
            get_soap=Mock(),
            get_numeric_report=Mock(),
            get_upload_types=Mock(side_effect=catalog),
        ),
        orders=SimpleNamespace(get_case_orders=Mock()),
        _runtime=SimpleNamespace(auth=auth, transport=transport),
    )
    sdk.queries = Queries(sdk)
    return sdk


class FailureProfileTests(unittest.TestCase):
    def test_plan_is_bounded_has_simulations_and_one_negative_before_login(self):
        with patch("requests.sessions.Session.request") as network:
            plan = build_test_plan(LiveTestConfig(profile="failures", test_mrn="TEST001"))
        network.assert_not_called()
        self.assertEqual([item["key"] for item in plan["operations"]], list(FAILURE_QUERIES))
        self.assertEqual(plan["max_cases"], 2)
        self.assertEqual(plan["negative_password_post_limit"], 1)
        self.assertTrue(plan["negative_tests_before_correct_login"])
        self.assertEqual(plan["anonymous_query_password_post_limit"], 0)
        self.assertIn("auth_edge.password_warning_visible", plan["simulated_scenarios"])
        self.assertIn("transport.relogin_dns", plan["simulated_scenarios"])

    def test_empty_results_and_unobserved_failure_categories_are_not_failures(self):
        sdk = fake_sdk()
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            root = Path(temporary)
            result = run_failure_test(
                sdk, LiveTestConfig(profile="failures", test_mrn="TEST001"), output_dir=root
            )
            steps = {step.name: step for step in result.steps}
            self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
            self.assertEqual(steps["failures.live.visits"].status, "EMPTY")
            self.assertEqual(steps["failures.live.soap"].status, "NO_SAMPLE")
            self.assertEqual(steps["failures.live.cookie_loss"].status, "OK")
            coverage = json.loads((root / "coverage.json").read_text())["failure_classification"]
            self.assertEqual(coverage["simulated_passed"], coverage["simulated_count"])
            self.assertEqual(coverage["live_categories"]["EMPTY"]["status"], "OBSERVED")
            self.assertEqual(coverage["live_categories"]["NETWORK"]["status"], "NO_SAMPLE")
            self.assertFalse(coverage["natural_ttl"]["verified"])
            self.assertTrue(
                all(
                    step.details["evidence"] == "SIMULATED"
                    for step in result.steps
                    if step.name.startswith("failures.simulated.")
                )
            )
        sdk.records.get_soap.assert_not_called()

    def test_visit_failure_blocks_dependents_and_retains_independent_queries(self):
        sdk = fake_sdk(visits_error=ParseError("synthetic changed page", code="PRQ_CASES_INVALID"))
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            result = run_failure_test(
                sdk,
                LiveTestConfig(profile="failures", test_mrn="TEST001"),
                output_dir=Path(temporary),
            )
            steps = {step.name: step for step in result.steps}
            self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
            self.assertEqual(steps["failures.live.visits"].issue.code, "PRQ_CASES_INVALID")
            self.assertEqual(steps["failures.live.soap"].status, "BLOCKED")
            self.assertEqual(steps["failures.live.catalog"].status, "OK")
        sdk.patients.get_registration_history.assert_called_once()
        sdk.records.get_soap.assert_not_called()

    def test_failed_login_leaves_simulations_independent_and_stops_data_queries(self):
        sdk = fake_sdk(login_error=True)
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            result = run_failure_test(
                sdk,
                LiveTestConfig(profile="failures", test_mrn="TEST001"),
                output_dir=Path(temporary),
            )
            coverage = json.loads((Path(temporary) / "coverage.json").read_text())[
                "failure_classification"
            ]
            self.assertEqual(coverage["live_categories"]["AUTHENTICATION"]["status"], "OBSERVED")
            self.assertTrue(coverage["additional_error_steps"])
        self.assertEqual(result.status, "AUTHENTICATION_FAILED")
        self.assertTrue(
            all(
                step.status == "OK"
                for step in result.steps
                if step.name.startswith("failures.simulated.")
            )
        )
        sdk.records.get_visit_cases.assert_not_called()
        sdk.patients.get_basic_info.assert_not_called()

    def test_missing_or_failed_order_reference_sources_are_classified_without_aborting(self):
        case = VisitCase("TEST001", date(2026, 1, 2), "O", "C1", "70", "Synthetic")
        detail_ref = OrderDetailRef(case.mrn, case.case_no, case.case_type, "1")
        report_ref = OrderReportRef(case.mrn, case.case_no, case.case_type, "1")
        order = ClinicalOrder(
            case.mrn,
            case.case_no,
            case.case_type,
            "Synthetic",
            status="已執行",
            detail_ref=detail_ref,
        )
        for stage in ("orders", "detail", "report"):
            for failure in (
                None,
                NotFoundError("synthetic absence"),
                ParseError("synthetic layout"),
            ):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    sdk = fake_sdk()
                    sdk.records.get_visit_cases.return_value = [case]
                    sdk.records.get_soap.return_value = None
                    sdk.records.get_numeric_report.return_value = None
                    sdk.orders.get_case_orders.return_value = [order]
                    sdk.orders.get_order_detail = Mock(
                        return_value=OrderDetail(detail_ref, {}, (report_ref,))
                    )
                    sdk.orders.get_order_report = Mock()
                    method = {
                        "orders": sdk.orders.get_case_orders,
                        "detail": sdk.orders.get_order_detail,
                        "report": sdk.orders.get_order_report,
                    }[stage]
                    method.return_value = None
                    method.side_effect = failure
                    with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
                        result = run_failure_test(
                            sdk,
                            LiveTestConfig(profile="failures", test_mrn=case.mrn),
                            output_dir=Path(temporary),
                        )
                    steps = {step.name: step for step in result.steps}
                    self.assertEqual(steps["failures.live.catalog"].status, "OK")
                    expected = "BLOCKED" if isinstance(failure, ParseError) else "NO_SAMPLE"
                    self.assertEqual(steps["failures.live.pacs"].status, expected)
                    if stage != "report":
                        self.assertEqual(steps["failures.live.report"].status, expected)

    def test_cookie_recovery_evidence_requires_actual_generation_change(self):
        step = {
            "name": "failures.live.cookie_loss",
            "status": "OK",
            "details": {
                "evidence": "LIVE_COOKIE_LOSS",
                "recovery_observed": True,
                "natural_ttl_verified": False,
                "generation_before": 1,
                "generation_after": 2,
            },
        }
        self.assertTrue(_verified_cookie_recovery(step))
        step["details"]["generation_after"] = 1
        self.assertFalse(_verified_cookie_recovery(step))
        self.assertEqual(
            _structured_root_issue(
                {
                    "code": "AUTH_RELOGIN_FAILED",
                    "cause": {
                        "code": "NETWORK_DNS_FAILED",
                        "category": "NETWORK",
                    },
                }
            )["code"],
            "NETWORK_DNS_FAILED",
        )

    def test_zero_argument_build_profile_selects_failures(self):
        with (
            patch(
                "vghks_sdk.live_test_app.build_identity",
                return_value={"default_profile": "failures"},
            ),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as runner,
        ):
            self.assertEqual(main([]), 0)
        self.assertEqual(runner.call_args.args[0].profile, "failures")
        self.assertTrue(runner.call_args.args[0].bundled_failures)


if __name__ == "__main__":
    unittest.main()
